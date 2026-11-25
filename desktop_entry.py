"""PyInstaller entry: one bundled interpreter runs UI or backend, never needs system Python."""
import os
import sys


if __name__ == "__main__":
    # The bootloader's parent PID remains alive until extraction cleanup is finished.
    if getattr(sys, "frozen", False):
        os.environ["WB_BOOTLOADER_PID"] = str(os.getppid())
        # Windowed executables have no console streams; dependencies still expect write().
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    from desktop.app import backend, run
    if len(sys.argv) == 3 and sys.argv[1] == "--backend":
        # 后端跑在独立进程里，父进程（桌面壳）要靠**退出码**区分两种情况：
        #   0  —— 正常停止（用户点停止/退出，或收到 SIGTERM）
        #   75 —— 已受理的计划重启（Web 重启 / 更新），应该开下一代
        # uvicorn.run() 时代这里永远返回 0，父进程无法分辨；现在由运行器
        # （admin/managed_worker.py）给出真实退出码，必须原样传给操作系统。
        code = backend(int(sys.argv[2]))
        sys.exit(code if isinstance(code, int) and not isinstance(code, bool) else 0)
    elif len(sys.argv) == 3 and sys.argv[1] == "--smoke-test":
        # Packaging verification needs the legacy windowed flow, never the tray.
        os.environ["WB_DESKTOP_SMOKE"] = sys.argv[2]
        run()
    else:
        # Double-clicking the EXE (or the autostart Run entry) should only drop a
        # tray icon in place; services start from its menu. Pass --no-tray for the
        # original single-window behaviour.
        run(tray_mode="--no-tray" not in sys.argv)
