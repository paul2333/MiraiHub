# -*- coding: utf-8 -*-
"""数据看板（/api/stats/dashboard）的回归锁定。

看板是「一屏给结论」的页面，最怕两件事：

1. **口径错**：失败请求被算进 Token/积分（消费虚高），或者成功率把失败漏掉
   永远显示 100%。这里用「成功 + 失败」混合样本把两条口径都钉住。
2. **图形对不上卡片**：堆叠分布的桶必须连续（没有请求的小时也要留空位），
   否则三天的空白会被压成一条线，看起来像服务一直在忙。

另外锁住两条策略：
* 超过 7 天自动降级为按天 —— 30 天 × 24 桶 = 720 根柱子没有信息量；
* 「其他 N 个模型」必须把 Top N 之外的量真实滚进去，不能凭空丢掉。
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from admin import stats_cache  # noqa: E402
from admin.db import get_db  # noqa: E402
from admin.models import (Account, Base, ModelConfig, UsageLog)  # noqa: E402
from admin.routers import stats as stats_routes  # noqa: E402
from admin.security import require_admin  # noqa: E402


def make_app():
    app = FastAPI()
    app.include_router(stats_routes.router)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[require_admin] = lambda: True
    app.dependency_overrides[get_db] = _override
    return app, session_factory


class StatsDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app, cls.session_factory = make_app()
        cls.client = TestClient(cls.app)
        # 本机若开了 Redis，L2 会把上一次运行的看板结果留在库里 ——
        # 用例之间必须隔离，所以这里把 L2 直接钉成「不可用」（只测计算口径）。
        stats_cache._redis = None
        stats_cache._redis_ready = True

    def setUp(self):
        # 统计缓存是进程级共享的：不清会把上一个用例的结果带进来。
        stats_cache._l1.clear()
        from admin.models import AccountModelCooldown, UsageRollup, UsageRollupState

        db = self.session_factory()
        # 汇总表是 `usage_logs` 的缓存：只删原表不删汇总，上一个用例写进去的
        # 行会留在汇总里（`ensure_coverage` 认为窗口没缺口就不重算），
        # 下一个用例就凭空多出一个模型 —— 这类脏数据极难反查，一律清干净。
        db.query(UsageRollup).delete()
        db.query(UsageRollupState).delete()
        db.query(AccountModelCooldown).delete()
        db.query(UsageLog).delete()
        db.commit()
        now = datetime.utcnow()
        rows = []
        # 24 条成功 + 6 条失败，分布在两个平台 / 三个模型上。
        for i in range(24):
            rows.append(UsageLog(
                api_key_id=1, account_id=1 + (i % 2),
                model=["dfmodel", "kmodel", "gpt-4.1"][i % 3],
                platform="workbuddy" if i % 2 else "loomy",
                use_case="chat", credits=0.5,
                prompt_tokens=1000, completion_tokens=500, total_tokens=1500,
                cached_tokens=200, ttfb_ms=400, latency_ms=12000,
                error_kind="", created_at=now - timedelta(minutes=11 * i),
            ))
        for i in range(6):
            rows.append(UsageLog(
                api_key_id=1, account_id=1, model="dfmodel", platform="workbuddy",
                use_case="chat", credits=0, prompt_tokens=None, completion_tokens=None,
                total_tokens=None, cached_tokens=None, ttfb_ms=None, latency_ms=None,
                error_kind="server", created_at=now - timedelta(minutes=17 * i),
            ))
        db.add_all(rows)
        db.commit()
        db.close()

    def get(self, **params):
        response = self.client.get("/api/stats/dashboard", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_kpi_separates_success_and_failure(self):
        data = self.get(days=1)
        summary = data["summary"]
        self.assertEqual(summary["requests"], 30)
        self.assertEqual(summary["successes"], 24)
        self.assertEqual(summary["failures"], 6)
        self.assertAlmostEqual(summary["success_rate"], 0.8, places=4)
        # 失败请求没有 Token/积分，绝不能算进消费
        self.assertEqual(summary["total_tokens"], 24 * 1500)
        self.assertAlmostEqual(summary["credits"], 24 * 0.5, places=2)
        self.assertGreater(summary["throughput_tps"], 0)
        self.assertAlmostEqual(summary["cache_hit_rate"], 0.2, places=3)
        # RPM/TPM 是「窗口内每分钟」，量级要落在合理区间而不是除以请求数
        self.assertGreater(summary["rpm"], 0)
        self.assertGreater(summary["tpm"], 0)
        self.assertEqual(data["errors"], [{"kind": "server", "count": 6}])

    def test_distribution_buckets_are_contiguous(self):
        data = self.get(days=1)
        buckets = data["distribution"]["buckets"]
        self.assertGreaterEqual(len(buckets), 20)          # 24 小时窗口按 1 小时铺满
        self.assertEqual(len(buckets), len(set(buckets)))   # 不重复
        self.assertEqual(buckets, sorted(buckets))          # 按时间正序
        series = data["distribution"]["series"]
        self.assertTrue(series)
        # 每个系列的 values 长度必须与桶数一致，否则前端堆叠会错位
        for item in series:
            self.assertEqual(len(item["values"]), len(buckets))

    def test_other_models_rollup_keeps_totals(self):
        data = self.get(days=1, top_models=1)
        series = data["distribution"]["series"]
        self.assertEqual(len(series), 2, "Top1 之后必须有「其他 N 个模型」")
        self.assertTrue(series[-1]["name"].startswith("其他"))
        stacked = sum(round(sum(item["values"]), 2) for item in series)
        self.assertAlmostEqual(stacked, data["summary"]["credits"], places=2)

    def test_ranking_shares_add_up(self):
        data = self.get(days=1)
        ranking = data["ranking"]
        self.assertEqual(len(ranking), 3)
        self.assertAlmostEqual(sum(row["share"] for row in ranking), 1.0, places=3)
        self.assertEqual(ranking[0]["requests"], 14)       # dfmodel: 8 成功 + 6 失败

    def test_long_window_falls_back_to_days(self):
        data = self.get(days=30, granularity="hour")
        self.assertEqual(data["window"]["granularity"], "day")
        self.assertLessEqual(len(data["distribution"]["buckets"]), 31)
        self.assertEqual(data["summary"]["requests"], 30)

    def test_platform_filter(self):
        data = self.get(days=1, platform="loomy")
        self.assertEqual(data["platform_label"], data["platforms"][0]["label"])
        self.assertEqual(len(data["platforms"]), 1)
        self.assertEqual(data["summary"]["requests"], 12)  # i%2==0 的 12 条成功请求
        self.assertEqual(data["summary"]["failures"], 0)

    def test_empty_window_is_safe(self):
        data = self.get(days=1, model="not-exist-model")
        self.assertEqual(data["summary"]["requests"], 0)
        self.assertIsNone(data["summary"]["success_rate"])
        self.assertEqual(data["ranking"], [])
        self.assertTrue(data["distribution"]["series"] == [])

    def test_rolling_window_counts_only_rows_inside_partial_hours(self):
        from admin import rollup

        end = datetime(2025, 1, 2, 12, 30)
        start = end - timedelta(days=1)
        first_hour = rollup.hour_start(start)
        last_hour = rollup.hour_start(end)

        class FixedDatetime(datetime):
            @classmethod
            def utcnow(cls):
                return end

        app, session_factory = make_app()
        with session_factory() as db:
            for timestamp in (
                start - timedelta(microseconds=1), start, start + timedelta(minutes=10),
                first_hour + timedelta(hours=1), last_hour, end - timedelta(microseconds=1), end,
            ):
                db.add(UsageLog(model="boundary-model", platform="workbuddy",
                                created_at=timestamp, credits=1, total_tokens=60,
                                completion_tokens=40, latency_ms=900, ttfb_ms=400))
            db.commit()
            covered = rollup.ensure_coverage(db, until=end)
            self.assertEqual(covered, (first_hour, last_hour))

        with patch.object(stats_routes, "datetime", FixedDatetime), \
                patch.object(rollup, "datetime", FixedDatetime), TestClient(app) as client:
            dashboard = client.get("/api/stats/dashboard", params={"days": 1, "model": "boundary-model"})
            self.assertEqual(dashboard.status_code, 200, dashboard.text)
            data = dashboard.json()
            self.assertEqual(data["source"], "rollup")
            self.assertEqual(data["summary"]["requests"], 5)
            self.assertEqual(data["summary"]["credits"], 5)
            self.assertEqual(data["summary"]["total_tokens"], 300)
            self.assertEqual(sum(data["trend"]["total_requests"]), 5)
            self.assertEqual(sum(data["distribution"]["series"][0]["values"]), 5)

            market = client.get("/api/stats/model-market", params={"hours": 24})
            self.assertEqual(market.status_code, 200, market.text)
            item = next(item for item in market.json()["items"] if "boundary-model" in item["models"])
            self.assertEqual(item["requests"], 5)
            self.assertEqual(item["tps"], 80)

    def test_window_series_keeps_throughput_on_raw_and_rollup_paths(self):
        from admin import rollup

        start = datetime(2025, 1, 2, 12)
        end = start + timedelta(hours=1)
        with self.session_factory() as db:
            db.add(UsageLog(model="throughput-model", created_at=start, completion_tokens=40,
                            latency_ms=900, ttfb_ms=400))
            db.commit()
            covered = rollup.ensure_coverage(db, until=end)
            raw = rollup.window_series(db, start, end, "hour", (None, None), model="throughput-model")
            aggregated = rollup.window_series(db, start, end, "hour", covered, model="throughput-model")

        self.assertEqual((raw[0]["tps_sum"], raw[0]["tps_count"]), (80, 1))
        self.assertEqual(raw, aggregated)

    def test_sum_coercion_handles_mysql_decimal(self):
        """MySQL 的 SUM() 返回 Decimal；直接拿它除 float 会 500（线上已复现）。"""
        from decimal import Decimal

        from admin import rollup

        latency = rollup._coerce("latency_sum", Decimal("12000"))
        self.assertEqual(latency, 12000)
        self.assertIsInstance(latency, int)
        credits = rollup._coerce("credits", Decimal("0.5"))
        self.assertAlmostEqual(credits, 0.5)
        self.assertIsInstance(credits, float)
        # None 也要能落地（空窗口 / 全失败）
        self.assertEqual(rollup._coerce("requests", None), 0)
        self.assertEqual(rollup._coerce("credits", None), 0.0)
        # 复现事故现场：int/Decimal 混算必须不出异常
        self.assertAlmostEqual(latency / float("0.5"), 24000.0)

    def test_model_market_joins_pricing_and_availability(self):
        """模型广场：用量来自汇总、价格来自配置、可用性看活跃账号 − 冷却。"""
        db = self.session_factory()
        try:
            db.add(ModelConfig(level="system", platform="workbuddy",
                               model_id="dfmodel", enabled=1, credit_multiplier=0.5))
            db.add(ModelConfig(level="system", platform="workbuddy",
                               model_id="never-used-model", enabled=1,
                               credit_multiplier=0))
            db.add(Account(platform="workbuddy", name="a1", uid="u1",
                           auth_json="{}", status="active"))
            db.commit()
        finally:
            db.close()

        response = self.client.get("/api/stats/model-market", params={"hours": 24})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["source"], "rollup")
        by_key = {item["key"]: item for item in data["items"]}

        # dfmodel 有真实调用：请求数、成功率、延迟都要在
        hit = next(item for item in data["items"] if "dfmodel" in item["models"])
        self.assertGreater(hit["requests"], 0)
        self.assertFalse(hit["free"])
        self.assertAlmostEqual(hit["min_multiplier"], 0.5, places=3)
        self.assertTrue(hit["available"])
        self.assertGreater(hit["avg_latency_ms"], 0)
        # never-used-model 没有流量也要出现在广场里（0 次调用），且标记为免费
        zero = next(item for item in data["items"]
                    if "never-used-model" in item["models"])
        self.assertEqual(zero["requests"], 0)
        self.assertTrue(zero["free"])
        self.assertIsNone(zero["success_rate"])
        self.assertTrue(by_key)

    # ── 模型广场的时段状态轴 ────────────────────────────────────────────────
    #
    # 卡片底部那一排小方块就是「一格 = 一段时间」。锁四件事：
    #   1. 格子的边界落在**北京时间整点**（用户说的「8 点那格」必须真是 8 点）；
    #   2. 状态是算出来的，不是写死的三色：成功率分档 + 无流量时看冷却记录；
    #   3. 窗口不能因为凑整被偷偷拉长（24 小时就是 24 小时）；
    #   4. 格宽随窗口自适应，格数始终落在 24~31 之间。

    def test_market_bucket_size_is_adaptive(self):
        from admin.routers.stats import _market_bucket_size

        self.assertEqual(_market_bucket_size(24), 1)
        self.assertEqual(_market_bucket_size(48), 2)
        self.assertEqual(_market_bucket_size(72), 3)
        self.assertEqual(_market_bucket_size(168), 6)

    def test_market_grid_aligns_to_local_hours_and_keeps_window_length(self):
        from admin.routers.stats import _market_grid

        now = datetime(2026, 11, 20, 12, 30)          # UTC ⇒ 北京 20:30
        grid = _market_grid(now, 24, 1)
        self.assertEqual(len(grid), 25)               # 20:00 起切到 21:00，25 格整
        self.assertEqual((grid[0][0] + timedelta(hours=8)).strftime("%H:%M"), "20:00")
        self.assertEqual((grid[-1][1] + timedelta(hours=8)).strftime("%H:%M"), "21:00")
        # 每格正好一小时，且首尾相接
        for index in range(len(grid) - 1):
            self.assertEqual(grid[index][1], grid[index + 1][0])
            self.assertEqual(grid[index][1] - grid[index][0], timedelta(hours=1))
        # 3 小时档切出来的是 18:00 / 21:00 / 00:00 这种本地整档，不是 17:00
        hours3 = _market_grid(now, 72, 3)
        self.assertEqual((hours3[0][0] + timedelta(hours=8)).strftime("%H:%M"), "18:00")
        self.assertEqual(len(hours3), 25)

    def test_market_timeline_states_follow_requests_and_cooldown(self):
        """一格一个状态：有流量按成功率分档，没流量才看冷却记录。"""
        from admin.models import AccountModelCooldown  # 只在用例里用

        end = datetime(2026, 11, 20, 12, 30)          # UTC ⇒ 北京 20:30
        start_of_day = datetime(2026, 11, 20, 0, 0)   # UTC ⇒ 北京 08:00

        class FixedDatetime(datetime):
            @classmethod
            def utcnow(cls):
                return end

        db = self.session_factory()
        try:
            # 北京 08:00–09:00：两次成功 → up
            for minute in (10, 20):
                db.add(UsageLog(model="axis-model", platform="workbuddy",
                                created_at=start_of_day + timedelta(minutes=minute),
                                error_kind="", credits=1, total_tokens=100,
                                latency_ms=1000, ttfb_ms=300))
            # 北京 09:00–10:00：只有失败 → down（不是「无样本」）
            for minute in (5, 15, 25):
                db.add(UsageLog(model="axis-model", platform="workbuddy",
                                created_at=start_of_day + timedelta(hours=1, minutes=minute),
                                error_kind="server"))
            # 北京 11:00–12:00：200 次里 2 次失败 = 99% → warn
            for index in range(200):
                db.add(UsageLog(model="axis-model", platform="workbuddy",
                                created_at=start_of_day + timedelta(hours=3, minutes=index % 60),
                                error_kind="server" if index < 2 else "",
                                credits=0.5, total_tokens=20, latency_ms=900, ttfb_ms=300))
            # 北京 13:00 起一个账号进了 model_daily 冷却：那一格没请求也要标 off
            db.add(AccountModelCooldown(account_id=1, model="axis-model",
                                        kind="model_daily", until=end + timedelta(hours=4),
                                        updated_at=start_of_day + timedelta(hours=5)))
            db.commit()
        finally:
            db.close()

        with patch.object(stats_routes, "datetime", FixedDatetime):
            response = self.client.get("/api/stats/model-market", params={"hours": 24})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        buckets = data["buckets"]
        self.assertEqual(len(buckets), 25)
        self.assertEqual(len(buckets), len(buckets and data["items"][0]["timeline"]))

        # 标签必须落在本地整点，且第一格带完整区间
        self.assertEqual(buckets[0]["label"], "20:00")
        self.assertIn("20:00–21:00", buckets[0]["tip"])
        self.assertEqual(buckets[-1]["label"], "20:00")
        self.assertAlmostEqual(buckets[-1]["coverage"], 0.5, places=2)  # 20:30 截断
        self.assertAlmostEqual(buckets[1]["coverage"], 1.0, places=3)

        item = next(row for row in data["items"] if "axis-model" in row["models"])
        cells = {buckets[index]["label"]: item["timeline"][index]
                 for index in range(len(buckets))}
        self.assertEqual(cells["08:00"]["state"], "up")
        self.assertEqual(cells["08:00"]["requests"], 2)
        self.assertAlmostEqual(cells["08:00"]["success_rate"], 1.0)
        self.assertEqual(cells["09:00"]["state"], "down")
        self.assertEqual(cells["09:00"]["success_rate"], 0.0)
        self.assertEqual(cells["11:00"]["state"], "warn")
        # 13:00 那一格没有请求、但有冷却记录 ⇒ off（确定性不可用）
        self.assertEqual(cells["13:00"]["requests"], 0)
        self.assertEqual(cells["13:00"]["cooling"], 1)
        self.assertEqual(cells["13:00"]["state"], "off")
        # 没有请求也没有冷却记录的格 ⇒ idle（没流量 ≠ 坏了）
        self.assertEqual(cells["07:00"]["state"], "idle")

        self.assertEqual(item["measured_cells"], 3)
        self.assertAlmostEqual(item["uptime"], 2 / 3, places=4)
        self.assertEqual(item["cells"]["down"], 1)

    def test_market_timeline_never_invents_unavailable_cells(self):
        """没有冷却记录时，空白格只能是 idle —— 不能把「没人用」画成「坏了」。"""
        data = self.client.get("/api/stats/model-market", params={"hours": 24}).json()
        for item in data["items"]:
            self.assertEqual(item["cells"]["off"], 0)
            self.assertTrue(all(cell["state"] != "off" for cell in item["timeline"]))
            self.assertEqual(len(item["timeline"]), len(data["buckets"]))
        # 窗口请求数必须等于各格之和（格与卡片不能各算一套）
        for item in data["items"]:
            self.assertEqual(item["requests"], sum(cell["requests"] for cell in item["timeline"]))

    def test_market_explicit_bucket_hours(self):
        """允许显式指定格宽：7 天也能按 1 小时铺（前端要钻细节时用）。"""
        data = self.client.get("/api/stats/model-market",
                               params={"hours": 168, "bucket_hours": 1}).json()
        self.assertEqual(data["bucket_hours"], 1)
        self.assertGreaterEqual(len(data["buckets"]), 168)
        self.assertLessEqual(len(data["buckets"]), 170)


if __name__ == "__main__":
    unittest.main(verbosity=2)