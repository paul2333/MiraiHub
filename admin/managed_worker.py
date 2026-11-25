"""单 worker 运行器：封口 -> 排空 -> 停服 -> 退出码。

为什么不再直接 uvicorn.run(app)
-------------------------------
uvicorn 自己的 SIGTERM 处理只做"停止监听 + 取消在途 task"，谁也不知道这次
退出是用户点的重启、还是进程被人杀了。计划任务 3 要求：

    starting -> ready -> draining(封口) -> stopping(归零) -> 退出

并且"归零"必须是业务侧真实计数（由 admin/lifecycle.py 维护），不能在秒表到
点时靠 terminate 收场。所以这里显式持有 uvicorn.Server，把"什么时候
server.should_exit = True"握在自己手里：

1. 运行器建好**仅本机**控制端点（admin/lifecycle_control.py），再开始接业务；
2. 收到 BEGIN（Web 路由或父进程）就封口，然后 await lifecycle.wait_empty()；
3. 归零后（且原 HTTP 重启响应已写出）才在 Uvicorn 自己的事件循环里设置
   server.should_exit = True，让 Uvicorn 正常走完 lifespan shutdown；
4. 退出码说明这次退出的性质：正常停止 0、受理的计划重启 75、其余异常沿用
   原错误码（Uvicorn 启动失败仍是 3）。

没有任何"自动强退"秒表：等待多久由用户决定，只有明确确认的强制动作才会
中断。父进程（桌面壳 / main.py 监督器）按退出码与 IPC 回执决定要不要开
下一代。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import secrets
import sys
from pathlib import Path

import uvicorn

from admin import lifecycle_control as control

logger = logging.getLogger("admin.managed_worker")

#: 正常停止（用户点了停止/退出，或收到 SIGTERM）。
EXIT_OK = 0
#: 受理的计划重启/更新：由持有 Popen 的拉起方决定是否开下一代。
EXIT_PLANNED_RESTART = 75
#: 运行器自身失败（不是 Uvicorn 的启动失败码）。
EXIT_FAILURE = 1

#: 进程内"当前运行器"引用，供测试与诊断读取真实 server / 快照。
_ACTIVE: "ManagedWorker | None" = None


def _run_with_loop_factory(coro, factory):
    """`asyncio.run(coro, loop_factory=...)`，兼容 Python < 3.12。

    `loop_factory` 是 3.12 才加的 kwarg。本仓库要跑在 3.10 / 3.11 上
    （生产机就是 3.10），直接传会 TypeError：

        run() got an unexpected keyword argument 'loop_factory'

    于是 worker 永远 ready 不了（`test_lifecycle_resume` 就是这么挂的）。
    低版本走"自己建循环 + asyncio.run"等价路径：语义一致 —— 新建事件循环、
    设成当前循环、跑完关掉。
    """
    if factory is None:
        return asyncio.run(coro)
    try:
        return asyncio.run(coro, loop_factory=factory)
    except TypeError:
        # 3.12 以下：手动建循环
        loop = factory()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001 - 收尾失败不覆盖退出结果
                pass
            asyncio.set_event_loop(None)
            loop.close()


def active_runner() -> "ManagedWorker | None":
    """返回本进程正在运行的运行器（没有则 None）。值不是凭据。"""
    return _ACTIVE


def _default_lifecycle():
    """默认取 admin.lifecycle 的进程单例（任务 1 交付）。"""
    from admin.lifecycle import get_lifecycle

    return get_lifecycle()


class ManagedWorker:
    """持有 uvicorn.Server 的运行器本体。"""

    def __init__(self, app, *, host: str, port: int, lifecycle=None,
                 control_dir: Path | None = None, instance_id: str | None = None,
                 authkey: bytes | None = None, proxy_headers: bool = False,
                 log_level: str = "info", timeout_graceful_shutdown: int | None = None,
                 loop: str = "asyncio", http: str = "h11", ws: str = "none",
                 log_config=None, access_log: bool = False,
                 supervised: bool = False) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.lifecycle = lifecycle
        self.control_dir = Path(control_dir) if control_dir is not None else None
        self.instance_id = instance_id
        self.authkey = bytes(authkey) if authkey else None
        self.supervised = bool(supervised)
        self.config = uvicorn.Config(
            app, host=host, port=port, loop=loop, http=http, ws=ws, lifespan="on",
            proxy_headers=proxy_headers, log_level=log_level, log_config=log_config,
            access_log=access_log, timeout_graceful_shutdown=timeout_graceful_shutdown,
        )
        # 显式持有：注册、监控、封口、should_exit 都由本运行器掌握。
        self.server = uvicorn.Server(self.config)
        self.local: control.LocalControl | None = None
        self.control_server: control.WorkerControlServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit_action: str | None = None
        self._stop_requested = False
        #: 正在观察的 operation_id。RESUME 撤销之后要为**下一次**操作重新武装，
        #: 所以不能用"启动过就不再启动"的一次性布尔（否则撤销后再点停止/重启会挂住）。
        self._watching_op: str | None = None
        self._watch_task: asyncio.Task | None = None
        self.drained = False

    # -- 公开信息 -----------------------------------------------------------
    @property
    def instance(self) -> str | None:
        return self.instance_id

    @property
    def planned_restart(self) -> bool:
        return self._exit_action in control.PLANNED_ACTIONS

    # -- 运行 ---------------------------------------------------------------
    def run(self) -> int:
        """同步入口：在独立事件循环里跑完 Uvicorn，然后返回退出码。"""
        global _ACTIVE
        _ACTIVE = self
        try:
            return _run_with_loop_factory(
                self.serve(), self.config.get_loop_factory())
        except SystemExit as exc:
            code = exc.code
            logger.info("Uvicorn 以 SystemExit(%r) 结束", code)
            return code if isinstance(code, int) else EXIT_FAILURE
        except KeyboardInterrupt:  # 处理器还没装好就被 Ctrl+C：仍算正常停止
            return EXIT_OK
        except Exception:
            logger.exception("运行器异常退出")
            return EXIT_FAILURE
        finally:
            _ACTIVE = None

    async def serve(self) -> int:
        global _ACTIVE
        _ACTIVE = self
        self._loop = asyncio.get_running_loop()
        lifecycle = self._resolve_lifecycle()
        self.lifecycle = lifecycle
        instance = (self.instance_id or control.read_control_env().instance_id
                    or secrets.token_hex(16))
        self.instance_id = instance
        # 让同一进程里的 app（admin/server.py 的 lifespan）用同一个代号 reset()。
        os.environ[control.INSTANCE_ENV] = instance

        # 让 lifecycle 自己的快照也说真话：容器/无人监督部署 can_restart=False。
        # （GET /api/app/lifecycle 直接读 lifecycle，不经过 LocalControl。）
        set_restart_available = getattr(lifecycle, "set_restart_available", None)
        if callable(set_restart_available):
            try:
                set_restart_available(self.supervised)
            except Exception:  # noqa: BLE001
                logger.exception("设置 can_restart 能力失败（忽略）")

        self.local = control.LocalControl(
            lifecycle, instance_id=instance, can_restart=self.supervised,
            on_sealed=self._on_sealed, on_force=self._on_force, on_resume=self._on_resume,
        )
        self.local.attach_loop(self._loop)
        control.set_local_control(self.local)

        if self.control_dir is not None and self.authkey is not None:
            # 端点必须早于业务：父进程/Web 在首个请求到达时就该能问到状态。
            self.control_server = control.WorkerControlServer(
                self.control_dir, self.authkey, self.local.dispatch)
            self.control_server.start()

        # Uvicorn 在 serve() 内部用 self.handle_exit 安装信号处理器；
        # 换成我们的桥，首次 SIGTERM/SIGINT 只投递"正常停止"，重复回调幂等。
        self.server.handle_exit = self._signal_bridge

        try:
            await self.server.serve()
        finally:
            if self.control_server is not None:
                self.control_server.stop()
                self.control_server = None
            control.clear_local_control(self.local)
        return self._exit_code()

    def _resolve_lifecycle(self):
        if self.lifecycle is not None:
            return self.lifecycle
        try:
            return _default_lifecycle()
        except ImportError as exc:
            raise RuntimeError(
                "缺少 admin.lifecycle（任务 1 的交付）：请显式传入 lifecycle=，"
                "或先实现该模块") from exc

    # -- 封口与排空 ---------------------------------------------------------
    def _on_sealed(self, action: str, operation_id: str, source: str) -> None:
        """受理成功（已封口）：在同一个事件循环里开始观察排空。"""
        self._exit_action = action
        logger.warning("已受理排空动作 action=%s operation_id=%s source=%s",
                       action, operation_id, source)
        live = self._watch_task is not None and not self._watch_task.done()
        if live and self._watching_op == operation_id:
            # 同一次操作（含 upgrade_hold → stop 提交）：观察者已经在等它。
            return
        self._watching_op = operation_id
        self._watch_task = asyncio.ensure_future(self._watch_drain())

    async def _watch_drain(self) -> None:
        """等真实计数归零；预排空（upgrade_hold）归零后继续等着被提交或撤销。"""
        lifecycle = self.lifecycle
        local = self.local
        try:
            while True:
                action = local.effective_action() if local is not None else None
                await lifecycle.wait_empty()
                self.drained = True
                if action != "upgrade_hold":
                    break
                # 预排空：保持可查询，等发布方"提交停机"或"撤销"。
                committed = asyncio.ensure_future(local.committed.wait())
                resumed = asyncio.ensure_future(local.resumed.wait())
                try:
                    await asyncio.wait([committed, resumed],
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for waiter in (committed, resumed):
                        if not waiter.done():
                            waiter.cancel()
                if local.resumed.is_set():
                    logger.info("预排空被撤销，恢复接单")
                    return
                # 已提交：对外动作变更为 stop，回到循环按零计数收尾。
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 观测失败不能把退出流程卡死
            logger.exception("等待排空时出错，按已受理的动作继续")
        # 归零且确定要停：进入 stopping（父进程/壳据此显示"正在关闭服务"）。
        # 关闭钩子可能还要跑一会儿（刷盘、flush 审计），阶段必须停在 stopping。
        mark_stopping = getattr(lifecycle, "mark_stopping", None)
        if callable(mark_stopping):
            try:
                mark_stopping()
            except Exception:  # noqa: BLE001 - 阶段标记失败不能拦住退出流程
                logger.exception("mark_stopping 失败（忽略）")
        # 先把原 HTTP 重启响应发出去（若有），再停服；Uvicorn 之后还会
        # 正常等待当前 ASGI task 结束，因此响应一定写得完。
        if self.local is not None:
            await self.local.wait_response_gate()
        action = local.effective_action() if local is not None else self._exit_action
        self._exit_action = action
        # 归零之后才允许停服：此刻不再有在途业务。
        self.server.should_exit = True

    def _on_force(self, operation_id: str, instance_id: str, first: bool) -> None:
        logger.warning("受理强制结束 operation_id=%s instance_id=%s first=%s",
                       operation_id, instance_id, first)

    def _on_resume(self, operation_id: str, instance_id: str) -> None:
        logger.warning("预排空已撤销 operation_id=%s instance_id=%s",
                       operation_id, instance_id)

    # -- 信号 ---------------------------------------------------------------
    def _signal_bridge(self, signum: int, frame) -> None:
        """uvicorn 会把本函数装成 SIGTERM/SIGINT 处理器（见 Server.capture_signals）。

        只做两件事：记下"用户要停"，把真正的处理投递到事件循环。重复回调
        （连按两次 Ctrl+C、父进程重复发信号）保持幂等：绝不置 force_exit。
        """
        self._request_stop(signum)

    def _request_stop(self, signum: int) -> None:
        if self._stop_requested:
            logger.info("重复的停止信号 %s 已忽略（幂等）", signum)
            return
        self._stop_requested = True
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_stop_intent, signum)
        except RuntimeError:
            pass

    def _schedule_stop_intent(self, signum: int) -> None:
        asyncio.ensure_future(self._stop_intent(signum))

    async def _stop_intent(self, signum: int) -> None:
        local = self.local
        if local is None:
            return
        snapshot = local.snapshot()
        if snapshot.get("operation_id"):
            # 先到先得：已受理的操作（例如 Web 重启）不被信号改成 stop。
            logger.info("已有进行中的操作 %s，信号 stop 不覆盖", snapshot.get("operation_id"))
            return
        receipt = local.begin("stop", "signal-" + secrets.token_hex(8), None, source="signal")
        if not receipt.get("ok"):
            logger.warning("信号 stop 未获受理：%s", receipt.get("reason"))
            # 无法通过 lifecycle 封口时至少让 Uvicorn 走正常关闭流程。
            self._exit_action = "stop"
            self.server.should_exit = True

    # -- 退出码 -------------------------------------------------------------
    def _exit_code(self) -> int:
        if self._exit_action in control.PLANNED_ACTIONS:
            return EXIT_PLANNED_RESTART
        return EXIT_OK


def serve(app, *, host: str, port: int, lifecycle=None, control_dir: Path | None = None,
          instance_id: str | None = None, proxy_headers: bool = False,
          log_level: str = "info", timeout_graceful_shutdown: int | None = None,
          loop: str = "asyncio", http: str = "h11", ws: str = "none",
          log_config=None, access_log: bool = False, authkey: bytes | None = None,
          supervised: bool | None = None) -> int:
    """跑一代 worker，返回退出码（契约第 4 节）。

    control_dir / authkey / instance_id / supervised 缺省时从进程环境读取
    （父进程用 lifecycle_control.prepare_control_secrets() 准备）。
    """
    env = control.read_control_env()
    if control_dir is None:
        control_dir = env.control_dir
    if authkey is None:
        authkey = env.authkey
    if supervised is None:
        supervised = env.supervised
    runner = ManagedWorker(
        app, host=host, port=port, lifecycle=lifecycle, control_dir=control_dir,
        instance_id=instance_id, authkey=authkey, proxy_headers=proxy_headers,
        log_level=log_level, timeout_graceful_shutdown=timeout_graceful_shutdown,
        loop=loop, http=http, ws=ws, log_config=log_config, access_log=access_log,
        supervised=supervised,
    )
    return runner.run()


def _load_app(target: str):
    """加载 "module:attribute" 形式的 ASGI 应用。"""
    module_name, _, attribute = target.partition(":")
    if not module_name or not attribute:
        raise SystemExit(f"--app 必须写成 module:attribute，收到 {target!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError as exc:
        raise SystemExit(f"{module_name} 里没有 {attribute}") from exc


def main(argv: list[str] | None = None) -> int:
    """CLI：供容器 / 脚本在不带父进程监督器时启动单 worker。

        python -m admin.managed_worker --host H --port P [--app admin.server:app]
            [--control-dir DIR] [--proxy-headers] [--log-level info]
            [--timeout-graceful-shutdown N]
    """
    parser = argparse.ArgumentParser(
        prog="python -m admin.managed_worker",
        description="单 worker 运行器：封口 -> 排空 -> 停服 -> 退出码",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--app", default="admin.server:app")
    parser.add_argument("--control-dir", default=None,
                        help="私有控制目录（默认按端口与实例派生）")
    parser.add_argument("--instance-id", default=None)
    parser.add_argument("--proxy-headers", action="store_true")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--timeout-graceful-shutdown", type=int, default=None)
    parser.add_argument("--no-control", action="store_true",
                        help="不建控制端点（容器里由平台替换进程时）")
    args = parser.parse_args(argv)

    if not args.no_control and not control.read_control_env().enabled:
        instance = args.instance_id or secrets.token_hex(16)
        args.instance_id = instance
        directory = (Path(args.control_dir) if args.control_dir else
                     control.default_control_dir("%s-%s" % (args.port, instance[:8])))
        # 自己跑没有人会拉起下一代：standalone 的 can_restart 必须是 False，
        # 由平台（容器/systemd）替换进程。
        os.environ.update(control.prepare_control_secrets(
            directory, instance_id=instance, supervised=False))

    app = _load_app(args.app)
    return serve(
        app, host=args.host, port=args.port, proxy_headers=args.proxy_headers,
        log_level=args.log_level, timeout_graceful_shutdown=args.timeout_graceful_shutdown,
        # CLI 保持原来 "uvicorn admin.server:app ..." 入口的默认：loop/http/ws
        # 交给 Uvicorn 自动选择，访问日志照旧打开。
        loop="auto", http="auto", ws="auto",
        log_config=uvicorn.config.LOGGING_CONFIG, access_log=True,
    )


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
