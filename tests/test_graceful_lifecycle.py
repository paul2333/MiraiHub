#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""优雅退出任务 1/2 的持久回归：准入计数、排空中间件、就绪与关闭顺序。

## 这个文件锁什么

1. **原子准入/在途计数**：阶段判定与加入在途集在同一把短锁内；begin() 在同一把锁里
   封口；租约唯一、finish() 幂等、计数永不为负；唤醒后重验谓词；线程完成通过
   loop.call_soon_threadsafe 唤醒事件循环。
2. **原生 ASGI 排空中间件**：不读请求体、不缓存输出；已准入的流能写完尾帧；排空后新
   业务请求立即 503（Retry-After: 3、Cache-Control: no-store）；协议信封按
   /v1/messages、OpenAI 兼容、/mcp 分别映射；CORS 已启用时 503 仍带 CORS 头。
3. **真实应用装配**：app.user_middleware 里「封禁/IP 防护 → 生命周期 → TimingMiddleware
   → 准入队列」的层序，并用真实优先返回路径（被封 IP 的 429、生产封禁路径的 404、
   503 上没有 TimingMiddleware 的 x-local-request-id）验证。
4. **接口**：GET /api/app/ready 匿名 200/503 + no-store；GET /api/app/lifecycle 需
   require_admin 且快照字段固定、不含本机路径。
5. **豁免表**：逐条有理由、按「方法 + 路径」精确豁免；/v1 全树、POST /mcp、
   /api/agent/*、上传/导入/恢复/导出等业务写请求一律计入在途，排队中的请求也持租约。
6. **脱离请求的工作（任务 2）**：JobRunner 守护线程与定时器每一轮在启动线程/任务**之前**
   取 job 租约，完成/异常/线程启动失败都在同一所有权下释放；复用 running 的 Job 不新增
   计数；封口后不再新派发。子租约先登记、父租约后释放，中间没有归零窗口。
7. **观察流（任务 2）**：两条日志 SSE 在 drain 时主动结束并发 draining 帧；同一时刻已
   准入的业务 SSE 仍能写完尾帧（对照断言，不统一取消所有 task）。
8. **就绪（任务 2）**：/api/app/ready 只在「必要初始化完成 + 数据库可用 + 必要表齐」后开；
   降级启动用有超时/单飞/限频的真实探测补一次初始化；排空期间迟到的探针无权改回 ready。
9. **关闭顺序（任务 2）**：封口 → 在途归零 → 停服务 → 现有关闭钩子；阻塞清理不在事件
   循环里，且只由一处承担。审计/记账用临时 SQLite 从**另一条连接**断言恰好一条。

运行：
  python -m unittest discover -s tests -p test_graceful_lifecycle.py -v

测试只用合成流、内存状态与开发默认密钥下的无会话票据，**不连接任何部署数据库**。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

from admin.lifecycle import (  # noqa: E402
    DRAIN_BUSINESS_CODE,
    DRAIN_EXEMPTIONS,
    DRAIN_MESSAGE,
    PHASE_DRAINING,
    PHASE_FAILED,
    PHASE_READY,
    PHASE_STARTING,
    PHASE_STOPPED,
    PHASE_STOPPING,
    Lifecycle,
    LifecycleConflict,
    LifecycleMiddleware,
    get_lifecycle,
    is_drain_exempt,
    wait_empty as wait_empty_singleton,
)

#: 契约第 1 节固定的快照字段（不得增删）。
SNAPSHOT_FIELDS = {
    "phase", "action", "instance_id", "operation_id", "active_requests",
    "active_jobs", "elapsed_ms", "can_restart", "can_force",
}


# ---------------------------------------------------------------------------
# ASGI 夹具
# ---------------------------------------------------------------------------


def http_scope(method="GET", path="/", *, query="", headers=None,
               client=("127.0.0.1", 51234)):
    """构造一条最小 HTTP scope（原生 ASGI 调用，不经过 TestClient）。"""
    raw_headers = [(b"host", b"testserver")]
    for name, value in (headers or {}).items():
        raw_headers.append((name.lower().encode("latin-1"),
                            str(value).encode("latin-1")))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": raw_headers,
        "client": tuple(client),
        "server": ("127.0.0.1", 8790),
    }


class _Session:
    """一次 ASGI 调用的记录器：收集出站消息，统计入站读取次数。"""

    def __init__(self, *, body=b"", no_request_body=False, fail_final_body=False):
        self.messages = []
        self.receive_calls = 0
        self._pending = [] if no_request_body else [
            {"type": "http.request", "body": body, "more_body": False}]
        self._fail_final_body = fail_final_body

    async def receive(self):
        self.receive_calls += 1
        if self._pending:
            return self._pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(self, message):
        if (self._fail_final_body and message["type"] == "http.response.body"
                and not message.get("more_body", False)
                and any(m["type"] == "http.response.start" for m in self.messages)):
            raise OSError("末帧发送失败（模拟客户端已断开）")
        self.messages.append(message)

    # -- 断言便利属性 ---------------------------------------------------
    @property
    def status(self):
        for message in self.messages:
            if message["type"] == "http.response.start":
                return message["status"]
        return None

    def header(self, name):
        wanted = name.lower().encode("latin-1")
        for message in self.messages:
            if message["type"] != "http.response.start":
                continue
            for key, value in message.get("headers", []):
                if key.lower() == wanted:
                    return value.decode("latin-1")
        return None

    @property
    def headers(self):
        for message in self.messages:
            if message["type"] == "http.response.start":
                return {key.decode("latin-1").lower(): value.decode("latin-1")
                        for key, value in message.get("headers", [])}
        return {}

    @property
    def body(self):
        return b"".join(message.get("body", b"") for message in self.messages
                        if message["type"] == "http.response.body")

    @property
    def finished(self):
        return any(message["type"] == "http.response.body"
                   and not message.get("more_body", False)
                   for message in self.messages)

    def json(self):
        return json.loads(self.body.decode("utf-8"))


async def drive(app, scope, session):
    """把一条 scope 灌进 ASGI 应用，返回同一个 session 记录器。"""
    await app(scope, session.receive, session.send)
    return session


async def eventually(predicate, *, timeout=2.0, what="条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"等待超时：{what}")


class _LatchedStreamApp:
    """最小 ASGI 子应用：两个 Event 分别卡住最后一帧与最后一次清理。"""

    def __init__(self):
        self.first_frame_sent = asyncio.Event()
        self.release_last_frame = asyncio.Event()
        self.last_frame_sent = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.cleaned_up = asyncio.Event()

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream; charset=utf-8"),
                                (b"cache-control", b"no-cache")]})
        await send({"type": "http.response.body", "body": b"data: first\n\n",
                    "more_body": True})
        self.first_frame_sent.set()
        await self.release_last_frame.wait()
        await send({"type": "http.response.body", "body": b"data: last\n\n",
                    "more_body": True})
        self.last_frame_sent.set()
        # 尾帧已经写出，但应用清理（审计落库等）还没完成 —— 租约必须还能持有。
        await self.release_cleanup.wait()
        self.cleaned_up.set()
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class _ProbeApp:
    """记录「被调用那一刻的在途计数」，用来判断某条路径到底有没有被计入。"""

    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.observed = []

    async def __call__(self, scope, receive, send):
        self.observed.append(self.lifecycle.snapshot()["active_requests"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _GatedApp:
    """模拟内层准入队列：进来先等一道闸，闸不开就一直挂着（仍持租约）。"""

    def __init__(self):
        self.gate = asyncio.Event()
        self.entered = 0

    async def __call__(self, scope, receive, send):
        self.entered += 1
        await self.gate.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _FakeClock:
    def __init__(self, start=1000.0):
        self.value = start

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def _stateless_dev_admin_token():
    """开发默认密钥下的无会话票据：不建会话行，因此完全不碰数据库。"""
    from admin import env_guard, security
    from admin.config import settings

    if env_guard.IS_PROD:
        return None
    if not str(getattr(settings, "JWT_SECRET", "")).startswith("dev-insecure"):
        return None
    return security._encode("")


# ---------------------------------------------------------------------------
# 1. Lifecycle 阶段机、计数与幂等
# ---------------------------------------------------------------------------


class LifecycleStateTests(unittest.IsolatedAsyncioTestCase):
    """Lifecycle 本体：准入、计数、封口、强制、重置。"""

    async def test_begin_seals_admission_before_waiting(self):
        state = Lifecycle()
        state.mark_ready()
        first = state.try_admit("request")
        self.assertIsNotNone(first)
        snapshot = state.begin("restart", "op-1")
        self.assertEqual(snapshot["active_requests"], 1)
        self.assertIsNone(state.try_admit("request"))
        waiter = asyncio.create_task(state.wait_empty())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        state.finish(first)
        await asyncio.wait_for(waiter, timeout=1)
        state.finish(first)
        self.assertEqual(state.snapshot()["active_requests"], 0)

    def test_fresh_instance_starts_in_starting_and_refuses_admission(self):
        state = Lifecycle()
        self.assertEqual(state.snapshot()["phase"], PHASE_STARTING)
        self.assertIsNone(state.try_admit("request"))
        self.assertEqual(state.snapshot()["active_requests"], 0)

    def test_snapshot_fields_are_fixed_and_free_of_paths_or_secrets(self):
        state = Lifecycle()
        state.mark_ready()
        state.begin("restart", "op-hygiene")
        snapshot = state.snapshot()
        self.assertEqual(set(snapshot), SNAPSHOT_FIELDS)
        blob = json.dumps(snapshot)
        for forbidden in ("C:", "\\", "/", "Users", "token", "password",
                          "cookie", "secret", "Bearer"):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(snapshot["action"], "restart")
        self.assertEqual(snapshot["operation_id"], "op-hygiene")

    def test_idle_snapshot_has_null_action_and_operation(self):
        state = Lifecycle()
        state.mark_ready()
        snapshot = state.snapshot()
        self.assertIsNone(snapshot["action"])
        self.assertIsNone(snapshot["operation_id"])
        self.assertEqual(snapshot["elapsed_ms"], 0)

    def test_finish_is_idempotent_and_counts_never_go_negative(self):
        state = Lifecycle()
        state.mark_ready()
        first = state.try_admit("request")
        second = state.try_admit("request")
        self.assertNotEqual(first.lease_id, second.lease_id)
        self.assertEqual(state.snapshot()["active_requests"], 2)

        state.finish(first)
        self.assertEqual(state.snapshot()["active_requests"], 1,
                         "释放一个租约后不能错误地归零")
        state.finish(first)
        state.finish(first)
        self.assertEqual(state.snapshot()["active_requests"], 1,
                         "同一租约重复释放不能再减计数")

        state.finish(None)
        state.finish(object())
        self.assertEqual(state.snapshot()["active_requests"], 1,
                         "陌生对象不能改动计数")
        state.finish(second)
        self.assertEqual(state.snapshot()["active_requests"], 0)

    def test_job_and_request_counters_are_separate(self):
        state = Lifecycle()
        state.mark_ready()
        request_lease = state.try_admit("request")
        job_lease = state.try_admit("job")
        snapshot = state.snapshot()
        self.assertEqual((snapshot["active_requests"], snapshot["active_jobs"]), (1, 1))
        state.finish(request_lease)
        self.assertEqual(state.snapshot()["active_requests"], 0)
        self.assertEqual(state.snapshot()["active_jobs"], 1,
                         "请求归零不代表后台任务归零")
        state.finish(job_lease)
        self.assertEqual(state.snapshot()["active_jobs"], 0)

    def test_unknown_kind_is_rejected_loudly(self):
        state = Lifecycle()
        state.mark_ready()
        with self.assertRaises(ValueError):
            state.try_admit("stream")

    def test_begin_same_operation_id_is_idempotent(self):
        clock = _FakeClock()
        state = Lifecycle(clock=clock)
        state.mark_ready()
        lease = state.try_admit("request")
        first = state.begin("restart", "op-same")
        clock.advance(1.5)
        again = state.begin("restart", "op-same")
        self.assertEqual(first["operation_id"], again["operation_id"])
        self.assertEqual(again["phase"], PHASE_DRAINING)
        self.assertEqual(again["active_requests"], 1,
                         "重复 begin 不能重置计数")
        self.assertIsNone(state.try_admit("request"))
        state.finish(lease)

    def test_begin_with_conflicting_intent_raises_with_snapshot(self):
        state = Lifecycle()
        state.mark_ready()
        state.begin("restart", "op-first")
        with self.assertRaises(LifecycleConflict) as caught:
            state.begin("update", "op-second")
        self.assertEqual(caught.exception.snapshot["operation_id"], "op-first")
        with self.assertRaises(LifecycleConflict):
            state.begin("restart", "op-second")
        # 同 operation_id 但动作不同，不算同一操作
        with self.assertRaises(LifecycleConflict):
            state.begin("stop", "op-first")

    def test_begin_rejects_stale_generation(self):
        state = Lifecycle()
        state.mark_ready()
        with self.assertRaises(LifecycleConflict):
            state.begin("restart", "op-stale", instance_id="wb-old-generation")

    def test_two_concurrent_begins_only_one_seals(self):
        state = Lifecycle()
        state.mark_ready()
        results, conflicts = [], []
        barrier = threading.Barrier(6)

        def run(operation_id):
            barrier.wait()
            try:
                results.append(state.begin("restart", operation_id))
            except LifecycleConflict as exc:
                conflicts.append(exc)

        threads = [threading.Thread(target=run, args=(f"op-race-{index}",))
                   for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 1, "并发 begin 只能有一个成功")
        self.assertEqual(len(conflicts), 5)
        self.assertEqual(state.snapshot()["operation_id"],
                         results[0]["operation_id"])
        for conflict in conflicts:
            self.assertEqual(conflict.snapshot["operation_id"],
                             results[0]["operation_id"])

    def test_force_binds_current_generation_and_operation(self):
        state = Lifecycle()
        state.mark_ready()
        lease = state.try_admit("request")
        snapshot = state.begin("restart", "op-force")
        generation = snapshot["instance_id"]
        self.assertTrue(snapshot["can_force"])

        with self.assertRaises(LifecycleConflict):
            state.force("op-force", "wb-other-generation", True)
        with self.assertRaises(LifecycleConflict):
            state.force("op-other", generation, True)
        with self.assertRaises(LifecycleConflict):
            state.force("op-force", generation, False)

        self.assertTrue(state.request_cancel(), "首次请求取消返回 True")
        self.assertFalse(state.request_cancel(), "重复请求取消不再置位")

        forced = state.force("op-force", generation, True)
        self.assertFalse(forced["can_force"], "强制一次后不能反复强制")
        # 同一操作的重复 force 幂等（契约第 3 节：BEGIN/FORCE 对同一操作幂等），
        # 不会再置位一次取消，也不会把 can_force 变回 True。
        again = state.force("op-force", generation, True)
        self.assertEqual(again["operation_id"], "op-force")
        self.assertFalse(again["can_force"])
        state.finish(lease)
        # 换代之后旧操作的 force 必须失效
        state.reset()
        state.mark_ready()
        with self.assertRaises(LifecycleConflict):
            state.force("op-force", generation, True)

    def test_reset_rebuilds_generation_and_clears_previous_drain(self):
        state = Lifecycle()
        state.mark_ready()
        lease = state.try_admit("request")
        before = state.begin("restart", "op-reset")["instance_id"]
        state.mark_stopping()
        state.reset()
        snapshot = state.snapshot()
        self.assertNotEqual(snapshot["instance_id"], before,
                            "每次 lifespan 启动都要重建代号")
        self.assertEqual(snapshot["phase"], PHASE_STARTING)
        self.assertIsNone(snapshot["action"])
        self.assertIsNone(snapshot["operation_id"])
        self.assertEqual((snapshot["active_requests"], snapshot["active_jobs"]), (0, 0))
        state.finish(lease)
        self.assertEqual(state.snapshot()["active_requests"], 0,
                         "换代后旧租约的释放不能把计数减成负数")
        state.mark_ready()
        self.assertEqual(state.snapshot()["phase"], PHASE_READY)
        self.assertIsNotNone(state.try_admit("request"))

    def test_reset_can_pin_an_explicit_instance_id(self):
        state = Lifecycle()
        state.reset(instance_id="wb-pinned-generation")
        self.assertEqual(state.snapshot()["instance_id"], "wb-pinned-generation")

    def test_mark_ready_only_moves_from_starting(self):
        state = Lifecycle()
        state.mark_ready()
        self.assertEqual(state.snapshot()["phase"], PHASE_READY)
        state.begin("restart", "op-late-ready")
        late = state.mark_ready()
        self.assertEqual(late["phase"], PHASE_DRAINING,
                         "排空期间迟到的就绪不能重新打开准入")
        self.assertIsNone(state.try_admit("request"))

    def test_phase_transitions_are_reported(self):
        state = Lifecycle()
        self.assertEqual(state.mark_ready()["phase"], PHASE_READY)
        state.begin("restart", "op-phases")
        self.assertEqual(state.mark_stopping()["phase"], PHASE_STOPPING)
        self.assertFalse(state.snapshot()["can_force"])
        self.assertEqual(state.mark_stopped()["phase"], PHASE_STOPPED)
        failed = state.mark_failed("数据库挂了")
        self.assertEqual(failed["phase"], PHASE_FAILED)
        self.assertNotIn("数据库挂了", json.dumps(failed),
                         "失败原因只进日志，不允许进快照")

    def test_elapsed_ms_uses_monotonic_clock_from_begin(self):
        clock = _FakeClock()
        state = Lifecycle(clock=clock)
        state.mark_ready()
        self.assertEqual(state.snapshot()["elapsed_ms"], 0)
        state.begin("restart", "op-elapsed")
        clock.advance(2.25)
        self.assertEqual(state.snapshot()["elapsed_ms"], 2250)
        clock.advance(0.5)
        self.assertEqual(state.snapshot()["elapsed_ms"], 2750)

    async def test_finish_from_thread_wakes_the_event_loop(self):
        state = Lifecycle()
        state.mark_ready()
        lease = state.try_admit("request")
        state.begin("restart", "op-thread")
        waiter = asyncio.create_task(state.wait_empty())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        thread = threading.Thread(target=state.finish, args=(lease,))
        thread.start()
        thread.join(5)
        await asyncio.wait_for(waiter, timeout=2)

    async def test_wait_empty_returns_immediately_when_idle(self):
        state = Lifecycle()
        await asyncio.wait_for(state.wait_empty(), timeout=1)
        self.assertIs(await wait_empty_singleton(), None)

    def test_process_singleton_is_stable(self):
        self.assertIs(get_lifecycle(), get_lifecycle())

    async def test_last_finish_and_new_admit_race_keeps_counters_exact(self):
        state = Lifecycle()
        for _ in range(30):
            state.reset()
            state.mark_ready()
            lease = state.try_admit("request")
            state.begin("restart", "op-race")
            outcome = {}
            barrier = threading.Barrier(2)

            def finish_last():
                barrier.wait()
                state.finish(lease)

            def admit_new():
                barrier.wait()
                outcome["lease"] = state.try_admit("request")

            threads = [threading.Thread(target=finish_last),
                       threading.Thread(target=admit_new)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertIsNone(outcome["lease"],
                              "已封口时新请求不可能拿到租约（也就不会出现错误的零）")
            snapshot = state.snapshot()
            self.assertEqual(
                (snapshot["active_requests"], snapshot["active_jobs"]), (0, 0))


    def test_resume_reopens_admission_for_uncommitted_operation(self):
        """未提交的 hold（升级预排空）撤销后，旧代必须能恢复接单。"""
        state = Lifecycle()
        state.mark_ready()
        generation = state.snapshot()["instance_id"]
        state.begin("upgrade_hold", "op-hold")
        self.assertIsNone(state.try_admit("request"), "封口后不能接单")

        resumed = state.resume("op-hold")
        self.assertEqual(resumed["phase"], PHASE_READY)
        self.assertIsNone(resumed["action"], "恢复后不再有进行中的操作")
        self.assertIsNone(resumed["operation_id"])
        self.assertEqual(resumed["instance_id"], generation, "恢复不换代")
        self.assertEqual(resumed["elapsed_ms"], 0)
        lease = state.try_admit("request")
        self.assertIsNotNone(lease, "恢复后必须重新打开准入")
        state.finish(lease)

        # 已经没有进行中的操作：同一 operation_id 的重试是幂等返回（见
        # test_resume_is_idempotent_after_success），不同 operation_id 才冲突
        again = state.resume("op-hold")
        self.assertEqual(again["phase"], PHASE_READY)
        with self.assertRaises(LifecycleConflict):
            state.resume("op-invented")

    def test_resume_is_idempotent_after_success(self):
        """CLI 重试：同一 operation_id 的重复 RESUME 不报错、不改变状态。"""
        state = Lifecycle()
        state.mark_ready()
        state.begin("upgrade_hold", "op-hold")
        first = state.resume("op-hold")
        self.assertEqual(first["phase"], PHASE_READY)
        again = state.resume("op-hold")
        self.assertEqual(again["phase"], PHASE_READY)
        self.assertIsNone(again["operation_id"])
        self.assertEqual(again["instance_id"], first["instance_id"])

        with self.assertRaises(LifecycleConflict):
            state.resume("op-other")

        # 新一轮操作开始后，旧的回执不再生效
        state.begin("restart", "op-hold")
        state.mark_stopping()
        with self.assertRaises(LifecycleConflict):
            state.resume("op-hold")

    def test_resume_rejects_committed_stop_and_foreign_generation(self):
        state = Lifecycle()
        state.mark_ready()
        state.begin("upgrade_hold", "op-hold")
        with self.assertRaises(LifecycleConflict):
            state.resume("op-other")
        with self.assertRaises(LifecycleConflict):
            state.resume("op-hold", instance_id="wb-old-generation")

        state.mark_stopping()          # 提交停止之后禁用回退
        with self.assertRaises(LifecycleConflict) as caught:
            state.resume("op-hold")
        self.assertEqual(caught.exception.snapshot["phase"], PHASE_STOPPING)
        self.assertIsNone(state.try_admit("request"),
                          "已提交停止的实例不能再接单")

    def test_reset_adopts_generation_from_environment(self):
        """父进程为每次启动准备代号：reset() 要采纳，避免父子对不上代。"""
        state = Lifecycle()
        with mock.patch.dict(os.environ, {"WB_LIFECYCLE_INSTANCE_ID": "wb-parent-generation"}):
            state.reset()
        self.assertEqual(state.snapshot()["instance_id"], "wb-parent-generation")

        # 显式参数优先于环境变量
        with mock.patch.dict(os.environ, {"WB_LIFECYCLE_INSTANCE_ID": "wb-parent-generation"}):
            state.reset(instance_id="wb-explicit")
        self.assertEqual(state.snapshot()["instance_id"], "wb-explicit")

        # 空值/未设置时自己生成，且每次不同
        with mock.patch.dict(os.environ, {"WB_LIFECYCLE_INSTANCE_ID": ""}):
            state.reset()
            first = state.snapshot()["instance_id"]
            state.reset()
        self.assertTrue(first)
        self.assertNotEqual(first, state.snapshot()["instance_id"])


# ---------------------------------------------------------------------------
# 2. 排空中间件：已准入的流写完尾帧，新业务立即 503
# ---------------------------------------------------------------------------


class DrainingMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    """原生 ASGI 中间件的准入边界与协议信封。"""

    async def test_first_frame_then_drain_then_tail_frame(self):
        state = Lifecycle()
        state.mark_ready()
        app = _LatchedStreamApp()
        middleware = LifecycleMiddleware(app, lifecycle=state)
        stream = _Session()
        task = asyncio.create_task(
            drive(middleware, http_scope("GET", "/api/agent/chat/stream"), stream))
        await asyncio.wait_for(app.first_frame_sent.wait(), 1)

        # 首帧已经到客户端：中间件没有攒包/缓存输出。
        self.assertTrue(stream.body.startswith(b"data: first"), stream.body[:40])
        self.assertFalse(task.done())
        self.assertEqual(state.snapshot()["active_requests"], 1)

        snapshot = state.begin("restart", "op-stream")
        self.assertEqual(snapshot["active_requests"], 1)

        # 排空后第一个新业务请求：立即 503，且不读请求体。
        rejected = _Session(body=b'{"model": "x"}')
        await drive(middleware,
                    http_scope("POST", "/v1/chat/completions",
                               headers={"content-type": "application/json"}),
                    rejected)
        self.assertEqual(rejected.status, 503)
        self.assertEqual(rejected.header("retry-after"), "3")
        self.assertEqual(rejected.header("cache-control"), "no-store")
        self.assertEqual(rejected.json()["error"]["type"], DRAIN_BUSINESS_CODE)
        self.assertEqual(rejected.json()["error"]["code"], DRAIN_BUSINESS_CODE)
        self.assertEqual(rejected.json()["error"]["message"], DRAIN_MESSAGE)
        self.assertEqual(rejected.receive_calls, 0, "排空拒绝不得读取请求体")

        # 放行第一道闩：尾帧可以送达，但清理闩仍卡住 → 计数不能归零。
        app.release_last_frame.set()
        await eventually(lambda: b"data: last" in stream.body, what="尾帧送达")
        self.assertFalse(app.cleaned_up.is_set())
        self.assertEqual(state.snapshot()["active_requests"], 1)
        self.assertFalse(task.done())

        # 放行清理，计数才允许归零。
        app.release_cleanup.set()
        await asyncio.wait_for(task, 2)
        self.assertTrue(app.cleaned_up.is_set())
        self.assertEqual(state.snapshot()["active_requests"], 0)
        self.assertTrue(stream.finished, "最后必须送出终止帧")
        self.assertEqual([m["body"] for m in stream.messages
                          if m["type"] == "http.response.body"],
                         [b"data: first\n\n", b"data: last\n\n", b""],
                         "已准入的流不能被排空截断或改写")
        self.assertEqual(stream.receive_calls, 0, "中间件不读请求体")

    async def test_draining_envelopes_follow_each_protocol(self):
        state = Lifecycle()
        state.mark_ready()
        state.begin("restart", "op-envelopes")
        middleware = LifecycleMiddleware(_ProbeApp(state), lifecycle=state)

        for path in ("/v1/messages", "/v1/messages/count_tokens"):
            session = await drive(middleware, http_scope("POST", path),
                                  _Session(body=b"{}"))
            self.assertEqual(session.status, 503)
            body = session.json()
            self.assertEqual(body["type"], "error")
            self.assertEqual(body["error"]["type"], DRAIN_BUSINESS_CODE)
            self.assertTrue(body["error"]["message"])
            self.assertEqual(session.header("retry-after"), "3")

        for path in ("/v1/chat/completions", "/v1/completions", "/v1/responses"):
            session = await drive(middleware, http_scope("POST", path),
                                  _Session(body=b"{}"))
            self.assertEqual(session.status, 503)
            body = session.json()
            self.assertEqual(body["error"]["type"], DRAIN_BUSINESS_CODE)
            self.assertEqual(body["error"]["code"], DRAIN_BUSINESS_CODE)
            self.assertTrue(body["error"]["message"])

        session = await drive(middleware, http_scope("POST", "/mcp"),
                              _Session(body=b"{}"))
        self.assertEqual(session.status, 503)
        self.assertEqual(session.json(), {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32000, "message": DRAIN_BUSINESS_CODE}})

        session = await drive(middleware, http_scope("POST", "/api/agent/chat"),
                              _Session())
        self.assertEqual(session.status, 503)
        self.assertEqual(session.json()["error"]["type"], DRAIN_BUSINESS_CODE)

    async def test_draining_response_keeps_cors_headers_when_enabled(self):
        from admin.config import settings

        saved = list(settings.CORS_ORIGINS)
        try:
            settings.CORS_ORIGINS = ["*"]
            state = Lifecycle()
            state.mark_ready()
            state.begin("restart", "op-cors")
            middleware = LifecycleMiddleware(_ProbeApp(state), lifecycle=state)
            session = await drive(
                middleware,
                http_scope("POST", "/v1/chat/completions",
                           headers={"origin": "https://console.example"}),
                _Session())
            self.assertEqual(session.status, 503)
            self.assertEqual(session.header("access-control-allow-origin"), "*")

            settings.CORS_ORIGINS = ["https://console.example"]
            session = await drive(
                middleware,
                http_scope("POST", "/v1/chat/completions",
                           headers={"origin": "https://console.example"}),
                _Session())
            self.assertEqual(session.header("access-control-allow-origin"),
                             "https://console.example")
            self.assertEqual(session.header("vary"), "Origin")

            session = await drive(
                middleware,
                http_scope("POST", "/v1/chat/completions",
                           headers={"origin": "https://evil.example"}),
                _Session())
            self.assertIsNone(session.header("access-control-allow-origin"),
                              "未放行的来源不能拿到 CORS 头")

            session = await drive(
                middleware, http_scope("POST", "/v1/chat/completions"), _Session())
            self.assertIsNone(session.header("access-control-allow-origin"))
        finally:
            settings.CORS_ORIGINS = saved

    async def test_receive_and_send_are_forwarded_untouched(self):
        state = Lifecycle()
        state.mark_ready()
        seen = {}

        async def app(scope, receive, send):
            seen["receive"] = receive
            seen["send"] = send
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = LifecycleMiddleware(app, lifecycle=state)
        session = _Session()
        receive, send = session.receive, session.send
        await middleware(http_scope("POST", "/api/agent/chat"), receive, send)
        self.assertIs(seen["receive"], receive)
        self.assertIs(seen["send"], send,
                      "中间件不能包一层 send（否则等于缓存/改写输出）")

    async def test_non_http_scope_passes_through(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])

        state = Lifecycle()      # 连 starting 都不放行 HTTP，但非 HTTP 必须透传
        middleware = LifecycleMiddleware(app, lifecycle=state)
        await middleware({"type": "lifespan"}, None, None)
        await middleware({"type": "websocket", "path": "/ws"}, None, None)
        self.assertEqual(seen, ["lifespan", "websocket"])

    async def test_business_paths_take_a_lease_and_exempt_paths_do_not(self):
        state = Lifecycle()
        state.mark_ready()
        probe = _ProbeApp(state)
        middleware = LifecycleMiddleware(probe, lifecycle=state)

        business = [
            ("POST", "/v1/messages"),
            ("GET", "/v1/models"),
            ("POST", "/v1/embeddings"),
            ("POST", "/mcp"),
            ("POST", "/api/agent/chat"),
            ("POST", "/api/accounts/import"),
            ("POST", "/api/maintenance/restore"),
            ("POST", "/api/accounts"),
            ("GET", "/api/stats/overview"),
            ("GET", "/api/logs/requests"),
            ("POST", "/api/join/start"),
        ]
        for method, path in business:
            probe.observed.clear()
            await drive(middleware, http_scope(method, path), _Session())
            self.assertEqual(probe.observed, [1], f"{method} {path} 必须计入在途")
            self.assertEqual(state.snapshot()["active_requests"], 0,
                             f"{method} {path} 结束后必须释放")

        exempt = [
            ("GET", "/api/app/ready"),
            ("GET", "/api/app/version"),
            ("GET", "/api/app/desktop-ready"),
            ("GET", "/api/app/lifecycle"),
            ("GET", "/api/login/session"),
            ("GET", "/api/accounts/probe"),
            ("GET", "/api/logs/requests/stream"),
            ("GET", "/api/logs/usage/stream"),
            ("POST", "/api/maintenance/restart"),
            ("POST", "/api/maintenance/restart/force"),
            ("GET", "/admin"),
            ("GET", "/admin/assets/console-app.js"),
            ("GET", "/static/login.html"),
        ]
        for method, path in exempt:
            probe.observed.clear()
            await drive(middleware, http_scope(method, path), _Session())
            self.assertEqual(probe.observed, [0],
                             f"{method} {path} 必须豁免（否则排空会被自己卡住）")

    def test_exemption_table_is_method_and_path_specific_with_reasons(self):
        self.assertTrue(DRAIN_EXEMPTIONS)
        for exemption in DRAIN_EXEMPTIONS:
            self.assertTrue(exemption.reason.strip(),
                            f"{exemption.path} 缺豁免理由")
            self.assertTrue(exemption.methods, f"{exemption.path} 没有方法约束")

        # 只看路径不看方法的"一概排除"是不允许的：同一路径换个方法必须计入。
        self.assertTrue(is_drain_exempt("/api/app/ready", "GET"))
        self.assertFalse(is_drain_exempt("/api/app/ready", "POST"))
        self.assertTrue(is_drain_exempt("/api/app/lifecycle", "HEAD"))
        self.assertFalse(is_drain_exempt("/api/app/lifecycle", "DELETE"))
        self.assertTrue(is_drain_exempt("/api/logs/usage/stream", "GET"))
        self.assertFalse(is_drain_exempt("/api/logs/usage/stream", "POST"))
        self.assertFalse(is_drain_exempt("/api/logs/usage", "GET"))
        self.assertTrue(is_drain_exempt("/api/maintenance/restart", "POST"))
        self.assertFalse(is_drain_exempt("/api/maintenance/restart", "GET"))
        self.assertTrue(is_drain_exempt("/admin", "GET"))
        self.assertFalse(is_drain_exempt("/admin", "POST"))
        self.assertTrue(is_drain_exempt("/admin/assets/console-app.js", "GET"))
        # Starlette 默认 redirect_slashes：尾斜杠变体也放行（跳转本身很便宜）
        self.assertTrue(is_drain_exempt("/api/app/ready/", "GET"))
        self.assertFalse(is_drain_exempt("/api/app/ready/", "PUT"))
        self.assertTrue(is_drain_exempt("/v1/chat/completions/", "OPTIONS"))
        # 业务写操作与 /v1 全树一律计入
        for path in ("/v1/messages", "/v1/chat/completions", "/mcp",
                     "/api/agent/chat", "/api/maintenance/restore",
                     "/api/accounts/import", "/api/app-source/tree"):
            self.assertFalse(is_drain_exempt(path, "POST"), path)
        # CORS 预检不触发业务，交给内层 CORSMiddleware 应答
        self.assertTrue(is_drain_exempt("/v1/chat/completions", "OPTIONS"))

    async def test_queued_requests_hold_leases_until_they_finish(self):
        state = Lifecycle()
        state.mark_ready()
        gated = _GatedApp()
        middleware = LifecycleMiddleware(gated, lifecycle=state)

        first = asyncio.create_task(
            drive(middleware, http_scope("POST", "/v1/chat/completions"), _Session()))
        second = asyncio.create_task(
            drive(middleware, http_scope("POST", "/v1/chat/completions"), _Session()))
        await eventually(lambda: gated.entered == 2, what="两条请求都进入内层排队")
        self.assertEqual(state.snapshot()["active_requests"], 2,
                         "排队中的请求也持有租约")

        state.begin("restart", "op-queued")
        self.assertEqual(state.snapshot()["active_requests"], 2)
        gated.gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(state.snapshot()["active_requests"], 0)

    async def test_downstream_exception_releases_lease_once(self):
        state = Lifecycle()
        state.mark_ready()

        async def app(scope, receive, send):
            raise RuntimeError("下游炸了")

        middleware = LifecycleMiddleware(app, lifecycle=state)
        with self.assertRaises(RuntimeError):
            await drive(middleware, http_scope("POST", "/api/agent/chat"), _Session())
        self.assertEqual(state.snapshot()["active_requests"], 0)

    async def test_final_frame_send_failure_releases_lease_once(self):
        state = Lifecycle()
        state.mark_ready()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"data: last\n\n",
                        "more_body": False})

        middleware = LifecycleMiddleware(app, lifecycle=state)
        with self.assertRaises(OSError):
            await drive(middleware, http_scope("GET", "/api/agent/chat/stream"),
                        _Session(fail_final_body=True))
        self.assertEqual(state.snapshot()["active_requests"], 0)

    async def test_client_disconnect_releases_lease_once(self):
        state = Lifecycle()
        state.mark_ready()
        disconnected = {"count": 0}

        async def app(scope, receive, send):
            message = await receive()
            self.assertEqual(message["type"], "http.disconnect")
            disconnected["count"] += 1
            raise ConnectionResetError("客户端已断开")

        middleware = LifecycleMiddleware(app, lifecycle=state)
        with self.assertRaises(ConnectionResetError):
            await drive(middleware, http_scope("GET", "/api/agent/chat/stream"),
                        _Session(no_request_body=True))
        self.assertEqual(disconnected["count"], 1)
        self.assertEqual(state.snapshot()["active_requests"], 0)

    async def test_cancelled_before_slot_releases_lease_once(self):
        state = Lifecycle()
        state.mark_ready()
        gated = _GatedApp()
        middleware = LifecycleMiddleware(gated, lifecycle=state)
        task = asyncio.create_task(
            drive(middleware, http_scope("POST", "/v1/chat/completions"), _Session()))
        await eventually(lambda: gated.entered == 1, what="请求进入内层排队")
        self.assertEqual(state.snapshot()["active_requests"], 1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(state.snapshot()["active_requests"], 0,
                         "还没拿到限流槽就被取消，也必须释放租约")

    async def test_no_protocol_tail_is_written_for_rejected_stream(self):
        state = Lifecycle()
        state.mark_ready()
        state.begin("restart", "op-reject-stream")
        middleware = LifecycleMiddleware(_LatchedStreamApp(), lifecycle=state)
        session = await drive(
            middleware,
            http_scope("POST", "/v1/messages",
                       headers={"content-type": "application/json"}),
            _Session(body=b"{}"))
        self.assertEqual(session.status, 503)
        self.assertNotIn(b"data: ", session.body,
                         "未准入的请求不能写出任何协议帧")


# ---------------------------------------------------------------------------
# 3. 真实应用：层序、探针、生产封禁与 CORS
# ---------------------------------------------------------------------------


class RealAppWiringTests(unittest.TestCase):
    """admin/server.py 里中间件注册顺序（外层在前）。"""

    @classmethod
    def setUpClass(cls):
        import admin.server as server
        cls.names = [entry.cls.__name__ for entry in server.app.user_middleware]

    def test_ban_guard_lifecycle_timing_admission_order(self):
        names = self.names
        self.assertEqual(names[0], "AbuseGuardMiddleware",
                         "最外层必须仍是不分路径的封禁守卫")
        self.assertLess(names.index("IPBlockMiddleware"),
                        names.index("LifecycleMiddleware"))
        self.assertLess(names.index("LifecycleMiddleware"),
                        names.index("TimingMiddleware"))
        self.assertLess(names.index("TimingMiddleware"),
                        names.index("GatewayAdmissionMiddleware"),
                        "生命周期必须在原准入队列之外")
        self.assertLess(names.index("TimingMiddleware"),
                        names.index("JoinAdmissionMiddleware"))


class RealAppHttpTests(unittest.IsolatedAsyncioTestCase):
    """用真实 app 的中间件栈验证探针、封禁、生产 404 与 CORS。"""

    @classmethod
    def setUpClass(cls):
        import admin.server as server
        from admin import lifecycle as lifecycle_module
        cls.app = server.app
        cls.lifecycle = lifecycle_module.get_lifecycle()

    def setUp(self):
        import admin.abuse_guard as abuse_guard
        import admin.env_guard as env_guard
        self.abuse_guard = abuse_guard
        self.env_guard = env_guard
        self._saved_prod = env_guard.IS_PROD
        self.lifecycle.reset()
        self.lifecycle.mark_ready()

    def tearDown(self):
        self.lifecycle.reset()
        self.env_guard.IS_PROD = self._saved_prod
        self.abuse_guard.reset("203.0.113.7")

    async def call(self, method, path, *, headers=None, body=b"", session=None):
        session = session or _Session(body=body)
        await self.app(http_scope(method, path, headers=headers), session.receive,
                       session.send)
        return session

    async def test_ready_probe_is_anonymous_and_flips_with_phase(self):
        session = await self.call("GET", "/api/app/ready")
        self.assertEqual(session.status, 200)
        self.assertEqual(session.json(), {"ready": True})
        self.assertEqual(session.header("cache-control"), "no-store")
        self.assertEqual(session.receive_calls, 0)

        self.lifecycle.begin("restart", "op-ready")
        session = await self.call("GET", "/api/app/ready")
        self.assertEqual(session.status, 503)
        self.assertEqual(session.json(), {"ready": False})
        self.assertEqual(session.header("cache-control"), "no-store")

    async def test_lifecycle_snapshot_requires_admin_and_stays_anonymous_safe(self):
        session = await self.call("GET", "/api/app/lifecycle")
        self.assertEqual(session.status, 401)

        token = _stateless_dev_admin_token()
        if token is None:
            self.skipTest("当前环境不是开发默认密钥，无法使用无会话票据")
        session = await self.call("GET", "/api/app/lifecycle",
                                  headers={"x-admin-token": token})
        self.assertEqual(session.status, 200)
        snapshot = session.json()
        self.assertEqual(set(snapshot), SNAPSHOT_FIELDS)
        self.assertEqual(snapshot["phase"], PHASE_READY)
        blob = session.body.decode("utf-8")
        for forbidden in ("C:", "Users", "token", "password", "cookie"):
            self.assertNotIn(forbidden, blob)

    async def test_banned_client_gets_old_ban_response_before_draining_reject(self):
        banned_ip = "203.0.113.7"
        with self.abuse_guard._lock:
            self.abuse_guard._banned[banned_ip] = (time.time() + 60, 1)
        self.lifecycle.begin("restart", "op-ban")
        session = _Session(body=b"{}")
        await self.app(
            http_scope("POST", "/api/agent/chat", client=(banned_ip, 50001)),
            session.receive, session.send)
        self.assertEqual(session.status, 429,
                         "封禁守卫在生命周期之外：被封 IP 仍拿到原来的 429 而不是 503")
        self.assertIn("retry-after", session.headers)

    async def test_prod_blocked_path_still_returns_404_while_draining(self):
        self.env_guard.IS_PROD = True
        self.lifecycle.begin("restart", "op-prod")
        session = await self.call("POST", "/api/maintenance/restart",
                                  body=b'{"confirm": true}')
        self.assertEqual(session.status, 404)
        self.assertEqual(session.json(), {"detail": "Not Found"})
        self.assertEqual(session.receive_calls, 0)

    async def test_draining_reject_does_not_enter_timing_middleware(self):
        from admin.config import settings
        saved = list(settings.CORS_ORIGINS)
        try:
            settings.CORS_ORIGINS = ["*"]
            self.lifecycle.begin("restart", "op-timing")
            session = await self.call(
                "POST", "/v1/chat/completions", body=b'{"model": "x"}',
                headers={"origin": "https://console.example"})
            self.assertEqual(session.status, 503)
            self.assertEqual(session.header("retry-after"), "3")
            self.assertEqual(session.header("cache-control"), "no-store")
            self.assertIsNone(session.header("x-local-request-id"),
                              "503 由生命周期生成，不能经过 TimingMiddleware")
            self.assertEqual(session.header("access-control-allow-origin"), "*",
                             "已启用 CORS 时 503 仍要带 CORS 头")
            self.assertEqual(session.header("connection"), "close",
                             "请求体没读完就回答：必须显式声明关闭连接")
            self.assertEqual(session.receive_calls, 0)
        finally:
            settings.CORS_ORIGINS = saved

    async def test_shell_probes_stay_reachable_while_draining(self):
        self.lifecycle.begin("restart", "op-shell")
        version = await self.call("GET", "/api/app/version")
        self.assertEqual(version.status, 200)
        self.assertIn("version", version.json())

        session_probe = await self.call("GET", "/api/login/session")
        self.assertEqual(session_probe.status, 401,
                         "会话核验仍由原鉴权回答，不是排空 503")

        accounts_probe = await self.call("GET", "/api/accounts/probe")
        self.assertEqual(accounts_probe.status, 401)

    async def test_desktop_ready_probe_keeps_nonce_check_while_draining(self):
        """T1 只锁自己拥有的两件事：排空期间这条探针仍可达；错误 nonce 仍 404。

        响应体里 ready / instance_id 的**内容语义**属于提交 desktop-ready 的任务
        （任务 2/3）：T1 不在这里锁它，只做能力探测 —— 出现了 instance_id 就核对
        它等于当前代，出现了 ready 就核对它是布尔值，其余留给那边的回归。
        """
        nonce = "test-desktop-boot-nonce"
        with mock.patch.dict(os.environ, {"WB_DESKTOP_BOOT": nonce}):
            ready_session = await self.call("GET", "/api/app/desktop-ready",
                                            headers={"x-desktop-boot": nonce})
            self.assertEqual(ready_session.status, 200)
            self.assert_desktop_ready_shape(ready_session.json())

            self.lifecycle.begin("restart", "op-desktop")
            # 排空期间的关键事实只有一个：豁免表让它仍然可达，没有变成 503。
            draining_session = await self.call("GET", "/api/app/desktop-ready",
                                               headers={"x-desktop-boot": nonce})
            self.assertEqual(draining_session.status, 200,
                             "排空期间 desktop-ready 仍应可达（豁免表）")
            self.assert_desktop_ready_shape(draining_session.json())

            bad = await self.call("GET", "/api/app/desktop-ready",
                                  headers={"x-desktop-boot": "wrong-nonce"})
            self.assertEqual(bad.status, 404, "nonce 校验仍由原路由执行")

    def assert_desktop_ready_shape(self, body):
        """能力探测：只核对 T1 能担保的部分，不锁 desktop-ready 的语义。"""
        self.assertIsInstance(body, dict)
        if "instance_id" in body:
            self.assertEqual(body["instance_id"], self.lifecycle.instance_id)
        if "ready" in body:
            self.assertIsInstance(body["ready"], bool)


# ---------------------------------------------------------------------------
# 3b. 启动接线：_startup 真的换代并发布 ready；TestClient 走完整 lifespan
# ---------------------------------------------------------------------------


class _NoDatabase:
    """SessionLocal 的替身：任何会话尝试都立刻失败（调用点都有 try/except 兜底）。"""

    def __call__(self, *args, **kwargs):
        raise RuntimeError("启动接线测试里不连数据库")


class _StubWB:
    """wb_install.WB 的替身：不扫描真实安装目录。"""

    @staticmethod
    def describe():
        return "stub"

    @staticmethod
    def is_version_dynamic():
        return False

    @staticmethod
    def source_dir():
        return ""


async def _noop_async(*args, **kwargs):
    return None


@contextlib.contextmanager
def _isolated_startup(server_module):
    """把 startup 钩子的外部依赖换成桩：不连库、不起后台线程、不扫系统证书。

    这些依赖在真实运行里都自带 try/except，但测试里让它们真的去连数据库、起
    后台线程（调度器/回填/GC）、扫一遍 Windows 证书库，只会又慢又脏，还可能
    碰到部署库。这里统一替换掉，只保留「换代 + 发布 ready」这段真实接线。
    """
    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(mock.patch.object(server_module, "wait_database_ready",
                                return_value=True))
        enter(mock.patch.object(server_module, "init_db", return_value=True))
        # 就绪闸门要求「必要表齐」：桩掉表校验，避免测试连部署库（默认 MySQL:3306），
        # 同时让 `_publish_readiness` 真的走到发布那一步。
        enter(mock.patch.object(server_module, "_required_tables_present",
                                return_value=(True, "")))
        enter(mock.patch.object(server_module, "SessionLocal", _NoDatabase()))
        enter(mock.patch.object(server_module, "_init_after_database_ready",
                                lambda: {"steps": []}))
        enter(mock.patch.object(server_module, "_log_client_version",
                                lambda: None))
        enter(mock.patch.object(server_module, "start_degraded_readiness_probe",
                                lambda **kwargs: None))
        enter(mock.patch("admin.wb_login.start_gc_thread", lambda: None))
        enter(mock.patch("admin.rollup.start_backfill_thread", lambda: None))
        enter(mock.patch("admin.runtime_settings.apply_overrides", lambda db: 0))
        enter(mock.patch("admin.platform.install_paths.warmup", lambda: None))
        enter(mock.patch("admin.wb_paths.load_into_wb", lambda: {}))
        enter(mock.patch("wb_install.WB", _StubWB))
        enter(mock.patch("admin.scheduler.start_scheduler", lambda: None))
        enter(mock.patch("admin.post_login.resume", lambda: None))
        enter(mock.patch("admin.upstream_http.install_ssl_defaults", lambda: None))
        enter(mock.patch("admin.upstream_http.warm_ssl_context", lambda: None))
        enter(mock.patch("admin.upstream_http.stop", _noop_async))
        enter(mock.patch("admin.loop_watch.start", lambda app: True))
        enter(mock.patch("admin.loop_watch.stop", _noop_async))
        enter(mock.patch("admin.ip_activity.flush", lambda: None))
        yield


class LifespanWiringTests(unittest.TestCase):
    """启动接线回归（t8 F1）：启动真的换代、真的发布 ready。

    它锁的是「on_startup 被摘掉就变红」这件事：直接调用 _startup() 要看代号的
    变化，TestClient 走完整 lifespan 要看业务路径不再被排空 503 拦住。
    """

    @classmethod
    def setUpClass(cls):
        import admin.server as server
        cls.server = server
        cls.lifecycle = get_lifecycle()

    def setUp(self):
        self.lifecycle.reset()

    def tearDown(self):
        self.lifecycle.reset()

    def test_startup_switches_generation_and_publishes_ready(self):
        before = self.lifecycle.snapshot()
        self.assertEqual(before["phase"], PHASE_STARTING)

        with _isolated_startup(self.server):
            self.server._startup()

        after = self.lifecycle.snapshot()
        self.assertEqual(after["phase"], PHASE_READY,
                         "启动钩子跑完必须把就绪发布出去")
        self.assertNotEqual(after["instance_id"], before["instance_id"],
                            "每次启动都要换代：否则同进程重建 app 会继承上次的 draining")

        # 再跑一次就是再换代（同进程重建 app / uvicorn --reload 复用进程）。
        with _isolated_startup(self.server):
            self.server._startup()
        again = self.lifecycle.snapshot()
        self.assertEqual(again["phase"], PHASE_READY)
        self.assertNotEqual(again["instance_id"], after["instance_id"])

    def test_lifespan_via_testclient_serves_business_paths(self):
        from fastapi.testclient import TestClient

        with _isolated_startup(self.server):
            # 只有进入 with 上下文时 TestClient 才会执行 lifespan（startup/shutdown）。
            with TestClient(self.server.app) as client:
                ready = client.get("/api/app/ready")
                self.assertEqual(ready.status_code, 200, ready.text)
                self.assertEqual(ready.json(), {"ready": True})
                self.assertEqual(ready.headers.get("cache-control"), "no-store")

                business = client.get("/api/accounts")
                self.assertNotEqual(
                    business.status_code, 503,
                    f"lifespan 之后业务路径不能是排空 503：{business.text}")
                self.assertEqual(business.status_code, 401,
                                 "没有凭据时仍是原来的 401")


# ---------------------------------------------------------------------------
# 3c. 连接语义：排空拒绝在读完请求体之前发生，同一连接不能被污染
# ---------------------------------------------------------------------------


class _SilentApp:
    """从不发送响应的下游：排空拒绝发生在它之前，它根本不该被调用。"""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True


def _read_http_response(sock):
    """读到响应头 + Content-Length 指定的正文为止（raw socket 用）。"""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = sock.recv(4096)
        if not chunk:
            return b"", b""
        raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return head, body


class DrainingConnectionTests(unittest.TestCase):
    """真实 uvicorn + raw socket：排空 503 之后同一连接不会冒出 400。

    场景与真实流式客户端一致：上传用 chunked 分块发送，服务端封口后立刻拒绝，
    客户端不知情地继续补发正文并下一条请求。要求这些字节被丢弃（连接关闭），
    而不是被解析成「非法请求 400」或第二条业务响应。
    """

    def test_chunked_body_after_reject_never_becomes_400(self):
        import uvicorn

        state = Lifecycle()
        state.mark_ready()
        state.begin("restart", "op-socket")
        downstream = _SilentApp()
        app = LifecycleMiddleware(downstream, lifecycle=state)

        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="error",
            lifespan="off", access_log=False))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started, "uvicorn 没能在 10 秒内起来")
            port = server.servers[0].sockets[0].getsockname()[1]

            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            self.addCleanup(sock.close)

            # 只发请求头：正文（chunked）还没发，服务端就已经拒绝。
            sock.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Transfer-Encoding: chunked\r\n"
                b"Content-Type: application/json\r\n\r\n")
            head, body = _read_http_response(sock)
            self.assertIn(b"503", head.split(b"\r\n", 1)[0], head[:80])
            lowered = head.lower()
            self.assertIn(b"retry-after: 3", lowered)
            self.assertIn(b"cache-control: no-store", lowered)
            self.assertIn(b"connection: close",
                          lowered, "请求体没读完就必须显式声明关闭连接")
            self.assertIn(DRAIN_BUSINESS_CODE.encode(), body)
            self.assertFalse(downstream.called,
                             "排空拒绝不能打穿到下游应用")

            # 客户端继续补发正文，并顺手 pipeline 一条完整请求。
            late = (b"5\r\nhello\r\n0\r\n\r\n"
                    b"GET /api/app/ready HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            try:
                sock.sendall(late)
            except OSError:
                pass          # 服务端已经关连接（RST）：这正是期望的结果

            sock.settimeout(1.0)
            leftover = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    leftover += chunk
            except (OSError, socket.timeout):
                pass

            flat = leftover.replace(b"\r\n", b" ")
            self.assertNotIn(b" 400 ", flat,
                             f"同一连接上的后续字节被当成非法请求：{leftover[:120]!r}")
            self.assertNotIn(b"HTTP/1.1 200", leftover,
                             "补发的字节不能被当成一条成功的新请求")
        finally:
            server.should_exit = True
            thread.join(5)


# ---------------------------------------------------------------------------
# 4. 任务 2：脱离请求的工作、观察流、就绪发布与关闭顺序
# ---------------------------------------------------------------------------


def _singleton_lifecycle():
    """服务自己用的生命周期单例（测试里先 reset 再按需发布就绪）。"""
    return get_lifecycle()


def _wait_until(predicate, timeout=2.0, what="条件"):
    """同步等待条件成立（线程场景用；异步场景用 eventually）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("等待超时：" + what)


class _TempSqlite:
    """临时 SQLite 夹具：独立目录 + 独立引擎，**绝不连接部署数据库**。

    退出时释放引擎并删掉目录；持久保留的只有本测试文件本身。
    """

    def __init__(self, *, create_tables=True):
        self.create_tables = create_tables

    def __enter__(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from admin.db import Base

        import admin.models  # noqa: F401  注册全部表定义

        self.tmpdir = tempfile.mkdtemp(prefix="wb-t5-")
        self.path = Path(self.tmpdir) / "audit.db"
        self.engine = create_engine("sqlite:///" + self.path.as_posix(), future=True)
        if self.create_tables:
            Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, autoflush=False,
                                    expire_on_commit=False)
        return self

    def create_all(self):
        from admin.db import Base

        Base.metadata.create_all(self.engine)

    def __exit__(self, exc_type, exc, tb):
        try:
            self.engine.dispose()
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
        return False


class JobLeaseTests(unittest.IsolatedAsyncioTestCase):
    """admin/jobrunner.py：线程启动**之前**取租约，且只有一处释放。"""

    def setUp(self):
        self.state = _singleton_lifecycle()
        self.state.reset()
        self.state.mark_ready()

    def tearDown(self):
        self.state.reset()

    async def test_child_lease_registered_before_parent_release(self):
        from admin import jobrunner

        parent = self.state.try_admit("request")
        self.assertIsNotNone(parent)
        child = jobrunner.take_job_lease(parent=parent)
        self.assertIsNotNone(child, "父租约仍在窗口内，子租约必须登记成功")
        self.state.finish(parent)            # 外层中间件此刻才释放父租约
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["active_requests"], 0)
        self.assertEqual(snapshot["active_jobs"], 1,
                         "子租约先登记，计数没有归零窗口")

        waiter = asyncio.create_task(self.state.wait_empty())
        await asyncio.sleep(0.02)
        self.assertFalse(waiter.done(), "子租约还在，排空不能结束")
        self.state.finish(child)
        await asyncio.wait_for(waiter, timeout=1)
        self.assertEqual(self.state.snapshot()["active_jobs"], 0)

    async def test_job_runner_holds_lease_across_thread_and_releases_after(self):
        from admin import jobrunner

        started = threading.Event()
        release = threading.Event()

        def worker(job):
            started.set()
            self.assertTrue(release.wait(5))
            return {"ok": True}

        runner = jobrunner.JobRunner()
        job = runner.start("t5-lease", total=1, worker=worker, title="租约")
        self.assertTrue(started.wait(2), "后台线程应当已经启动")
        self.assertEqual(self.state.snapshot()["active_jobs"], 1,
                         "线程还在跑就必须占着一个 job 租约")

        waiter = asyncio.create_task(self.state.wait_empty())
        await asyncio.sleep(0.02)
        self.assertFalse(waiter.done(), "线程没结束，排空不能结束")

        release.set()
        await eventually(lambda: job.status == "finished", what="后台任务完成")
        await asyncio.wait_for(waiter, timeout=2)
        self.assertEqual(self.state.snapshot()["active_jobs"], 0)
        self.assertEqual(job.result, {"ok": True})

    def test_job_runner_releases_lease_when_thread_start_fails(self):
        from admin import jobrunner

        runner = jobrunner.JobRunner()
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("线程池拒绝启动")):
            with self.assertRaises(RuntimeError):
                runner.start("t5-start-fail", total=1, worker=lambda job: {},
                             title="启动失败")
        self.assertEqual(self.state.snapshot()["active_jobs"], 0,
                         "线程没起来就不许留下租约")

    async def test_job_runner_releases_lease_when_worker_raises(self):
        from admin import jobrunner

        def worker(job):
            raise ValueError("上游炸了")

        runner = jobrunner.JobRunner()
        job = runner.start("t5-raise", total=1, worker=worker, title="异常")
        await eventually(lambda: job.status == "failed", what="任务失败")
        self.assertIn("ValueError", job.error or "")
        await eventually(lambda: self.state.snapshot()["active_jobs"] == 0,
                         what="异常路径同样释放租约")

    async def test_job_runner_reuses_running_job_without_extra_count(self):
        from admin import jobrunner

        started = threading.Event()
        release = threading.Event()

        def worker(job):
            started.set()
            self.assertTrue(release.wait(5))
            return {}

        runner = jobrunner.JobRunner()
        first = runner.start("t5-reuse", total=1, worker=worker, title="复用")
        self.assertTrue(started.wait(2))
        second = runner.start("t5-reuse", total=1, worker=worker, title="复用")
        self.assertIs(second, first, "已 running 的同键任务必须复用同一个 Job")
        self.assertEqual(self.state.snapshot()["active_jobs"], 1,
                         "复用已 running 的 Job 不新增计数")

        release.set()
        await eventually(lambda: first.status == "finished", what="任务完成")
        await eventually(lambda: self.state.snapshot()["active_jobs"] == 0,
                         what="租约释放")

    def test_job_runner_defers_after_seal(self):
        from admin import jobrunner

        called = []
        self.state.begin("restart", "op-seal-job")
        runner = jobrunner.JobRunner()
        job = runner.start("t5-sealed", total=1,
                           worker=lambda job: called.append(1), title="封口后")
        self.assertEqual(called, [], "封口后不许再起后台线程")
        self.assertEqual(job.status, "failed")
        self.assertIn("排空", job.error or "")
        self.assertEqual(self.state.snapshot()["active_jobs"], 0)
        self.assertIsNone(runner.get("t5-sealed"), "未启动的任务不进注册表")

    def test_take_job_lease_returns_none_after_seal(self):
        from admin import jobrunner

        lease = jobrunner.take_job_lease()
        self.assertIsNotNone(lease, "ready 阶段必须能取到 job 租约")
        self.state.finish(lease)
        self.assertFalse(jobrunner.admission_closed())

        self.state.begin("restart", "op-seal-lease")
        self.assertIsNone(jobrunner.take_job_lease())
        self.assertTrue(jobrunner.admission_closed())


class SchedulerLeaseTests(unittest.TestCase):
    """admin/scheduler.py：每轮在封口后跳过派发，已开始的有限写入完成才释放。"""

    def setUp(self):
        self.state = _singleton_lifecycle()
        self.state.reset()
        self.state.mark_ready()
        from admin import scheduler

        self.scheduler = scheduler
        scheduler._last_platform_seed = 0.0

    def tearDown(self):
        self.state.reset()
        self.scheduler._last_platform_seed = 0.0

    def _add_due_schedule(self, tmp):
        from datetime import timedelta

        from admin.models import Schedule

        session = tmp.session()
        try:
            session.add(Schedule(name="t5 到期任务", task="refresh_balances",
                                 interval_minutes=60, enabled=1,
                                 next_run_at=datetime.utcnow() - timedelta(minutes=5)))
            session.commit()
        finally:
            session.close()

    def _seed_patch(self):
        return mock.patch.object(self.scheduler, "ensure_platform_schedules",
                                 return_value=[])

    def test_sealed_round_skips_dispatch(self):
        calls = []
        with _TempSqlite() as tmp, self._seed_patch(), mock.patch.object(
                self.scheduler, "_run_one",
                side_effect=lambda s, db, now: calls.append(s.id)):
            self._add_due_schedule(tmp)
            session = tmp.session()
            try:
                self.state.begin("restart", "op-sched-sealed")
                result = self.scheduler._scheduler_round(session)
            finally:
                session.close()
        self.assertEqual(calls, [], "封口后本轮不派发任何任务")
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(result["reason"], "closed")
        self.assertEqual(self.state.snapshot()["active_jobs"], 0)

    def test_ready_round_dispatches_once_and_releases(self):
        calls = []
        with _TempSqlite() as tmp, self._seed_patch(), mock.patch.object(
                self.scheduler, "_run_one",
                side_effect=lambda s, db, now: calls.append(s.id)):
            self._add_due_schedule(tmp)
            session = tmp.session()
            try:
                result = self.scheduler._scheduler_round(session)
            finally:
                session.close()
        self.assertEqual(len(calls), 1, "到期任务派发一次")
        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(self.state.snapshot()["active_jobs"], 0,
                         "本轮有限写入结束后立刻释放租约")

    def test_round_holds_lease_while_task_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def fake_run_one(schedule, db, now):
            entered.set()
            release.wait(5)

        def target(session):
            try:
                self.scheduler._scheduler_round(session)
            except Exception as exc:  # noqa: BLE001 - 测试里要看到真实异常
                errors.append(exc)

        with _TempSqlite() as tmp, self._seed_patch(), mock.patch.object(
                self.scheduler, "_run_one", side_effect=fake_run_one):
            self._add_due_schedule(tmp)
            session = tmp.session()
            thread = threading.Thread(target=target, args=(session,), daemon=True)
            try:
                thread.start()
                self.assertTrue(entered.wait(2), "调度轮次应当已经派发")
                self.assertEqual(self.state.snapshot()["active_jobs"], 1,
                                 "已开始的有限写入必须计数，排空要等它")
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
            finally:
                session.close()
        self.assertEqual(errors, [])
        self.assertEqual(self.state.snapshot()["active_jobs"], 0)

    def test_idle_round_does_not_count_resident_loop(self):
        with _TempSqlite() as tmp, self._seed_patch():
            session = tmp.session()
            try:
                result = self.scheduler._scheduler_round(session)
            finally:
                session.close()
        self.assertEqual(result["due"], 0)
        self.assertEqual(result["reason"], "idle")
        self.assertEqual(self.state.snapshot()["active_jobs"], 0,
                         "常驻调度循环本身绝不能被算成一个永远活跃的 Job")


class LogStreamDrainTests(unittest.IsolatedAsyncioTestCase):
    """两条日志观察流在 drain 时自己收尾；业务 SSE 仍然继续（对照）。"""

    def setUp(self):
        self.state = _singleton_lifecycle()
        self.state.reset()
        self.state.mark_ready()

    def tearDown(self):
        self.state.reset()

    @staticmethod
    def _request(path, headers=None):
        from starlette.requests import Request

        async def receive():
            await asyncio.Event().wait()

        return Request(http_scope("GET", path, headers=headers), receive)

    @staticmethod
    async def _collect_until_end(iterator, deadline=3.0):
        frames = []

        async def consume():
            async for chunk in iterator:
                frames.append(chunk)

        try:
            await asyncio.wait_for(consume(), deadline)
        except (asyncio.TimeoutError, TimeoutError):
            raise AssertionError("观察流在排空后没有主动结束（会被 Uvicorn 一直等）")
        return frames

    async def test_request_activity_stream_ends_with_draining_event(self):
        from admin.routers import logs as logs_router

        token = _stateless_dev_admin_token()
        if token is None:
            self.skipTest("当前环境不是开发默认密钥，无法使用无会话票据")

        payload = {"active": [], "active_count": 0, "recent": [], "usage_revision": 1}
        request = self._request("/api/logs/requests/stream",
                                headers={"x-admin-token": token})
        with mock.patch.object(logs_router, "_activity_sample", return_value=payload):
            response = await logs_router.request_activity_stream(
                request, key=None, limit=10, ticket=None)
            iterator = response.body_iterator
            first = await asyncio.wait_for(iterator.__anext__(), 2)
            self.assertIn("event: activity", first)

            self.state.begin("restart", "op-requests-stream")
            frames = await self._collect_until_end(iterator)

        joined = "".join(frames)
        self.assertIn(logs_router.DRAIN_EVENT, joined,
                      "排空时必须发一条明确收尾事件，前端据此暂停重连：" + repr(frames))
        self.assertIn('"retry": false', joined,
                      "收尾帧要带 retry=false，前端不能盲目重连")
        self.assertEqual(self.state.snapshot()["active_requests"], 0)

    async def test_ticket_issuance_is_refused_while_draining(self):
        """排空期间换不到票据：前端任何一次重连都不可能和旧代重建观察流。"""
        import admin.server as server

        token = _stateless_dev_admin_token()
        if token is None:
            self.skipTest("当前环境不是开发默认密钥，无法使用无会话票据")

        ready_session = _Session()
        await server.app(http_scope("GET", "/api/logs/requests/ticket",
                                    headers={"x-admin-token": token}),
                         ready_session.receive, ready_session.send)
        self.assertEqual(ready_session.status, 200, ready_session.body)
        self.assertTrue(ready_session.json().get("ticket"))

        self.state.begin("restart", "op-ticket-drain")
        drained = _Session()
        await server.app(http_scope("GET", "/api/logs/requests/ticket",
                                    headers={"x-admin-token": token}),
                         drained.receive, drained.send)
        self.assertEqual(drained.status, 503,
                         "排空期间换票被生命周期中间件拒绝")
        self.assertEqual(drained.json()["error"]["code"], "service_draining")

    async def test_usage_stream_ends_with_draining_event(self):
        from admin.routers import logs as logs_router

        def unreachable_session():
            raise RuntimeError("测试不连接任何数据库")

        request = self._request("/api/logs/usage/stream",
                                headers={"x-admin-token": "t5"})
        with mock.patch.object(logs_router, "stream_ticket_valid", return_value=True), \
                mock.patch("admin.db.SessionLocal", side_effect=unreachable_session):
            response = await logs_router.usage_log_stream(request, ticket="t5", _=True)
            iterator = response.body_iterator
            first = await asyncio.wait_for(iterator.__anext__(), 2)
            self.assertIn("retry:", first)

            self.state.begin("restart", "op-usage-stream")
            frames = await self._collect_until_end(iterator)

        self.assertIn(logs_router.DRAIN_EVENT, "".join(frames),
                      "用量观察流同样要主动收尾：" + repr(frames))

    async def test_business_sse_keeps_streaming_when_log_streams_close(self):
        """排空只结束观察流：已准入的业务 SSE 必须能继续写（不统一取消 task）。"""
        after_drain = []
        release = asyncio.Event()

        class _BusinessStream:
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/event-stream")]})
                await send({"type": "http.response.body",
                            "body": b"data: first\n\n", "more_body": True})
                await release.wait()
                for index in range(3):
                    await send({"type": "http.response.body",
                                "body": (b"data: %d\n\n" % (index + 2)),
                                "more_body": True})
                    after_drain.append(index)
                await send({"type": "http.response.body", "body": b"",
                            "more_body": False})

        middleware = LifecycleMiddleware(_BusinessStream(), lifecycle=self.state)
        session = _Session()
        task = asyncio.create_task(
            drive(middleware, http_scope("GET", "/api/agent/chat/stream"), session))
        await eventually(lambda: session.messages != [], what="首帧已发送")

        self.state.begin("restart", "op-business-stream")
        self.assertIsNone(self.state.try_admit("request"),
                          "新请求被拒，但已准入的流不受影响")
        release.set()
        await asyncio.wait_for(task, timeout=2)

        self.assertEqual(after_drain, [0, 1, 2], "已准入的业务流必须能写完尾帧")
        self.assertTrue(session.finished)
        self.assertEqual(self.state.snapshot()["active_requests"], 0)


class ReadinessTests(unittest.TestCase):
    """admin/server.py：就绪发布闸门与降级启动后的真实 DB 探测。"""

    def setUp(self):
        import admin.server as server

        self.server = server
        self.state = _singleton_lifecycle()
        self.state.reset()

    def tearDown(self):
        self.state.reset()

    def test_probe_reports_missing_tables_then_ready(self):
        with _TempSqlite(create_tables=False) as tmp:
            ok, reason = self.server._database_probe(tmp.engine)
            self.assertFalse(ok)
            self.assertIn("缺少必要表", reason)
            self.assertEqual(self.state.snapshot()["phase"], PHASE_STARTING)
            self.assertIsNone(self.state.try_admit("request"),
                              "必要表不齐时不许开准入")

            tmp.create_all()
            ok, reason = self.server._database_probe(tmp.engine)
            self.assertTrue(ok, reason)

    def test_probe_is_single_flight_and_rate_limited(self):
        attempts = []
        gate = threading.Event()

        def probe():
            attempts.append(1)
            gate.wait(5)
            # 库还没恢复：阶段保持 starting，限频才观察得到。
            return False, "数据库连接失败：OperationalError"

        probe_obj = self.server.ReadinessProbe(
            probe=probe, on_recovered=lambda: None, timeout=5.0,
            min_interval=60.0, lifecycle=self.state)
        first = {}
        worker = threading.Thread(target=lambda: first.setdefault("result", probe_obj.poke()))
        worker.start()
        try:
            _wait_until(lambda: probe_obj.busy, 2, "第一个探测进入在跑状态")
            self.assertEqual(probe_obj.poke(), "busy", "单飞：不许并发探测")
        finally:
            gate.set()
            worker.join(5)
        self.assertEqual(first.get("result"), "unavailable")
        self.assertEqual(len(attempts), 1)

        self.assertEqual(probe_obj.poke(), "throttled", "限频：立刻再来一次被挡住")
        self.assertEqual(len(attempts), 1, "被挡住的那次不许真的打库")

    def test_probe_timeout_is_bounded(self):
        def probe():
            time.sleep(0.5)
            return True, ""

        probe_obj = self.server.ReadinessProbe(
            probe=probe, timeout=0.05, min_interval=0.0, lifecycle=self.state)
        started = time.monotonic()
        self.assertEqual(probe_obj.poke(), "unavailable")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.45, "卡住的探测必须有上限")
        self.assertIn("超时", probe_obj.last_result[1])

    def test_publish_readiness_requires_database_init_and_tables(self):
        self.server._publish_readiness(db_ok=True, init_ok=True, tables_ok=False,
                                       reason="缺少必要表：usage_logs")
        self.assertEqual(self.state.snapshot()["phase"], PHASE_STARTING)
        self.assertIsNone(self.state.try_admit("request"), "缺表时不许开准入")

        self.server._publish_readiness(db_ok=True, init_ok=True, tables_ok=True)
        self.assertEqual(self.state.snapshot()["phase"], PHASE_READY)
        lease = self.state.try_admit("request")
        self.assertIsNotNone(lease)
        self.state.finish(lease)

    def test_database_failure_then_recovery_publishes_ready_once(self):
        with _TempSqlite(create_tables=False) as tmp:
            initialised = []
            probe_obj = self.server.ReadinessProbe(
                probe=lambda: self.server._database_probe(tmp.engine),
                on_recovered=lambda: initialised.append(1),
                timeout=2.0, min_interval=0.0, lifecycle=self.state)

            self.assertEqual(probe_obj.poke(), "unavailable")
            self.assertEqual(initialised, [], "库没恢复就不许补初始化")
            self.assertEqual(self.state.snapshot()["phase"], PHASE_STARTING)

            tmp.create_all()
            self.assertEqual(probe_obj.poke(), "ready")
            self.assertEqual(initialised, [1], "恢复后只补一次初始化")
            self.assertEqual(self.state.snapshot()["phase"], PHASE_READY)
            lease = self.state.try_admit("request")
            self.assertIsNotNone(lease)
            self.state.finish(lease)

            self.assertEqual(probe_obj.poke(), "phase_closed",
                             "已经 ready 之后不再重复探测/初始化")
            self.assertEqual(initialised, [1])

    def _startup_side_effects(self, *, db_ok, init_ok, tables):
        """把 `_startup` 里会真跑起来的副作用挡掉，只留「就绪发布」这条接线。"""
        import admin.platform.install_paths as install_paths
        import admin.pool as pool
        import admin.rollup as rollup
        import admin.wb_login as wb_login

        return [
            mock.patch.object(self.server, "wait_database_ready", return_value=db_ok),
            mock.patch.object(self.server, "init_db", return_value=init_ok),
            mock.patch.object(self.server, "_required_tables_present",
                              return_value=tables),
            mock.patch.object(self.server, "_log_client_version"),
            mock.patch.object(wb_login, "start_gc_thread"),
            mock.patch.object(rollup, "start_backfill_thread"),
            mock.patch.object(pool.POOL, "configure"),
            mock.patch.object(install_paths, "warmup"),
        ]

    def test_startup_does_not_publish_ready_when_tables_are_missing(self):
        """接线回归：缺表时 _startup **不许**发布 ready（去掉 mark_ready 闸门即红）。"""
        import contextlib

        initialised, probes = [], []
        with contextlib.ExitStack() as stack:
            for patch in self._startup_side_effects(
                    db_ok=True, init_ok=True,
                    tables=(False, "缺少必要表：usage_logs")):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                self.server, "_init_after_database_ready",
                side_effect=lambda: initialised.append(1) or {"steps": []}))
            stack.enter_context(mock.patch.object(
                self.server, "start_degraded_readiness_probe",
                side_effect=lambda **kwargs: probes.append(kwargs)))
            self.server._startup()

        self.assertEqual(self.state.snapshot()["phase"], PHASE_STARTING,
                         "必要表不齐时不许发布就绪")
        self.assertIsNone(self.state.try_admit("request"))
        self.assertEqual(initialised, [], "没就绪就不跑依赖数据库的初始化")
        self.assertEqual(len(probes), 1, "降级启动要拉起后台 DB 探测")
        self.assertTrue(callable(probes[0].get("on_recovered")),
                        "探测恢复后必须能补一次所需初始化")

    def test_startup_publishes_ready_after_necessary_init(self):
        import contextlib

        initialised, probes = [], []
        with contextlib.ExitStack() as stack:
            for patch in self._startup_side_effects(
                    db_ok=True, init_ok=True, tables=(True, "")):
                stack.enter_context(patch)
            stack.enter_context(mock.patch.object(
                self.server, "_init_after_database_ready",
                side_effect=lambda: initialised.append(1) or {"steps": []}))
            stack.enter_context(mock.patch.object(
                self.server, "start_degraded_readiness_probe",
                side_effect=lambda **kwargs: probes.append(kwargs)))
            self.server._startup()

        self.assertEqual(initialised, [1], "发布就绪之前必须先跑必要初始化")
        self.assertEqual(self.state.snapshot()["phase"], PHASE_READY)
        lease = self.state.try_admit("request")
        self.assertIsNotNone(lease)
        self.state.finish(lease)
        self.assertEqual(probes, [], "一切正常时不需要降级探测")

    def test_late_probe_cannot_reopen_admission_during_drain(self):
        initialised = []
        probe_obj = self.server.ReadinessProbe(
            probe=lambda: (True, ""), on_recovered=lambda: initialised.append(1),
            timeout=1.0, min_interval=0.0, lifecycle=self.state)
        self.state.begin("restart", "op-late-probe")

        self.assertEqual(probe_obj.poke(), "phase_closed")
        self.assertEqual(initialised, [], "排空期间不许补初始化")
        self.assertEqual(self.state.snapshot()["phase"], PHASE_DRAINING)
        self.assertIsNone(self.state.try_admit("request"),
                          "迟到的探针无权把准入改回 ready")


class ShutdownSequenceTests(unittest.IsolatedAsyncioTestCase):
    """关闭顺序：封口 → 在途归零 → 现有关闭钩子；清理只由一处承担。"""

    def setUp(self):
        self.state = _singleton_lifecycle()
        self.state.reset()
        self.state.mark_ready()

    def tearDown(self):
        self.state.reset()

    def test_only_one_shutdown_handler_is_registered(self):
        import admin.server as server

        handlers = [getattr(handler, "__name__", type(handler).__name__)
                    for handler in server.app.router.on_shutdown]
        self.assertEqual(handlers, ["graceful_shutdown"],
                         "清理只由一处承担，不许散在多个钩子里")

    async def test_shutdown_times_out_but_still_runs_hooks(self):
        """等不到零也必须记下来并继续收尾：不许谎报「已排空」更不能卡死进程。"""
        import admin.ip_activity as ip_activity
        import admin.loop_watch as loop_watch
        import admin.server as server
        import admin.upstream_http as upstream_http

        lease = self.state.try_admit("request")
        self.assertIsNotNone(lease)
        order = []

        async def fake_upstream_stop(app):
            order.append("upstream")

        async def fake_loop_watch_stop():
            order.append("loop")

        with mock.patch.dict(os.environ, {"ADMIN_SHUTDOWN_DRAIN_TIMEOUT": "0.05"}), \
                mock.patch.object(ip_activity, "flush", side_effect=lambda: order.append("ip")), \
                mock.patch.object(ip_activity, "_task", None), \
                mock.patch.object(upstream_http, "stop", side_effect=fake_upstream_stop), \
                mock.patch.object(loop_watch, "stop", side_effect=fake_loop_watch_stop):
            result = await server.graceful_shutdown()

        self.state.finish(lease)
        self.assertTrue(result["timed_out"], "还有在途请求时必须如实报告超时")
        self.assertEqual(order, ["ip", "upstream", "loop"],
                         "超时也要按顺序跑完现有钩子")
        self.assertEqual(self.state.snapshot()["phase"], PHASE_STOPPED)

    async def test_shutdown_waits_for_drain_then_runs_hooks_off_loop(self):
        import admin.ip_activity as ip_activity
        import admin.loop_watch as loop_watch
        import admin.server as server
        import admin.upstream_http as upstream_http

        timeline = []
        loop_ident = threading.get_ident()
        lease = self.state.try_admit("request")
        self.assertIsNotNone(lease)

        async def release_later():
            await asyncio.sleep(0.1)
            timeline.append("lease_released")
            self.state.finish(lease)

        releaser = asyncio.create_task(release_later())

        def fake_flush():
            timeline.append("hook:ip")
            self.assertNotEqual(
                threading.get_ident(), loop_ident,
                "阻塞清理不许在事件循环里跑")

        async def fake_upstream_stop(app):
            timeline.append("hook:upstream")

        async def fake_loop_watch_stop():
            timeline.append("hook:loop_watch")

        with mock.patch.object(ip_activity, "flush", side_effect=fake_flush), \
                mock.patch.object(ip_activity, "_task", None), \
                mock.patch.object(upstream_http, "stop", side_effect=fake_upstream_stop), \
                mock.patch.object(loop_watch, "stop", side_effect=fake_loop_watch_stop):
            result = await server.graceful_shutdown()

        await releaser
        self.assertEqual(timeline,
                         ["lease_released", "hook:ip", "hook:upstream", "hook:loop_watch"],
                         "必须先等已经准入的请求归零，再跑现有关闭钩子")
        self.assertEqual(result["order"],
                         ["sealed", "drained", "ip_activity", "upstream_pool",
                          "loop_watch", "stopped"])
        self.assertEqual(self.state.snapshot()["phase"], PHASE_STOPPED)
        self.assertIsNone(self.state.try_admit("request"))


class AuditPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """审计/记账落在请求租约之内，并且从另一条连接看恰好一条。"""

    async def test_audit_write_holds_lease_and_is_visible_once_from_another_connection(self):
        from admin import request_audit, request_metrics
        from admin.models import UsageLog
        from admin.request_metrics import TimingMiddleware

        with _TempSqlite() as tmp:
            gate = threading.Event()
            audit_started = threading.Event()
            events = []
            original_save = request_audit.save

            def hanging_save(record):
                events.append("audit_started")
                audit_started.set()
                gate.wait(5)
                original_save(record)
                events.append("audit_finished")

            class _BillingApp:
                async def __call__(self, scope, receive, send):
                    live = request_metrics.current.get()
                    live["key_id"] = 7
                    live["requested_model"] = "t5-model"
                    # 记账：这条请求产生一条用量行（与真实网关同表同口径）
                    with tmp.session() as db:
                        db.add(UsageLog(api_key_id=7, account_id=0,
                                        model="t5-model", credits=0.5,
                                        total_tokens=3,
                                        created_at=datetime.utcnow()))
                        db.commit()
                    await send({"type": "http.response.start", "status": 200,
                                "headers": []})
                    await send({"type": "http.response.body", "body": b'{"ok":true}'})
                    events.append("response_complete")

            state = Lifecycle()
            state.mark_ready()
            original_finish = state.finish

            def recording_finish(lease):
                events.append("lease_released")
                return original_finish(lease)

            state.finish = recording_finish
            app = LifecycleMiddleware(TimingMiddleware(_BillingApp(), persist=True),
                                      lifecycle=state)
            with mock.patch.object(request_audit, "save", side_effect=hanging_save), \
                    mock.patch.object(request_audit, "SessionLocal", tmp.session), \
                    mock.patch("admin.traffic_stats.record", return_value=None):
                task = asyncio.create_task(drive(
                    app,
                    http_scope("POST", "/v1/chat/completions",
                               headers={"content-type": "application/json"}),
                    _Session(body=b'{"model": "t5"}')))
                self.assertTrue(await asyncio.to_thread(audit_started.wait, 5),
                                "审计写入应已开始（挂起）")

                self.assertEqual(state.snapshot()["active_requests"], 1,
                                 "响应已写完但审计还挂着：请求租约必须仍握着")
                waiter = asyncio.create_task(state.wait_empty())
                await asyncio.sleep(0.05)
                self.assertFalse(waiter.done(), "审计没落库，排空不能结束")

                gate.set()
                await asyncio.wait_for(waiter, timeout=3)
                await asyncio.wait_for(task, timeout=3)

            self.assertEqual(state.snapshot()["active_requests"], 0)
            self.assertEqual(events, ["response_complete", "audit_started",
                                      "audit_finished", "lease_released"],
                             "顺序必须是：响应写完 → 审计落库 → 释放租约")

            # 另开一条连接（不经 SQLAlchemy 会话）确认记录确实可见且只有一条
            connection = sqlite3.connect(str(tmp.path))
            try:
                audits = connection.execute(
                    "SELECT COUNT(*) FROM request_audits").fetchone()[0]
                usage = connection.execute(
                    "SELECT COUNT(*) FROM usage_logs").fetchone()[0]
                outcome = connection.execute(
                    "SELECT outcome FROM request_audits").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(audits, 1, "审计必须恰好一条（不许重复写）")
            self.assertEqual(usage, 1, "记账必须恰好一条")
            self.assertEqual(outcome, "completed")


# ---------------------------------------------------------------------------
# 5. 任务 2 补：proxy 真实用量回填的 job 租约、agent 取消回收
# ---------------------------------------------------------------------------


class AgentTurnCancellationTests(unittest.IsolatedAsyncioTestCase):
    """agent._run_turn 取消路径：两个子 task 必须被回收，不留 pending。"""

    async def test_normal_turn_still_completes_without_leftovers(self):
        """正常路径不变：一轮跑完（meta → done），也不留 pending task。"""
        from admin.routers import agent as agent_router

        class _FakeRegistry:
            _tools = {}

            def specs(self):
                return []

        class _Row:
            conversation_id = "conv-t13-ok"
            messages = "[]"

        class _Request:
            async def is_disconnected(self):
                return False

        async def fake_call_model(db, key, model, messages, tools, **kwargs):
            return {"ok": True, "message": {"content": "hi"},
                    "usage": {"credits": 0.5, "total_tokens": 2}}

        with mock.patch.object(agent_router, "_call_model",
                               side_effect=fake_call_model), \
                mock.patch.object(agent_router, "_build_registry",
                                  return_value=_FakeRegistry()), \
                mock.patch.object(agent_router, "_load_messages", return_value=[]), \
                mock.patch.object(agent_router, "_system_prompt", return_value="sys"), \
                mock.patch.object(agent_router, "_parameter_hint", return_value=""), \
                mock.patch.object(agent_router, "_context_usage", return_value={}), \
                mock.patch.object(agent_router, "_save_messages",
                                  lambda *args, **kwargs: None):
            frames = [frame async for frame in agent_router._run_turn(
                _Request(), None, None, _Row(), "hi", "m", "chat")]

        self.assertEqual(len(frames), 2, "正常一轮应当是 meta + done 两帧")
        self.assertIn(b'"type": "meta"', frames[0])
        self.assertIn(b'"type": "done"', frames[1])
        leftover = [task for task in asyncio.all_tasks()
                    if task is not asyncio.current_task() and not task.done()]
        self.assertEqual(leftover, [], "正常路径同样不许留下 pending task")

    async def test_cancelled_turn_cancels_child_tasks(self):
        from admin.routers import agent as agent_router

        child_tasks = []
        started = asyncio.Event()

        class _FakeRegistry:
            _tools = {}

            def specs(self):
                return []

        class _Row:
            conversation_id = "conv-t13"
            messages = "[]"

        class _Request:
            async def is_disconnected(self):
                return False

        async def fake_call_model(db, key, model, messages, tools, **kwargs):
            child_tasks.append(asyncio.current_task())
            started.set()
            await asyncio.Event().wait()      # 永不返回：只有被取消才会结束

        with mock.patch.object(agent_router, "_call_model",
                               side_effect=fake_call_model), \
                mock.patch.object(agent_router, "_build_registry",
                                  return_value=_FakeRegistry()), \
                mock.patch.object(agent_router, "_load_messages", return_value=[]), \
                mock.patch.object(agent_router, "_system_prompt", return_value="sys"), \
                mock.patch.object(agent_router, "_parameter_hint", return_value=""), \
                mock.patch.object(agent_router, "_context_usage", return_value={}), \
                mock.patch.object(agent_router, "_save_messages",
                                  lambda *args, **kwargs: None):
            generator = agent_router._run_turn(
                _Request(), None, None, _Row(), "hi", "m", "chat")
            meta = await generator.__anext__()        # 第一帧 meta
            self.assertTrue(meta)
            consumer = asyncio.create_task(generator.__anext__())
            await asyncio.wait_for(started.wait(), 2)
            self.assertEqual(len(child_tasks), 1)
            call_task = child_tasks[0]

            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer

            for _ in range(8):
                await asyncio.sleep(0)
            self.assertTrue(call_task.done(),
                            "被取消的模型调用 task 必须结束（不许留成 pending）")
            self.assertTrue(call_task.cancelled())
            leftover = [task for task in asyncio.all_tasks()
                        if task is not asyncio.current_task() and not task.done()]
            self.assertEqual(leftover, [], "取消后不许留下 pending task")


class ResumeScopeTests(unittest.TestCase):
    """RESUME 只撤销尚未提交的 upgrade_hold：公共 API 自己守边界（不只是控制通道）。"""

    def _drained(self, action, operation_id):
        state = Lifecycle()
        state.mark_ready()
        state.begin(action, operation_id)
        return state

    def test_upgrade_hold_can_be_resumed(self):
        from admin.lifecycle import RESUMABLE_ACTION

        self.assertEqual(RESUMABLE_ACTION, "upgrade_hold")
        state = self._drained(RESUMABLE_ACTION, "op-hold-ok")
        self.assertEqual(state.snapshot()["phase"], PHASE_DRAINING)

        snapshot = state.resume("op-hold-ok")
        self.assertEqual(snapshot["phase"], PHASE_READY)
        self.assertIsNone(snapshot["action"])
        self.assertIsNone(snapshot["operation_id"])
        lease = state.try_admit("request")
        self.assertIsNotNone(lease, "撤销预排空后准入必须重新打开")
        state.finish(lease)

    def test_restart_stop_update_and_shutdown_cannot_be_resumed(self):
        for action in ("restart", "stop", "update", "shutdown"):
            with self.subTest(action=action):
                state = self._drained(action, "op-" + action)
                with self.assertRaises(LifecycleConflict) as caught:
                    state.resume("op-" + action)
                self.assertEqual(caught.exception.snapshot["action"], action)
                self.assertEqual(state.snapshot()["phase"], PHASE_DRAINING,
                                 action + " 的排空不许被 RESUME 撤销")
                self.assertIsNone(state.try_admit("request"),
                                  "被拒绝的 RESUME 不能偷偷把准入打开")

    def test_resume_keeps_idempotency_and_rejects_other_operation(self):
        state = self._drained("upgrade_hold", "op-hold-idem")
        first = state.resume("op-hold-idem")
        again = state.resume("op-hold-idem")      # CLI 重试：同 operation_id
        self.assertEqual(first, again, "同一 operation_id 的重复 RESUME 必须幂等")
        self.assertEqual(again["phase"], PHASE_READY)

        with self.assertRaises(LifecycleConflict) as caught:
            state.resume("op-invented")
        self.assertEqual(caught.exception.snapshot["phase"], PHASE_READY)

    def test_rejected_resume_keeps_the_operation_open(self):
        """被拒绝的 RESUME 不许改动任何状态：仍是同一次 restart 的排空。"""
        state = self._drained("restart", "op-still-open")
        with self.assertRaises(LifecycleConflict):
            state.resume("op-still-open")
        snapshot = state.snapshot()
        self.assertEqual(snapshot["action"], "restart")
        self.assertEqual(snapshot["operation_id"], "op-still-open")
        # 同一操作重复 begin 仍然幂等（发起方的重试路径不受影响）
        self.assertEqual(state.begin("restart", "op-still-open")["operation_id"],
                         "op-still-open")


if __name__ == "__main__":
    unittest.main(verbosity=2)
