"""数据保留（retention）：按天归档 / 删除过期的明细行（P7）。

## 为什么需要

测试实例的 SQLite 已经 380MB，而且**没有任何表做过清理**。按现在的流量
（每天约 2.4 万次请求，每次在库里约 2KB）一年后整库约 17GB：2G 内存的机器
放不下热数据的页缓存，按账号前导做全索引扫描的统计查询会随总行数线性变慢。

## 做什么

每天最多跑一次（由 `rollup` 的后台回填线程顺带调用，不新开线程）：

* `usage_logs`：保留 `ADMIN_USAGE_RETENTION_DAYS` 天（默认 90）。**只删已经被小时
  汇总表覆盖的行**（`created_at < usage_rollup_state.covered_to`）——看板 / 模型广场
  的历史数字由汇总表继续提供，删明细不会让趋势图出现断崖；
* `request_audits`：保留 `ADMIN_AUDIT_RETENTION_DAYS` 天（默认 30）；
* `credit_events` 里 `kind='observed'`（余额观察，来源未确认）：保留
  `ADMIN_CREDIT_OBSERVED_RETENTION_DAYS` 天（默认 30）。确认过的奖励流水永远不删。

删除按主键分批（`ADMIN_RETENTION_BATCH`，默认 5000 行），批间让出 CPU，单轮最多
`ADMIN_RETENTION_BUDGET_S` 秒（默认 60）；没删完就不写「今天已跑」标记，5 分钟后
下一轮接着删。

配置了 `ADMIN_RETENTION_ARCHIVE_DIR` 时，删除前先把整行写成
`<表名>-YYYYMMDD.jsonl.gz` 归档（追加写），归档失败则**本批不删**。

## 默认关闭

删数据属于「会让运维意外」的行为：`ADMIN_RETENTION_ENABLED=1` 才生效。
可以先 `GET /api/stats/retention`（stats 路由）看一眼「将删除多少行」再打开。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import time
from datetime import date, datetime, timedelta

from sqlalchemy import func

logger = logging.getLogger("admin.retention")

#: 「今天已经跑完」的标记（system_settings.key）
MARKER = "retention:last_run"
#: 两次完整运行之间的最短间隔（小时）
MIN_INTERVAL_HOURS = 20


def _env_int(name: str, default: int, low: int = 0, high: int = 36500) -> int:
    raw = (os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(low, min(high, value))


def enabled() -> bool:
    return (os.getenv("ADMIN_RETENTION_ENABLED", "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def policy() -> dict:
    """当前保留策略（天数为 0 表示该表不清理）。"""
    return {
        "enabled": enabled(),
        "usage_logs_days": _env_int("ADMIN_USAGE_RETENTION_DAYS", 90),
        "request_audits_days": _env_int("ADMIN_AUDIT_RETENTION_DAYS", 30),
        "credit_observed_days": _env_int("ADMIN_CREDIT_OBSERVED_RETENTION_DAYS", 30),
        "batch": _env_int("ADMIN_RETENTION_BATCH", 5000, 100, 100000),
        "budget_s": _env_int("ADMIN_RETENTION_BUDGET_S", 60, 5, 3600),
        "archive_dir": (os.getenv("ADMIN_RETENTION_ARCHIVE_DIR", "") or "").strip(),
    }


def _targets(db, now: datetime, conf: dict) -> list[dict]:
    """每张表的删除条件。usage_logs 的截止点不能越过汇总表的覆盖水位。"""
    from admin.models import CreditEvent, RequestAudit, UsageLog, UsageRollupState

    out = []
    days = conf["usage_logs_days"]
    if days > 0:
        cutoff = now - timedelta(days=days)
        state = db.get(UsageRollupState, 1)
        covered_to = getattr(state, "covered_to", None) if state is not None else None
        if covered_to is None:
            cutoff = None          # 汇总表还没建起来：先不删明细
        elif covered_to < cutoff:
            cutoff = covered_to
        if cutoff is not None:
            out.append({"name": "usage_logs", "model": UsageLog,
                        "where": [UsageLog.created_at < cutoff], "cutoff": cutoff})
    days = conf["request_audits_days"]
    if days > 0:
        cutoff = now - timedelta(days=days)
        out.append({"name": "request_audits", "model": RequestAudit,
                    "where": [RequestAudit.started_at < cutoff], "cutoff": cutoff})
    days = conf["credit_observed_days"]
    if days > 0:
        cutoff = now - timedelta(days=days)
        out.append({"name": "credit_events_observed", "model": CreditEvent,
                    "where": [CreditEvent.kind == "observed", CreditEvent.created_at < cutoff],
                    "cutoff": cutoff})
    return out


def preview(db, now: datetime | None = None) -> dict:
    """只读：按当前策略「将删除多少行」（不受 enabled 开关影响）。"""
    now = now or datetime.utcnow()
    conf = policy()
    items = []
    for target in _targets(db, now, conf):
        model = target["model"]
        count = db.query(func.count(model.id)).filter(*target["where"]).scalar() or 0
        items.append({"table": target["name"], "cutoff": target["cutoff"].isoformat() + "Z",
                      "rows": int(count)})
    return {**{k: v for k, v in conf.items() if k != "archive_dir"},
            "archive": bool(conf["archive_dir"]), "tables": items}


def _row_dict(row) -> dict:
    out = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name, None)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        out[column.name] = value
    return out


def _row_day(row) -> str:
    """行自己的日期（UTC，YYYYMMDD）：按**数据发生的那一天**归档，而不是按清理那天。"""
    for column in ("created_at", "started_at"):
        value = getattr(row, column, None)
        if isinstance(value, (datetime, date)):
            return value.strftime("%Y%m%d")
    return datetime.utcnow().strftime("%Y%m%d")


def _archive(directory: str, name: str, rows) -> None:
    """追加写 `<表名>-<行日期>.jsonl.gz`（同一天的行落在同一个文件，可重复追加）。"""
    os.makedirs(directory, exist_ok=True)
    by_day: dict[str, list] = {}
    for row in rows:
        by_day.setdefault(_row_day(row), []).append(row)
    for day, items in sorted(by_day.items()):
        path = os.path.join(directory, "%s-%s.jsonl.gz" % (name, day))
        with gzip.open(path, "at", encoding="utf-8") as handle:
            for row in items:
                handle.write(json.dumps(_row_dict(row), ensure_ascii=False, default=str))
                handle.write("\n")


def run_once(db, now: datetime | None = None) -> dict:
    """按策略删一轮。返回每张表删了多少行，以及是否已经删完（`complete`）。"""
    now = now or datetime.utcnow()
    conf = policy()
    deadline = time.monotonic() + conf["budget_s"]
    result = {"complete": True, "tables": {}}
    for target in _targets(db, now, conf):
        model = target["model"]
        deleted = 0
        while True:
            if time.monotonic() > deadline:
                result["complete"] = False
                break
            ids = [row[0] for row in db.query(model.id).filter(*target["where"])
                   .order_by(model.id.asc()).limit(conf["batch"]).all()]
            if not ids:
                break
            if conf["archive_dir"]:
                try:
                    _archive(conf["archive_dir"], target["name"],
                             db.query(model).filter(model.id.in_(ids)).all())
                except Exception as exc:  # noqa: BLE001 - 归档失败绝不删
                    logger.warning("数据保留：%s 归档失败，本批不删除：%s", target["name"], exc)
                    db.rollback()
                    result["complete"] = False
                    break
            db.query(model).filter(model.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            deleted += len(ids)
            if len(ids) < conf["batch"]:
                break
            time.sleep(0.05)       # 让出写锁 / CPU，别挡住记账
        result["tables"][target["name"]] = deleted
        if not result["complete"]:
            break
    if any(result["tables"].values()):
        logger.info("数据保留：%s", result["tables"])
        if db.get_bind().dialect.name == "sqlite":
            try:
                db.execute(__import__("sqlalchemy").text("PRAGMA optimize"))
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
    return result


def _last_run(db):
    from admin.models import SystemSetting

    row = db.get(SystemSetting, MARKER)
    try:
        return datetime.strptime(str(row.value), "%Y-%m-%d %H:%M:%S") if row else None
    except (TypeError, ValueError):
        return None


def maybe_run(db=None, now: datetime | None = None) -> dict | None:
    """开关打开且距上次完整运行 ≥ 20 小时才跑；返回 None 表示这次没跑。

    由 `rollup.start_backfill_thread` 的后台线程每 5 分钟调一次。
    """
    if not enabled():
        return None
    own = db is None
    if own:
        from admin.db import SessionLocal

        db = SessionLocal()
    try:
        now = now or datetime.utcnow()
        last = _last_run(db)
        if last is not None and now - last < timedelta(hours=MIN_INTERVAL_HOURS):
            return None
        result = run_once(db, now)
        if result.get("complete"):
            from admin.models import SystemSetting

            row = db.get(SystemSetting, MARKER)
            if row is None:
                row = SystemSetting(key=MARKER)
                db.add(row)
            row.value = now.strftime("%Y-%m-%d %H:%M:%S")
            db.commit()
        return result
    except Exception as exc:  # noqa: BLE001 - 清理失败下一轮再试，不能把后台线程带崩
        logger.warning("数据保留执行失败：%s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    finally:
        if own:
            db.close()
