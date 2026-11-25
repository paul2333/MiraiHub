"""持久回归：RESUME 走 Lifecycle.resume 的权威语义 + hold 租期到期自动恢复。

覆盖（计划任务 3 第 3 条 / 任务 7 第 191 行的回退通道）：
* RESUME 成功后 phase 从 draining 拉回 ready、try_admit 恢复可用、计数归零；
* 三类拒绝原因可判别：stale_generation / not_current_operation / stop_committed
  （worker 本地已把 hold 提交为 stop 时另有 already_committed，保留给已有调用方）；
* 旧代没有 lifecycle.resume 时仍回 resume_unsupported，绝不假装已恢复；
* upgrade_hold 租期到期且仍是同一未提交 hold → 自动恢复准入；提交 stop 后禁用；
* 真实 scripts/upgrade_control.py 的 begin → drain → abort 端到端跑通，
  输出里不再出现 resume_unsupported。

只用临时目录、动态端口与合成 ASGI 应用，不连部署库，不常驻服务。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin import lifecycle_control as control  # noqa: E402
from admin import managed_worker  # noqa: E402
from admin.lifecycle import Lifecycle  # noqa: E402


class _NoResumeLifecycle:
    """旧代替身：有 begin/force/snapshot，但没有 resume（降级路径用）。"""

    def __init__(self) -> None:
        self.instance_id = "inst-legacy"
        self._phase = "ready"
        self._action = None
        self._op = None
        self.cancelled = False

    def snapshot(self) -> dict:
        return {"phase": self._phase, "action": self._action,
                "instance_id": self.instance_id, "operation_id": self._op,
                "active_requests": 0, "active_jobs": 0, "elapsed_ms": 0,
                "can_restart": True, "can_force": self._phase == "draining"}

    def begin(self, action: str, operation_id: str, instance_id=None) -> dict:
        self._action = action
        self._op = operation_id
        self._phase = "draining"
        return self.snapshot()

    def force(self, operation_id: str, instance_id: str, confirm: bool) -> dict:
        self.cancelled = True
        return self.snapshot()

    def request_cancel(self) -> bool:
        return True


def _new_lifecycle(instance_id: str) -> Lifecycle:
    lifecycle = Lifecycle()
    lifecycle.reset(instance_id=instance_id)
    lifecycle.mark_ready()
    lifecycle.set_restart_available(True)
    return lifecycle


class ResumeSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """进程内：LocalControl.resume 的权威语义与租期恢复。"""

    async def asyncSetUp(self) -> None:
        self.lifecycle = _new_lifecycle("inst-resume")
        self.local = control.LocalControl(self.lifecycle, instance_id="inst-resume",
                                          can_restart=True)
        self.local.attach_loop(asyncio.get_running_loop())

    async def _hold(self, operation_id: str = "op-hold", *, seconds: float = 60.0) -> dict:
        return self.local.begin("upgrade_hold", operation_id,
                                hold_until=time.time() + seconds)

    async def test_resume_reopens_admission_and_keeps_counts(self):
        lease = self.lifecycle.try_admit("request")
        self.assertIsNotNone(lease)
        held = await self._hold()
        self.assertTrue(held["ok"], held)
        self.assertEqual(held["snapshot"]["phase"], "draining")
        self.assertIsNone(self.lifecycle.try_admit("request"), "封口后必须拒绝新业务")

        resumed = self.local.resume("op-hold", "inst-resume")
        self.assertTrue(resumed["ok"], resumed)
        self.assertFalse(resumed["duplicate"])
        # 加厚快照：直接当快照读
        self.assertEqual(resumed["phase"], "ready")
        self.assertIsNone(resumed["action"])
        self.assertEqual(resumed["operation_id"], "op-hold")
        self.assertEqual(resumed["instance_id"], "inst-resume")
        self.assertEqual(resumed["active_requests"], 1, "在途请求不受 RESUME 影响")

        self.lifecycle.finish(lease)
        self.assertEqual(self.local.snapshot()["active_requests"], 0)
        self.assertEqual(self.local.snapshot()["phase"], "ready")
        again = self.lifecycle.try_admit("request")
        self.assertIsNotNone(again, "RESUME 之后必须重新能接单")
        self.lifecycle.finish(again)

    async def test_resume_repeat_is_idempotent(self):
        self.assertTrue((await self._hold())["ok"])
        first = self.local.resume("op-hold", "inst-resume")
        self.assertTrue(first["ok"])
        repeat = self.local.resume("op-hold", "inst-resume")
        self.assertTrue(repeat["ok"], "同一次撤销重复投递要幂等")
        self.assertTrue(repeat["duplicate"])

    async def test_resume_rejects_stale_generation(self):
        self.assertTrue((await self._hold())["ok"])
        refused = self.local.resume("op-hold", "inst-old")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "stale_generation")
        self.assertEqual(self.local.snapshot()["phase"], "draining",
                         "拒绝时不许改变阶段")

    async def test_resume_rejects_not_current_operation(self):
        self.assertTrue((await self._hold())["ok"])
        refused = self.local.resume("op-other", "inst-resume")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "not_current_operation")
        self.assertEqual(self.local.snapshot()["phase"], "draining")

    async def test_resume_rejects_after_stop_committed(self):
        # (a) lifecycle 层已经进入 stopping：回退被禁用
        self.assertTrue((await self._hold())["ok"])
        self.lifecycle.mark_stopping()
        refused = self.local.resume("op-hold", "inst-resume")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "stop_committed")

        # (b) worker 本地把同一 hold 提交为 stop：另一条可判别原因
        lifecycle = _new_lifecycle("inst-commit")
        local = control.LocalControl(lifecycle, instance_id="inst-commit", can_restart=True)
        local.attach_loop(asyncio.get_running_loop())
        self.assertTrue(local.begin("upgrade_hold", "op-commit")["ok"])
        committed = local.begin("stop", "op-commit")
        self.assertTrue(committed["ok"], committed)
        self.assertEqual(committed["action"], "stop")
        after_commit = local.resume("op-commit", "inst-commit")
        self.assertFalse(after_commit["ok"])
        self.assertEqual(after_commit["reason"], "already_committed",
                         "本地已提交 stop 的可判别原因（保持与既有调用方一致）")
        self.assertEqual(lifecycle.snapshot()["phase"], "draining",
                         "提交后不许被 RESUME 拉回 ready")

    async def test_resume_refuses_non_hold_and_keeps_sealed(self):
        """RESUME 只撤销"尚未提交的部署预排空 hold"，不是通用取消键。"""
        restart = self.local.begin("restart", "op-restart")
        self.assertTrue(restart["ok"], restart)
        refused = self.local.resume("op-restart", "inst-resume")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "not_a_hold",
                         "restart/stop/update 的操作必须被独立识别，不能混成别的理由")
        self.assertEqual(self.local.snapshot()["phase"], "draining",
                         "拒绝之后准入不许被重开")
        self.assertIsNone(self.lifecycle.try_admit("request"))

        stop = control.LocalControl(_new_lifecycle("inst-stop"), instance_id="inst-stop",
                                    can_restart=True)
        stop.attach_loop(asyncio.get_running_loop())
        self.assertTrue(stop.begin("stop", "op-stop")["ok"])
        self.assertEqual(stop.resume("op-stop", "inst-stop")["reason"], "not_a_hold")

    async def test_non_hold_operation_has_no_lease_recovery(self):
        """租期自动恢复只对 upgrade_hold 生效：restart 不会被"过期"偷偷放开。"""
        lifecycle = _new_lifecycle("inst-restart-lease")
        local = control.LocalControl(lifecycle, instance_id="inst-restart-lease",
                                     can_restart=True)
        local.attach_loop(asyncio.get_running_loop())
        self.assertTrue(local.begin("restart", "op-restart-lease")["ok"])
        self.assertIsNone(local.hold_deadline, "非 hold 操作不该有租期")
        await asyncio.sleep(0.4)
        self.assertEqual(lifecycle.snapshot()["phase"], "draining",
                         "restart 不许被租期机制自动恢复准入")
        self.assertEqual(local.effective_action(), "restart")

    async def test_resume_unsupported_on_old_generation(self):
        legacy = _NoResumeLifecycle()
        local = control.LocalControl(legacy, instance_id="inst-legacy", can_restart=True)
        local.attach_loop(asyncio.get_running_loop())
        self.assertTrue(local.begin("upgrade_hold", "op-legacy")["ok"])
        refused = local.resume("op-legacy", "inst-legacy")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["reason"], "resume_unsupported")
        self.assertEqual(legacy.snapshot()["phase"], "draining",
                         "没有能力时绝不允许假装已恢复准入")

    async def test_hold_lease_expiry_auto_resumes_admission(self):
        """租期到期且仍是同一未提交 hold：自动恢复准入（用 0.2 秒短租期）。"""
        held = await self._hold("op-lease", seconds=0.2)
        self.assertTrue(held["ok"], held)
        self.assertEqual(self.local.snapshot()["phase"], "draining")
        await asyncio.sleep(0.6)
        self.assertEqual(self.local.snapshot()["phase"], "ready",
                         "租期到期必须自动恢复准入，不能永久封口")
        self.assertIsNone(self.local.effective_action())
        lease = self.lifecycle.try_admit("request")
        self.assertIsNotNone(lease, "自动恢复后必须重新能接单")
        self.lifecycle.finish(lease)

    async def test_hold_lease_after_commit_does_not_resume(self):
        self.assertTrue((await self._hold("op-hold-c", seconds=0.2))["ok"])
        self.assertTrue(self.local.begin("stop", "op-hold-c")["ok"])
        await asyncio.sleep(0.6)
        self.assertEqual(self.local.effective_action(), "stop")
        # LocalControl 对"已提交的 hold"对外报 stopping；权威阶段仍在 lifecycle 里，
        # 关键断言是：租期到期没有把它拉回 ready、也没有重开准入。
        self.assertEqual(self.lifecycle.snapshot()["phase"], "draining",
                         "提交 stop 之后禁用租期回退")
        self.assertIsNone(self.lifecycle.try_admit("request"))


class IpcResumeTests(unittest.IsolatedAsyncioTestCase):
    """IPC 面：RESUME 经 WorkerOwner 走通，拒绝原因翻译成 ControlRefused。"""

    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wb-resume-ipc-"))
        self.control_dir = self.root / "control"
        secrets_env = control.prepare_control_secrets(
            self.control_dir, instance_id="inst-ipc-resume", supervised=True)
        self.key = bytes.fromhex(secrets_env[control.AUTHKEY_ENV])
        self.lifecycle = _new_lifecycle("inst-ipc-resume")
        self.local = control.LocalControl(self.lifecycle, instance_id="inst-ipc-resume",
                                          can_restart=True)
        self.local.attach_loop(asyncio.get_running_loop())
        self.server = control.WorkerControlServer(self.control_dir, self.key,
                                                  self.local.dispatch, read_timeout=1.0)
        self.server.start()
        self.owner = control.WorkerOwner(self.control_dir, self.key, timeout=1.5)

    async def asyncTearDown(self) -> None:
        if self.server is not None:
            self.server.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_resume_over_ipc_reopens_and_reports_snapshot(self):
        held = await asyncio.to_thread(self.owner.begin_upgrade_hold,
                                       "op-ipc", time.time() + 60)
        self.assertEqual(held["phase"], "draining", held)
        resumed = await asyncio.to_thread(self.owner.resume, "op-ipc", "inst-ipc-resume")
        self.assertTrue(resumed["ok"], resumed)
        self.assertEqual(resumed["phase"], "ready")
        self.assertEqual(resumed["instance_id"], "inst-ipc-resume")
        self.assertEqual(resumed["active_requests"], 0)
        self.assertIsNotNone(self.lifecycle.try_admit("request"))

    async def test_refusals_are_distinguishable_over_ipc(self):
        await asyncio.to_thread(self.owner.begin_upgrade_hold, "op-ipc2", time.time() + 60)
        with self.assertRaises(control.ControlRefused) as stale:
            await asyncio.to_thread(self.owner.resume, "op-ipc2", "inst-old")
        self.assertEqual(stale.exception.reason, "stale_generation")
        with self.assertRaises(control.ControlRefused) as wrong_op:
            await asyncio.to_thread(self.owner.resume, "op-other", "inst-ipc-resume")
        self.assertEqual(wrong_op.exception.reason, "not_current_operation")
        # 提交为 stop 之后回退失效
        await asyncio.to_thread(self.owner.begin, "stop", "op-ipc2", "inst-ipc-resume")
        with self.assertRaises(control.ControlRefused) as committed:
            await asyncio.to_thread(self.owner.resume, "op-ipc2", "inst-ipc-resume")
        self.assertIn(committed.exception.reason, ("already_committed", "stop_committed"))


# ---------------------------------------------------------------------------
# 真实 scripts/upgrade_control.py：begin → drain → abort
# ---------------------------------------------------------------------------

class _AdminLikeApp:
    """合成 ASGI 应用：lifespan 复位真实 Lifecycle，/api/app/ready 按真实阶段回答。"""

    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle = lifecycle

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self.lifecycle.reset(instance_id=os.getenv(control.INSTANCE_ENV))
                    self.lifecycle.mark_ready()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    self.lifecycle.mark_stopped()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return
        if scope["type"] != "http":
            return
        path = scope.get("path") or ""
        phase = self.lifecycle.snapshot()["phase"]
        if path == "/api/app/ready":
            ready = phase == "ready"
            body = json.dumps({"ready": ready}).encode("utf-8")
            status = 200 if ready else 503
        else:
            body = b'{"detail":"not found"}'
            status = 404
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"cache-control", b"no-store"),
                                (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})


def _child_main(argv: list[str]) -> int:
    """子进程入口：真实 managed_worker.serve + 真实 Lifecycle。"""
    assert argv and argv[0] == "--serve-child", argv
    port = int(argv[1])
    lifecycle = Lifecycle()
    code = managed_worker.serve(
        _AdminLikeApp(lifecycle), host="127.0.0.1", port=port, lifecycle=lifecycle,
        log_level="warning", access_log=False, log_config=None,
        timeout_graceful_shutdown=30,
    )
    return code


class _GateHandler(BaseHTTPRequestHandler):
    """升级等待代理的最小替身：/__upgrade_health 报告窗口已开、队列为空。"""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        payload = json.dumps({"ok": True, "upgrading": False, "pending": 0}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静默：测试输出只留 CLI 的话
        return


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class UpgradeCliAbortTests(unittest.TestCase):
    """真实 CLI：begin（hold）→ drain → abort（RESUME）必须真正恢复准入。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="wb-cli-abort-"))
        self.marker = self.root / "upgrade" / "active"
        self.control_dir = self.root / "control"
        self.key_file = self.root / "authkey"
        secrets_env = control.prepare_control_secrets(
            self.control_dir, instance_id="inst-cli", supervised=True)
        self.key_file.write_text(secrets_env[control.AUTHKEY_ENV], encoding="utf-8")
        self.key = bytes.fromhex(secrets_env[control.AUTHKEY_ENV])
        self.port = _free_port()
        self.gate = ThreadingHTTPServer(("127.0.0.1", 0), _GateHandler)
        self.gate_port = self.gate.server_address[1]
        self.gate_thread = __import__("threading").Thread(target=self.gate.serve_forever,
                                                         daemon=True)
        self.gate_thread.start()
        log = (self.root / "child.log").open("w", encoding="utf-8")
        env = dict(os.environ, **secrets_env)
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve-child", str(self.port)],
                cwd=str(ROOT), env=env, stdout=log, stderr=log)
        finally:
            log.close()
        self.owner = control.WorkerOwner(self.control_dir, self.key, timeout=1.5, popen=self.proc)

    def tearDown(self) -> None:
        if self.proc.poll() is None:
            try:
                self.owner.begin("stop", "cleanup-stop", None, timeout=5)
            except Exception:
                pass
            try:
                self.proc.wait(timeout=15)
            except Exception:
                self.proc.kill()
        self.gate.shutdown()
        self.gate.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    # -- 夹具 ---------------------------------------------------------------
    def _child_log(self) -> str:
        log = self.root / "child.log"
        return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

    def _wait(self, predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    def _ready(self) -> bool:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/app/ready" % self.port, timeout=1) as response:
                return response.status == 200 and json.load(response).get("ready") is True
        except (OSError, ValueError):
            return False

    def _cli(self, action: str, *extra: str) -> subprocess.CompletedProcess:
        command = [sys.executable, str(ROOT / "scripts" / "upgrade_control.py"), action,
                   "--marker", str(self.marker),
                   "--gate", "http://127.0.0.1:%d" % self.gate_port,
                   "--backend", "http://127.0.0.1:%d" % self.port,
                   "--control-dir", str(self.control_dir),
                   "--authkey-file", str(self.key_file), *extra]
        # 部署里 CLI 跑在装好本项目的环境里（admin 可导入）；测试用 PYTHONPATH 等价。
        # 输出固定按 UTF-8 解码：Windows 默认 GBK 会把中文日志读成 None。
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        return subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=120)

    # -- 用例 ---------------------------------------------------------------
    def test_begin_drain_abort_restores_admission(self):
        self.assertTrue(self._wait(self._ready),
                        "worker 没有 ready：\n" + self._child_log())

        begin = self._cli("begin", "--seconds", "60")
        self.assertEqual(begin.returncode, 0, begin.stdout + begin.stderr)
        self.assertTrue(self.marker.exists(), "begin 必须开启升级标记")
        self.assertEqual(self.owner.status()["phase"], "draining",
                         "begin 之后旧代必须已封口")

        drain = self._cli("drain", "--seconds", "30")
        self.assertEqual(drain.returncode, 0, drain.stdout + drain.stderr)
        self.assertIn("归零", drain.stdout)
        self.assertIn("commit", drain.stdout, "drain 之后应提示用 commit 提交停止")

        abort = self._cli("abort")
        self.assertEqual(abort.returncode, 0, abort.stdout + abort.stderr)
        self.assertNotIn("resume_unsupported", abort.stdout + abort.stderr)
        self.assertIn("旧代已恢复准入", abort.stdout)
        self.assertFalse(self.marker.exists(), "abort 成功后必须撤掉升级标记")
        self.assertIsNone(self.proc.poll(), "撤销只放开准入，不许把旧代弄退出")

        snapshot = self.owner.status()
        self.assertEqual(snapshot["phase"], "ready")
        self.assertIsNone(snapshot["operation_id"])
        self.assertEqual(snapshot["active_requests"], 0)
        self.assertTrue(self._wait(self._ready), "撤销后 HTTP readiness 必须恢复")

        # 撤销之后的**下一次**操作仍必须被受理（观察者要重新武装，不能被
        # 上一次 hold 的"已启动"标志挡住，否则停止/重启会挂住）。
        self.assertTrue(self.owner.begin("stop", "after-abort-stop", None)["ok"])
        self.assertEqual(self.owner.wait_exit(timeout=30), managed_worker.EXIT_OK)


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(_child_main(sys.argv[1:]))
