"""桌面壳统一控制器的持久回归（PERSISTENT REGRESSION TESTS）。

锁定计划任务 4 的行为：
* start / retry_start / restart / stop_services / quit / apply_update 走同一串行控制器；
* 连续 start 只 Popen 一次，慢启动期间 quit 不留孤儿；
* _start_worker 不再 terminate 已在运行的实例；
* open_window / open_console / splash 共用「本代 nonce + instance_id + 阶段」的就绪判定；
* 事件顺序固定为：关准入 -> 原请求尾帧 -> 后端实退 -> 安装器 -> 销毁窗口；
* 托盘 × 只隐藏（对 backend 零调用），无托盘 × 排队 quit 并在后端退出后只 destroy 一次；
* 同一重启双击只有一个 operation_id。

全部用临时目录 + 假 Popen / 假窗口 / 假托盘 / 事件闩：不启动真实服务、不连真实数据库、
不动系统设置。运行方式：

    cd MiraiHub
    python -m unittest discover -s tests -p test_desktop_lifecycle.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop import app as desktop_app  # noqa: E402


def wait_for(predicate, timeout=8.0, message="等待条件超时"):
    """轮询等待断言条件；超时就抛出带上下文的断言失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(message)


class FakeProcess:
    """最小 Popen 替身：只实现控制器真正依赖的 poll / wait / terminate。"""

    def __init__(self, world, index):
        self.world = world
        self.pid = 9000 + index
        self.exit_code = None
        self.signals = []

    def poll(self):
        if self.exit_code is not None and not getattr(self, "_announced", False):
            self._announced = True
            self.world.event("exit")
        return self.exit_code

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.exit_code is None:
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("wait timeout")
            time.sleep(0.01)
        self.poll()
        return self.exit_code

    def terminate(self):
        self.signals.append("terminate")
        self.world.event("terminate")

    def kill(self):
        self.signals.append("kill")
        self.world.event("kill")


class FakeOwner:
    """控制通道替身：记录 BEGIN / 回执快照 / FORCE，并按脚本放行尾帧与退出。"""

    def __init__(self, world, generation, process, api):
        self.world = world
        self.generation = generation
        self.process = process
        self.api = api
        self.action = None
        self.operation_id = None
        self.phase = "draining"
        self.begins = []
        self.force_calls = []

    # -- BEGIN / FORCE ------------------------------------------------------
    def begin(self, action, operation_id, instance_id=None, *, timeout=10.0):
        self.begins.append((action, operation_id, instance_id))
        self.world.event("seal:" + action)
        self.action = action
        self.operation_id = operation_id
        self.phase = "draining"
        if self.world.auto_finish:
            threading.Thread(target=self._finish, name="fake-tail", daemon=True).start()
        return {"ok": True, "accepted": True, "action": action,
                "operation_id": operation_id, "instance_id": instance_id,
                "snapshot": self.status()}

    def _finish(self):
        # 尾帧属于业务侧：先写完最后一帧，进程才真的退出。
        time.sleep(self.world.tail_delay)
        self.world.event("tail")
        self.phase = "stopping"
        self.process.exit_code = 75 if self.action in ("restart", "update") else 0

    def status(self):
        if self.world.unreachable:
            return {"phase": "unreachable", "action": self.action,
                    "instance_id": None, "operation_id": None,
                    "active_requests": None, "active_jobs": None,
                    "elapsed_ms": None, "can_restart": False, "can_force": False}
        return {"phase": self.phase, "action": self.action,
                "instance_id": self.generation.instance_id,
                "operation_id": self.operation_id,
                "active_requests": self.world.active_requests,
                "active_jobs": self.world.active_jobs,
                "elapsed_ms": 1000, "can_restart": True, "can_force": True}

    def force(self, operation_id, instance_id, confirm, *, timeout=10.0):
        if self.world.unreachable:
            raise desktop_app._control_module().ControlUnavailable(
                "控制通道暂时不可用", reason="unreachable")
        self.force_calls.append((operation_id, instance_id, confirm))
        self.world.event("force")
        return {"ok": True, "snapshot": self.status()}

    def wait_exit(self, timeout=None):
        return self.process.wait(timeout)


class FakeWindow:
    """记录导航 / 本地壳 / 销毁；evaluate_js 只记录脚本，绝不真正执行。"""

    def __init__(self, world):
        self.world = world
        self.urls = []
        self.htmls = []
        self.scripts = []
        self.destroy_calls = 0
        self.hidden = 0
        self.shown = 0
        self.closed = False

    def load_url(self, url):
        self.urls.append(url)
        self.world.event("navigate")

    def load_html(self, html):
        self.htmls.append(html)
        self.world.event("load_html")

    def destroy(self):
        self.destroy_calls += 1
        self.closed = True
        self.world.event("window-destroyed")

    def hide(self):
        self.hidden += 1

    def show(self):
        self.shown += 1

    def restore(self):
        pass

    def minimize(self):
        pass

    def evaluate_js(self, script):
        self.scripts.append(script)
        return None

    def get_current_url(self):
        return self.urls[-1] if self.urls else ""

    def create_file_dialog(self, *args, **kwargs):
        return None


class FakeTray:
    """托盘替身：只记录状态迁移与通知。"""

    def __init__(self, world):
        self.world = world
        self.states = []
        self.notifications = []
        self.stopped = 0
        self.icon = None
        self.state = "stopped"

    def set_state(self, state):
        self.states.append(state)
        self.state = state

    def notify(self, message):
        self.notifications.append(message)

    def stop(self):
        self.stopped += 1


class World:
    """一次用例的全部替身与事件日志。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.events = []
        self.processes = []
        self.ready = threading.Event()
        self.auto_finish = True
        self.tail_delay = 0.01
        self.active_requests = 1
        self.active_jobs = 0
        self.foreign_instance = False
        self.unreachable = False
        self.installer_error = None
        self.installer_calls = []
        self.window = FakeWindow(self)
        self.tray = FakeTray(self)
        self.api = None

    # -- 事件 ---------------------------------------------------------------
    def event(self, name):
        with self._lock:
            self.events.append(name)

    def order(self):
        with self._lock:
            return list(self.events)

    def seen(self, prefix):
        return [name for name in self.order() if name.startswith(prefix)]

    # -- 注入给 DesktopAPI 的协作者 -----------------------------------------
    def spawn(self, generation):
        process = FakeProcess(self, len(self.processes))
        self.processes.append(process)
        self.event("spawn")
        return process

    def owner(self, generation, process):
        return FakeOwner(self, generation, process, self.api)

    def readiness(self, generation):
        if not self.ready.is_set():
            return None
        instance = "instance-other" if self.foreign_instance else generation.instance_id
        return {"ready": True, "instance_id": instance}

    def install(self, source, target, bootloader_pid):
        if self.installer_error is not None:
            self.event("installer-failed")
            raise self.installer_error
        self.installer_calls.append((str(source), str(target), int(bootloader_pid)))
        self.event("installer")

    def windows(self):
        window = self.window
        return [window] if window is not None and not window.closed else []


class DesktopLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.world = World()
        self.addCleanup(self._tmp.cleanup)

    # -- 构造 ---------------------------------------------------------------
    def make_api(self, *, tray_mode=True, ready=False, update_target=None, **overrides):
        world = self.world
        if ready:
            world.ready.set()
        target = update_target if update_target is not None else self.base / "MiraiHub.exe"
        options = dict(
            spawn=world.spawn, owner_factory=world.owner, readiness=world.readiness,
            installer=world.install, update_target=target, windows=world.windows,
            port_picker=lambda preferred: 8790, clock=time.monotonic,
            poll_interval=0.02, start_timeout=5.0, seal_timeout=2.0,
            late_exit_seconds=0.0, tray_mode=tray_mode,
        )
        options.update(overrides)
        api = desktop_app.DesktopAPI(self.base, self._config(), **options)
        api.window = world.window
        api.tray = world.tray
        world.api = api
        world.api = api
        return api

    def _config(self):
        return {"port": 8790, "username": "desktop", "password": "pw",
                "jwt_secret": "secret-value-not-a-credential",
                "converter_api_key": "wb-test"}

    # -- 用例 ---------------------------------------------------------------
    def test_repeated_start_spawns_only_once(self):
        api = self.make_api(ready=False)
        self.assertTrue(api.start()["ok"])
        wait_for(lambda: self.world.seen("spawn"), message="没有启动子进程")
        # 慢启动期间连点三次：只报状态，绝不再 Popen，也绝不 terminate。
        api.retry_start()
        api.start()
        api.retry_start()
        self.assertEqual(len(self.world.processes), 1)
        self.assertEqual(self.world.seen("terminate"), [])
        self.world.ready.set()
        wait_for(lambda: api.phase == "ready")
        self.assertEqual(len(self.world.processes), 1)
        self.assertEqual(len(self.world.window.urls), 1)
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_quit_during_slow_start_leaves_no_orphan(self):
        api = self.make_api(ready=False)
        api.start()
        wait_for(lambda: self.world.seen("spawn"))
        api.quit()
        wait_for(lambda: api.allow_close, message="慢启动期间退出没有收尾")
        self.assertIsNone(api.current_generation)
        self.assertEqual(len(self.world.processes), 1)
        self.assertEqual(self.world.processes[0].exit_code, 0)
        self.assertEqual(self.world.seen("terminate"), [])
        self.assertEqual(self.world.seen("seal:stop"), ["seal:stop"])
        self.assertEqual(self.world.window.destroy_calls, 1)
        # 迟到的就绪回调：不导航、不保存旧端口、不再起进程。
        self.world.ready.set()
        time.sleep(0.2)
        self.assertEqual(self.world.window.urls, [])
        self.assertEqual(len(self.world.processes), 1)
        self.assertEqual(api.port, 0)

    def test_generation_registers_control_secrets_before_spawn(self):
        # 用非当前配置的端口，验证「就绪后才落盘」真的写回 config.json。
        api = self.make_api(ready=True, port_picker=lambda preferred: 8791)
        api.start()
        wait_for(lambda: api.phase == "ready")
        generation = api.current_generation
        self.assertIsNotNone(generation)
        env = generation.env
        self.assertEqual(env["WB_LIFECYCLE_INSTANCE_ID"], generation.instance_id)
        self.assertTrue(env["WB_LIFECYCLE_AUTHKEY"])
        self.assertEqual(env["WB_LIFECYCLE_SUPERVISED"], "1")
        self.assertEqual(env["WB_DESKTOP_BOOT"], generation.nonce)
        self.assertEqual(env["WB_DESKTOP_PARENT"], str(os.getpid()))
        self.assertTrue(Path(env["WB_LIFECYCLE_CONTROL_DIR"]).is_dir())
        self.assertEqual(generation.port, 8791)
        # 端口只有在**本代**确认就绪后才落盘。
        self.assertEqual(api.config.get("port"), 8791)
        self.assertTrue((self.base / "config.json").is_file())
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_startup_status_has_contract_fields(self):
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        status = api.startup_status()
        for field in desktop_app.SNAPSHOT_FIELDS:
            self.assertIn(field, status)
        self.assertIn("message", status)
        self.assertIn("failed", status)
        self.assertEqual(status["phase"], "ready")
        self.assertEqual(status["instance_id"], api.current_generation.instance_id)
        self.assertFalse(status["failed"])
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_draining_text_reports_remaining_and_waited(self):
        self.world.tail_delay = 0.4
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        api.restart()
        wait_for(lambda: api.phase == "draining")
        status = api.startup_status()
        self.assertIn("剩余 1 个请求", status["message"])
        self.assertIn("已等待", status["detail"])
        self.assertIn("自动重启", status["detail"])
        self.assertTrue(status["can_force"])
        wait_for(lambda: api.phase == "ready", message="重启没有进入下一代")
        api.quit()
        wait_for(lambda: api.allow_close)
        # 文案入口在自包含 SPLASH 里也要有（强退按钮与诊断入口）。
        self.assertIn("查看诊断", desktop_app.SPLASH)
        self.assertIn("强制结束", desktop_app.SPLASH)
        self.assertIn("继续等待", desktop_app.SPLASH)

    def test_tray_close_hides_without_touching_backend(self):
        api = self.make_api(ready=True, tray_mode=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        generation = api.current_generation
        before = len(self.world.events)
        self.assertFalse(api.on_window_closing())
        self.assertEqual(self.world.window.hidden, 1)
        self.assertEqual(self.world.seen("seal:"), [])
        self.assertEqual(generation.owner.begins, [])
        self.assertEqual(generation.owner.force_calls, [])
        self.assertIs(api.current_generation, generation)
        self.assertGreaterEqual(len(self.world.events), before)
        self.assertTrue(api.running())
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_window_mode_close_queues_quit_and_destroys_once(self):
        api = self.make_api(ready=True, tray_mode=False)
        api.start()
        wait_for(lambda: api.phase == "ready")
        self.assertFalse(api.on_window_closing())
        wait_for(lambda: api.allow_close, message="关窗没有排队 quit")
        self.assertEqual(self.world.window.destroy_calls, 1)
        # 销毁之后 close 事件放行，且绝不二次 destroy。
        self.assertTrue(api.on_window_closing())
        self.assertEqual(self.world.window.destroy_calls, 1)
        self.assertEqual(self.world.seen("terminate"), [])

    def test_restart_double_click_uses_one_operation_id(self):
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        generation = api.current_generation
        first = api.restart()
        second = api.restart()
        self.assertTrue(first["ok"])
        self.assertTrue(second.get("duplicate"), second)
        self.assertEqual(first["operation_id"], second["operation_id"])
        wait_for(lambda: api.phase == "ready" and api.current_generation is not generation,
                 message="重启没有进入下一代")
        self.assertEqual(len(self.world.processes), 2)
        self.assertEqual(len(generation.owner.begins), 1)
        action, operation_id, instance = generation.owner.begins[0]
        self.assertEqual(action, "restart")
        self.assertEqual(operation_id, first["operation_id"])
        self.assertEqual(instance, generation.instance_id)
        time.sleep(0.1)
        self.assertEqual(len(self.world.processes), 2)
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_stop_services_returns_to_local_shell_after_exit(self):
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        api.stop_services()
        wait_for(lambda: api.phase == "stopped", message="停服务没有收敛到 stopped")
        order = self.world.order()
        self.assertIn("seal:stop", order)
        self.assertIn("load_html", order)
        self.assertLess(order.index("exit"), order.index("load_html"))
        self.assertEqual(self.world.window.destroy_calls, 0)
        self.assertFalse(api.running())
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_unexpected_backend_exit_is_diagnosable_and_retryable(self):
        self.world.auto_finish = False
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        first = api.current_generation
        # 后台意外退出：没人请求停服，只是进程自己没了。
        self.world.processes[0].exit_code = 1
        wait_for(lambda: api.phase == "failed", message="意外退出没有进入可诊断状态")
        self.assertIsNone(api.current_generation)
        self.assertIn("退出码 1", api.startup_status()["message"])
        self.assertTrue(api.startup_status()["failed"])
        # 按实际 PID 状态重试：确认孩子已退出后允许新建（不是 terminate 旧句柄）。
        self.world.auto_finish = True
        api.retry_start()
        wait_for(lambda: api.current_generation is not None
                 and api.current_generation is not first, message="重试没有新建本代")
        wait_for(lambda: api.phase == "ready")
        self.assertEqual(len(self.world.processes), 2)
        self.assertEqual(self.world.seen("terminate"), [])
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_console_requires_current_generation_not_just_tcp(self):
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        generation = api.current_generation
        opened = []
        with mock.patch.object(desktop_app.webbrowser, "open", lambda url: opened.append(url)):
            # 端口已经被另一实例接管：即使 TCP 通也不能传登录信息。
            self.world.foreign_instance = True
            result = api.open_console()
            self.assertIn("error", result)
            self.assertEqual(opened, [])
            self.world.foreign_instance = False
            result = api.open_console()
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(len(opened), 1)
            url = opened[0]
            self.assertIn("/admin?token=", url)
            self.assertNotIn(generation.authkey, url)
            self.assertNotIn(generation.nonce, url)
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_console_refuses_when_not_ready(self):
        api = self.make_api(ready=False)
        opened = []
        with mock.patch.object(desktop_app.webbrowser, "open", lambda url: opened.append(url)):
            result = api.open_console()
        self.assertIn("error", result)
        self.assertEqual(opened, [])
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_update_order_and_single_installer(self):
        api = self.make_api(ready=True)
        api.update_file = self.base / "1.exe"
        api.update_file.write_bytes(b"MZ")
        api.update_state = {"busy": False, "ready": True, "message": ""}
        api.start()
        wait_for(lambda: api.phase == "ready")
        with mock.patch.dict(os.environ, {"WB_BOOTLOADER_PID": "4242"}):
            self.assertTrue(api.apply_update().get("ok"))
            self.assertTrue(api.apply_update().get("ok"))
            wait_for(lambda: api.allow_close, message="更新没有走到关宿主")
        order = self.world.order()
        for earlier, later in (("seal:update", "tail"), ("tail", "exit"),
                               ("exit", "installer"), ("installer", "window-destroyed")):
            self.assertIn(earlier, order)
            self.assertIn(later, order)
            self.assertLess(order.index(earlier), order.index(later), order)
        self.assertEqual(len(self.world.installer_calls), 1)
        source, target, bootloader = self.world.installer_calls[0]
        self.assertEqual(Path(source), api.update_file)
        self.assertEqual(Path(target), self.base / "MiraiHub.exe")
        self.assertEqual(bootloader, 4242)
        self.assertEqual(self.world.window.destroy_calls, 1)
        self.assertEqual(self.world.seen("terminate"), [])

    def test_update_preflight_failure_keeps_serving(self):
        api = self.make_api(ready=True, update_target=self.base / "missing" / "MiraiHub.exe")
        api.update_file = self.base / "1.exe"
        api.update_file.write_bytes(b"MZ")
        api.update_state = {"busy": False, "ready": True, "message": ""}
        api.start()
        wait_for(lambda: api.phase == "ready")
        result = api.apply_update()
        self.assertIn("error", result)
        self.assertEqual(self.world.seen("seal:"), [])
        self.assertTrue(api.running())
        self.assertEqual(api.phase, "ready")
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_update_requires_staged_package(self):
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        result = api.apply_update()
        self.assertIn("error", result)
        self.assertEqual(self.world.seen("seal:"), [])
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_installer_failure_keeps_window_and_allows_retry(self):
        self.world.installer_error = RuntimeError("powershell 不可用")
        api = self.make_api(ready=True)
        api.update_file = self.base / "1.exe"
        api.update_file.write_bytes(b"MZ")
        api.update_state = {"busy": False, "ready": True, "message": ""}
        api.start()
        wait_for(lambda: api.phase == "ready")
        api.apply_update()
        wait_for(lambda: api.phase == "failed", message="安装器失败没有进入可诊断状态")
        self.assertEqual(self.world.window.destroy_calls, 0)
        self.assertEqual(self.world.installer_calls, [])
        status = api.startup_status()
        self.assertIn("更新安装器", status["message"])
        self.assertTrue(status["failed"])
        # 用户点「重试安装」：同一操作允许重试**未启动成功**的脚本，成功后关宿主。
        self.world.installer_error = None
        self.assertTrue(api.retry_install().get("ok"))
        wait_for(lambda: api.allow_close, message="重试安装没有关宿主")
        self.assertEqual(len(self.world.installer_calls), 1)
        self.assertEqual(self.world.window.destroy_calls, 1)

    def test_start_old_version_relaunches_current_exe(self):
        """安装器起不来时的第二条出路：直接重新开一个宿主（旧版）。"""
        self.world.installer_error = RuntimeError("powershell 不可用")
        api = self.make_api(ready=True)
        api.update_file = self.base / "1.exe"
        api.update_file.write_bytes(b"MZ")
        api.update_state = {"busy": False, "ready": True, "message": ""}
        api.start()
        wait_for(lambda: api.phase == "ready")
        api.apply_update()
        wait_for(lambda: api.phase == "failed")
        with mock.patch.object(desktop_app.subprocess, "Popen") as popen:
            result = api.start_old_version()
            self.assertTrue(popen.called)
        self.assertTrue(result.get("ok"), result)
        wait_for(lambda: api.allow_close, message="启动旧版没有关宿主")
        self.assertEqual(self.world.window.destroy_calls, 1)

    def test_force_requires_confirmation_and_binds_operation(self):
        self.world.tail_delay = 0.4
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        generation = api.current_generation
        api.restart()
        wait_for(lambda: api.phase == "draining")
        self.assertIn("error", api.force_stop())
        self.assertEqual(generation.owner.force_calls, [])
        operation_id = api.startup_status()["operation_id"]
        result = api.force_stop(confirm=True)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(generation.owner.force_calls,
                         [(operation_id, generation.instance_id, True)])
        wait_for(lambda: api.phase == "ready", message="强退后没有进入下一代")
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_progress_view_is_non_destructive_on_live_page(self):
        self.world.tail_delay = 0.4
        api = self.make_api(ready=True)
        api.start()
        wait_for(lambda: api.phase == "ready")
        self.assertEqual(len(self.world.window.htmls), 0)
        api.restart()
        wait_for(lambda: api.phase == "draining")
        api.open_progress_window()
        self.assertEqual(self.world.window.htmls, [])
        self.assertTrue(self.world.window.scripts)
        self.assertIn("WBDesktopLifecycle", self.world.window.scripts[-1])
        self.assertEqual(self.world.window.destroy_calls, 0)
        wait_for(lambda: api.phase == "ready")
        api.quit()
        wait_for(lambda: api.allow_close)

    def test_unreachable_control_does_not_kill_process(self):
        # 让"孩子还活着但控制通道没有应答"成为稳定状态，而不是靠竞态碰运气。
        self.world.auto_finish = False
        self.world.unreachable = True
        api = self.make_api(ready=True, seal_timeout=0.3)
        api.start()
        wait_for(lambda: api.phase == "ready")
        api.stop_services()
        wait_for(lambda: api.phase == "unreachable", message="失联没有进入可诊断状态")
        self.assertEqual(self.world.seen("terminate"), [])
        self.assertTrue(api.running())
        self.assertIn("状态暂不可用", api.startup_status()["message"])
        # 只有明确确认的强制动作才允许父进程结束自己持有的进程树。
        killed = []

        def fake_kill(pid):
            killed.append(pid)
            self.world.event("kill-tree")
            self.world.processes[0].exit_code = 0

        with mock.patch.object(desktop_app, "_kill_process_tree", fake_kill):
            result = api.force_stop(confirm=True)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(killed, [self.world.processes[0].pid])
        wait_for(lambda: api.phase == "stopped", message="强退后没有收敛到 stopped")
        api.quit()
        wait_for(lambda: api.allow_close)


if __name__ == "__main__":
    unittest.main()
