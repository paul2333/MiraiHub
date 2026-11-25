#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""main.py — 一键拉起 workbuddy2api（单端口单进程部署）。

本项目对外只暴露「一个端口」即可：
  - 管理后台前端（index.html）与后端 API：http://127.0.0.1:8790/admin
  - 对外共享的托管网关（带 Key 校验 / 配额 / 用量记账）：http://127.0.0.1:8790/v1/chat/completions、/v1/models
  - 内嵌的独立网关 converter（本机桌面登录态直连，额外支持 /v1/responses、/v1/messages、/v1/balance）：
        http://127.0.0.1:8790/v1 (多平台网关)

（若你确实需要独立运行 converter 在 8787，直接 `python converter.py --desensitize` 即可，与本脚本互不冲突。）

子进程输出实时 tee 到控制台 + logs/ 日志；Ctrl+C / 关闭终端优雅关闭。

用法:
  python main.py                  # 前台常驻，监听 0.0.0.0:8790
  python main.py --port 8790
  python main.py --host 127.0.0.1
  python main.py --status         # 查看本部署所有者状态（加 --json 供脚本解析）
  python main.py --stop           # 停止本部署：先排空在途请求，不强杀
  python main.py --restart        # 只重启 worker：旧代排空退出后再开下一代
  python main.py --force          # 启动时强制接管端口（会中断在途工作）

进程角色:
  本脚本是"进程所有者/监督器"：用 python -m admin.managed_worker 启动**一个**
  worker，登记自己的 pid、启动身份、工作目录、端口、仅本机控制端点与本代信息，
  再按 worker 的真实退出码决定要不要开下一代（计划重启 75 不退避，其它码沿用
  原崩溃退避）。信号处理器只记录意图，真正的关闭与清理只由 finalize 做一次。

端口占用处理:
  普通再跑一次**不会**结束任何进程：同一部署同一端口已有活的所有者时，只显示
  "既有实例正在运行/启动/排空"和连接信息后返回。所有者被杀而孩子还活着时，
  只要它支持控制协议，就先请求它正常排空、确认端口释放再接管；没有协议或归属
  不清的旧实例一律保留，并打印手动处置方式。只有显式 --force 才结束占用者。

环境变量（可选，覆盖默认；部署务必设置）:
  ADMIN_PORT / ADMIN_HOST
  ADMIN_JWT_SECRET  : 生产务必覆盖为 >=32 字节随机串（默认是弱密钥，会报警）
  ADMIN_USERNAME / ADMIN_PASSWORD     : 后台登录凭据（**必填**，无默认值；未配置则登录 503）
  CONVERTER_DESENSITIZE / CONVERTER_LOG : 内嵌网关的脱敏开关与日志路径
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable  # 用运行本脚本的同一个解释器（venv / 系统都可）
LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)

#: 运行目录覆盖点（多实例 / 回归测试）：登记文件与每代控制端点都放这里下面。
RUN_DIR_ENV = "WB_RUN_DIR"
#: 单 worker 应用目标的覆盖点（回归测试用合成应用；生产不要设置）。
APP_TARGET_ENV = "WB_APP_TARGET"
DEFAULT_APP_TARGET = "admin.server:app"
#: 单 worker 运行器模块（任务 3 的交付）。
WORKER_MODULE = "admin.managed_worker"

#: 退出码：脚本据此区分"已就绪 / 正在切换 / 没有所有者 / 暂时问不到 / 归属不清"。
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_MISSING_DEPS = 2
EXIT_PORT_FOREIGN = 3
EXIT_BUSY = 4
EXIT_NO_OWNER = 5
EXIT_UNREACHABLE = 6
EXIT_UNMANAGED = 7
EXIT_REFUSED = 8
#: 与 admin/managed_worker.EXIT_PLANNED_RESTART 对齐：受理的计划重启。
PLANNED_RESTART_CODE = 75

REGISTRY_NAME = "owner.json"
GENERATION_NAME = "generation.json"
SUPERVISOR_DIR_NAME = "supervisor"
#: 孩子状态的轮询间隔（只影响快照新鲜度，绝不触发任何进程处置）。
CHILD_POLL_INTERVAL = 0.5
#: 孤儿排空的默认等待上限（秒）：给足时间，到点只放弃接管、不强杀。
ORPHAN_DRAIN_WAIT = 90.0

#: admin.lifecycle_control 延迟到"依赖自检之后"再导入：admin 包的初始化会加载
#: 平台模块，缺依赖时必须在 execv 之前就发现，而不是在 import 期崩掉。
control = None  # type: ignore[assignment]


def _load_control():
    """把 admin.lifecycle_control 绑定到模块级 control（幂等）。"""
    global control
    if control is None:
        from admin import lifecycle_control as module

        control = module
    return control


def _load_dotenv() -> None:
    """尽早把项目根目录的 .env 注入到 os.environ。

    必须在下面的部署安全检查之前执行：此前这些检查读的是「尚未加载 .env」的
    环境，于是即使 .env 里已经配好了 ADMIN_JWT_SECRET / ADMIN_USERNAME /
    ADMIN_PASSWORD，启动时依然会打印「未设置强 ADMIN_JWT_SECRET」「未配置
    ADMIN_USERNAME / ADMIN_PASSWORD」这类**误导性告警**，让人误以为配置丢了。
    （子系统自身会加载 .env，所以这只是虚惊一场，但足够让人排查半天。）
    """
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file)
        return
    except Exception:
        pass
    # 没装 python-dotenv 时退化为一个极简解析器（够用：KEY=VALUE，忽略注释）
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

_PRINT_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """安全打印：绝不因为控制台编码问题把整个监督进程搞崩。

    真实事故：Windows 控制台默认 GBK，`print("❌ ...")` 抛
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u274c'`。
    而 `_log` 是**监督进程**用来报告「子进程已退出」的关键路径 ——
    它一崩，父进程直接退出，uvicorn 子进程变成孤儿：
    端口还在监听，但已经没人管它，表现为**服务无响应且日志不再增长**，
    排查时极难看出真正原因（报错只写在 stderr 里）。

    所以这里显式按 UTF-8 写 stdout，失败再退到「替换不可编码字符」，
    保证日志功能永远不会反过来杀死进程。
    """
    with _PRINT_LOCK:
        try:
            print(msg, flush=True)
        except (BrokenPipeError, OSError):
            # 控制台/管道被对方关掉（守护脚本 -Once 退出、终端关窗口、
            # 宝塔回收子进程的 stdout）时写日志会抛 OSError。这里必须吞掉：
            # 让"打日志"把监督进程本身弄死，比丢一行日志严重得多
            # （表现为服务突然没人管、登记文件留在原地）。
            return
        except UnicodeEncodeError:
            # 退路 1：按 UTF-8 直接写底层 buffer（绕过控制台编码）
            try:
                buf = getattr(sys.stdout, "buffer", None)
                if buf is not None:
                    buf.write((msg + "\n").encode("utf-8", "replace"))
                    buf.flush()
                    return
            except Exception:
                pass
            # 退路 2：替换掉编不了的字符，至少把信息打出来
            try:
                enc = getattr(sys.stdout, "encoding", None) or "utf-8"
                print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
            except Exception:
                pass


def _pump(stream, log_path: Path, tag: str) -> None:
    """把子进程输出实时打到控制台并写入日志文件（类 tee，文本模式）。

    ⚠️ **这个函数一旦停止读取，整个服务就会「僵死」** —— 必须永不退出。

    原理：子进程的 stdout 是一根**管道**（PIPE）。管道缓冲区满了以后，
    子进程再写就会**阻塞在 write 上**；而 uvicorn 的 access log 是在
    事件循环里打的，于是「打日志」这一步把事件循环冻住：
    端口还在监听、进程 CPU 归零、**任何请求（连 404 路由）都不再返回**。
    实测就是这样：`admin.log` 停在某一刻不再增长，全站随后无响应。

    原实现把整个 `for` 循环包在一个 `try/except: pass` 里 —— 只要写入
    控制台或日志文件抛一次异常（最典型是 Windows GBK 控制台遇到中文/emoji
    的 `UnicodeEncodeError`），**读取线程直接结束**，管道再没人排空，
    子进程随即卡死。而 `pass` 让这件事连一行痕迹都没留下。

    现在的做法：读循环绝不因为「写失败」而退出 —— 写不进去就丢弃该行，
    继续排空管道。宁可丢日志，也不能让服务僵死。
    """
    lf = None
    try:
        lf = open(log_path, "a", encoding="utf-8")
    except Exception:
        lf = None
    try:
        for raw in iter(stream.readline, ""):
            if not raw:
                break
            # 1) 控制台回显：失败不算事，接着读
            try:
                _log(f"[{tag}] {raw.rstrip()}")
            except Exception:
                pass
            # 2) 落盘：失败也不算事，接着读
            if lf is not None:
                try:
                    lf.write(raw)
                    lf.flush()
                except Exception:
                    try:
                        lf.close()
                    except Exception:
                        pass
                    lf = None       # 之后只排空管道，不再尝试写盘
    finally:
        # 无论如何都要把管道读到 EOF：这是保证子进程不被写阻塞的最后一道保险。
        try:
            for _ in iter(stream.readline, ""):
                pass
        except Exception:
            pass
        if lf is not None:
            try:
                lf.close()
            except Exception:
                pass


# 运行后台所需的核心依赖；当前解释器缺任一即尝试切换到带依赖的虚拟环境。
_REQUIRED_DEPS = ["pymysql", "uvicorn", "fastapi", "redis", "sqlalchemy"]


def _interpreter_with_deps() -> str | None:
    """返回带全部依赖的 python 解释器路径。

    优先用当前解释器；若缺包，则回退到项目内 .venv/venv 或本机 managed venv。
    都找不到返回 None（调用方给出安装提示后退出）。
    """
    try:
        import importlib
        for m in _REQUIRED_DEPS:
            importlib.import_module(m)
        return sys.executable
    except Exception:
        pass
    candidates = [
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / "venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT / "venv" / "bin" / "python",
        Path(r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"),
    ]
    for c in candidates:
        if not c.exists():
            continue
        try:
            subprocess.run(
                [str(c), "-c", "import " + ", ".join(_REQUIRED_DEPS)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return str(c)
        except Exception:
            continue
    return None


def _resolve_host_port(args) -> tuple[str, int]:
    """解析监听地址/端口。

    **优先级：显式命令行 > 环境变量(.env) > 默认值。**

    原实现写成 `os.getenv("ADMIN_PORT", str(args.port))` —— 环境变量优先，
    于是 `.env` 里的 `ADMIN_PORT=8790` 会**吞掉** `--port 58634`，
    命令行上写的端口被静默忽略、仍然去起 8790：接管逻辑随即杀掉线上实例。
    实测中这造成过两次线上服务被误停（用 `--port` 起测试实例时）。
    命令行是操作者的**当次明确意图**，必须压过配置文件里的常驻值。
    """
    # **默认监听 `::`（双栈）而不是 `0.0.0.0`**：
    #   `0.0.0.0` 只绑 IPv4，客户端把 `localhost` 解析成 `::1` 时
    #   会报 `error sending request for url (http://127.0.0.1:8790/...)`。
    #   `::` 在 Windows/Linux 上默认双栈，同时接受 IPv4 与 IPv6。
    #   想只绑 IPv4 时显式传 `--host 0.0.0.0` 即可。
    #
    # **生产例外**：APP_ENV=prod 时默认只绑 127.0.0.1，并拒绝任何非回环地址。
    # 8790 直连公网意味着 nginx 上那套 TLS / 限流 / IP 封禁全部失效，
    # 攻击者可以绕过反代直接打应用层（事故报告 R9）。
    app_env = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
    is_prod = app_env == "prod"
    default_host = "127.0.0.1" if is_prod else "::"
    host = args.host if args.host is not None else os.getenv("ADMIN_HOST", default_host)
    if is_prod and host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(
            f"[main] APP_ENV=prod 时 ADMIN_HOST 必须是回环地址（当前 {host}）。"
            "生产只允许 nginx 反代进来。")
    if args.port is not None:
        port = int(args.port)
    else:
        port = int(os.getenv("ADMIN_PORT", "8790"))
    return host, port



# ---------------------------------------------------------------------------
# 运行目录 / 所有者登记 / 进程探活（计划任务 6）
#
# 为什么需要这一层：
#   旧实现靠"端口被占 + 命令行像自己人"推导归属，然后 taskkill 掉再接管。
#   那在长请求场景下等于把正在跑的工作连人带连接一起砍掉，而且"监听这个
#   端口"从来不能证明进程归属（端口可以被转发、SO_REUSEADDR、容器映射）。
#   现在改成：所有者自己登记身份与控制地址，控制走仅本机的 IPC，
#   启动/停止都先问所有者状态，只有确认退出才处理下一步。
# ---------------------------------------------------------------------------

def _app_target() -> str:
    """本次要跑的单 worker 应用（默认 admin.server:app）。

    WB_APP_TARGET 是回归测试/临时实例的覆盖点（合成 ASGI 应用）；
    生产、宝塔、桌面都不设它。
    """
    value = (os.getenv(APP_TARGET_ENV) or "").strip()
    return value or DEFAULT_APP_TARGET


def _worker_command(*, host: str, port: int, app_target: str,
                    control_dir: Path | None = None) -> list[str]:
    """构造单 worker 启动命令：任务 3 的 managed runner，不再是 uvicorn 命令行。

    只有运行器知道"封口 → 排空 → 停服 → 退出码"，父进程才能区分
    "受理的计划重启（75）"和"真崩了（其它码）"。
    """
    cmd = [PY, "-m", WORKER_MODULE,
           "--host", host, "--port", str(port), "--app", app_target,
           "--log-level", "info"]
    if control_dir is not None:
        cmd += ["--control-dir", str(control_dir)]
    return cmd


def _build_admin_cmd(args, *, control_dir: Path | None = None) -> tuple[list[str], int, str]:
    """保留原函数名与返回形状（命令、端口、主机）。

    名字沿用历史（admin 指管理端应用），实际启动的已经是 managed runner。
    """
    host, port = _resolve_host_port(args)
    return _worker_command(host=host, port=port, app_target=_app_target(),
                           control_dir=control_dir), port, host


def _run_base() -> Path:
    """运行目录根：受保护、按"部署 + 端口"分区，供 --stop 找到活的所有者。

    WB_RUN_DIR 是显式覆盖点（多实例 / 回归测试）；默认落在当前用户的
    LOCALAPPDATA（Windows）或 XDG_RUNTIME_DIR（Unix），不写仓库目录。
    """
    override = (os.getenv(RUN_DIR_ENV) or "").strip()
    if override:
        return Path(override)
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / "MiraiHub" / "run"
    return Path(os.getenv("XDG_RUNTIME_DIR") or "/tmp") / ("miraihub-%d" % os.getuid())


def _deployment_dir(port: int, root: Path = ROOT) -> Path:
    """同一仓库 + 同一端口 → 同一个目录；换端口/换部署互不干扰。"""
    digest = hashlib.sha1(_norm_text(root).encode("utf-8")).hexdigest()[:10]
    return _run_base() / ("deploy-%s-%d" % (digest, int(port)))


def _secure_dir(path: Path) -> Path:
    """建私有目录并尽量收敛权限（Windows ACL / Unix 0700）。

    失败只是少一层加固，不影响功能：绝不因为 ACL 工具不存在就让服务起不来。
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        user = os.getenv("USERNAME") or ""
        if user:
            try:
                subprocess.run(["icacls", str(path), "/inheritance:r",
                                "/grant:r", user + ":(OI)(CI)F"],
                               capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _atomic_write_json(path: Path, payload: dict) -> None:
    """同目录临时文件 + 原子替换：读者只会看到完整的旧版或完整的新版。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.tmp-%s" % (path.name, secrets.token_hex(6)))
    handle = open(tmp, "w", encoding="utf-8")
    try:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    finally:
        handle.close()
    os.replace(tmp, path)


def _read_json_file(path: Path) -> dict:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（不依赖 psutil；Windows 用内核 API，失败再退 tasklist）。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            # 5 = ACCESS_DENIED：进程存在但我们没权限查 → 判活（宁可保守）
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    out = _run_quiet(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], timeout=10.0)
    return str(pid) in out


def _find_pids_by_marker(marker: str) -> list[int]:
    """按命令行里的标记找进程（恢复"记录里没来得及写 pid"的孤儿）。"""
    marker_norm = _norm_text(marker)
    if not marker_norm:
        return []
    pids: list[int] = []
    me = os.getpid()
    if os.name == "nt":
        out = _run_quiet(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", _PS_LIST_PROCS], timeout=25.0)
        for line in out.splitlines():
            if "\t" not in line:
                continue
            pid_s, cmd = line.split("\t", 1)
            if marker_norm not in _norm_text(cmd):
                continue
            try:
                pid = int(pid_s.strip())
            except ValueError:
                continue
            if pid != me and pid not in pids:
                pids.append(pid)
        return pids
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid == me or pid in pids:
                continue
            if marker_norm in _norm_text(_cmdline_of(pid)):
                pids.append(pid)
    except Exception:
        pass
    return pids


class OwnerRegistry:
    """所有者登记：谁在跑这个部署、控制地址在哪、现在是第几代。

    文件躺在受保护的运行目录里（0700 / 受限 ACL）：authkey 只出现在这里和
    子进程环境，绝不进日志、HTTP 参数或响应体。写入一律"临时文件 + 原子
    替换"；清理只按"pid + 启动身份"删自己那一份，绝不误删下一代的。
    """

    def __init__(self, port: int, root: Path = ROOT) -> None:
        self.port = int(port)
        self.root = Path(root)
        self.dir = _deployment_dir(self.port, self.root)
        self.path = self.dir / REGISTRY_NAME

    def read(self) -> dict:
        return _read_json_file(self.path)

    def write(self, payload: dict) -> None:
        _secure_dir(self.dir)
        _atomic_write_json(self.path, payload)

    def clear(self, *, owner_pid: int, identity: str) -> bool:
        record = self.read()
        if not record:
            return False
        try:
            recorded_pid = int(record.get("owner_pid") or 0)
        except (TypeError, ValueError):
            recorded_pid = 0
        if recorded_pid != int(owner_pid):
            return False
        if (record.get("owner_identity") or "") != (identity or ""):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True


class GenerationRecord:
    """当前这一代的记录：孩子 pid、受保护的 IPC 目录与密钥。

    所有者意外消失（崩溃、被强杀、断电）时孩子可能还活着。下一位启动者据此
    认出这个"支持控制协议的孤儿"，请求它正常排空、确认端口释放后再接管。
    """

    def __init__(self, port: int, root: Path = ROOT) -> None:
        self.port = int(port)
        self.root = Path(root)
        self.dir = _deployment_dir(self.port, self.root)
        self.path = self.dir / GENERATION_NAME

    def read(self) -> dict:
        return _read_json_file(self.path)

    def write(self, payload: dict) -> None:
        _secure_dir(self.dir)
        _atomic_write_json(self.path, payload)

    def clear(self, *, instance_id: str, child_pid) -> bool:
        record = self.read()
        if not record:
            return False
        same_instance = (record.get("instance_id") or "") == (instance_id or "")
        try:
            same_pid = int(record.get("child_pid") or 0) == int(child_pid or 0)
        except (TypeError, ValueError):
            same_pid = False
        if not (same_instance or same_pid):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True


def _record_is_ours(record: dict) -> bool:
    """登记是不是本仓库这个部署的（换部署/换机器时不许互相接管）。"""
    root = (record.get("root") or "").strip()
    if not root:
        return True  # 老格式没有 root：后续靠端口与命令行核对
    return _norm_text(root) == _norm_text(ROOT)


def _process_start_time(pid: int):
    """进程创建时间（epoch 秒）；取不到返回 None（绝不据此否定任何东西）。

    为什么要它：pid 会被复用。只靠"pid 还在"判断归属，会在老所有者死掉、pid
    被别的进程拿走时把陌生人当成自己人。创建时间是内核给的、无法伪造的判据。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                ok = kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exited),
                    ctypes.byref(kernel), ctypes.byref(user))
                if not ok:
                    return None
                value = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            finally:
                kernel32.CloseHandle(handle)
            return value / 1e7 - 11644473600.0  # 1601-01-01 -> 1970-01-01
        except Exception:
            return None
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8", errors="replace") as fh:
            fields = fh.read().rsplit(")", 1)[-1].split()
        ticks = float(fields[19])  # starttime：字段 22（列表从字段 3 起）
        btime = 0.0
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("btime "):
                    btime = float(line.split()[1])
                    break
        return btime + ticks / (os.sysconf("SC_CLK_TCK") or 100)
    except Exception:
        return None


def _owner_process_matches(record: dict) -> bool:
    """登记里的 pid 现在还是不是同一代所有者（防 pid 被复用后误判）。

    判据顺序（任一能证伪就返回 False；都取不到时宁可保留）：
      1. pid 还活着；
      2. 进程创建时间与登记里的 started_at 对得上（内核提供，pid 复用时必然不符）；
      3. 命令行还是本项目入口 —— 相对路径启动（守护脚本就是 main.py）也算，
         所以只要求出现 main.py / admin.managed_worker，或通过严格判据。
    """
    try:
        pid = int(record.get("owner_pid") or 0)
    except (TypeError, ValueError):
        return False
    if not pid or not _pid_alive(pid):
        return False
    started = _process_start_time(pid)
    recorded = record.get("started_at")
    if started is not None:
        try:
            reference = float(recorded)
        except (TypeError, ValueError):
            reference = None
        if reference is not None:
            # 登记比进程自身创建更早、或晚了整整一天，只能是 pid 被复用了。
            if started > reference + 5.0 or started < reference - 86400.0:
                return False
    cmd = _cmdline_of(pid)
    if not cmd:
        return True  # 取不到命令行：不据此否定（宁可保留，也不删活人的登记）
    lowered = cmd.replace(chr(92), "/").lower()
    if "main.py" in lowered or WORKER_MODULE in lowered:
        return True
    return _looks_like_our_service(cmd)


def _iter_registries() -> list[dict]:
    base = _run_base()
    if not base.is_dir():
        return []
    found: list[dict] = []
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        record = _read_json_file(child / REGISTRY_NAME)
        if not record:
            continue
        record["_path"] = str(child / REGISTRY_NAME)
        found.append(record)
    return found


def _find_live_owner(port: int | None = None) -> dict | None:
    """找本部署里活着（pid 在、命令行是本项目）的所有者。"""
    for record in _iter_registries():
        if not _record_is_ours(record):
            continue
        if port is not None:
            try:
                recorded_port = int(record.get("port") or 0)
            except (TypeError, ValueError):
                recorded_port = 0
            if recorded_port != int(port):
                continue
        if _owner_process_matches(record):
            return record
    return None


def _clear_stale_registries(port: int | None = None) -> list[str]:
    """按身份清掉死掉的登记（pid 已退出，或 pid 已被别的程序复用）。"""
    removed: list[str] = []
    for record in _iter_registries():
        if not _record_is_ours(record):
            continue
        if port is not None:
            try:
                recorded_port = int(record.get("port") or 0)
            except (TypeError, ValueError):
                recorded_port = 0
            if recorded_port != int(port):
                continue
        if _owner_process_matches(record):
            continue
        registry = OwnerRegistry(int(record.get("port") or 0), ROOT)
        try:
            recorded_pid = int(record.get("owner_pid") or 0)
        except (TypeError, ValueError):
            recorded_pid = 0
        if registry.clear(owner_pid=recorded_pid,
                          identity=record.get("owner_identity") or ""):
            removed.append(str(registry.path))
    return removed


def _owner_client(record: dict, *, timeout: float = 2.0):
    """按登记里的控制目录与密钥构造客户端；缺字段返回 None（不猜）。"""
    _load_control()
    directory = (record.get("control_dir") or "").strip()
    key = (record.get("authkey") or "").strip()
    if not directory or not key:
        return None
    try:
        return control.WorkerOwner(Path(directory), key, timeout=timeout)
    except ValueError:
        return None


def _query_owner(record: dict) -> tuple[str, dict]:
    """问所有者一次状态：返回 (ok|unreachable, 快照)。

    失联只代表这次问不到，绝不解释成"可以安全强杀"。
    """
    client = _owner_client(record)
    if client is None:
        return "unreachable", {"phase": "unreachable", "reason": "no_control_channel"}
    try:
        snapshot = client.status()
    except Exception as exc:  # noqa: BLE001 - 任何异常都只是"问不到"
        return "unreachable", {"phase": "unreachable", "reason": type(exc).__name__}
    if (snapshot or {}).get("phase") == "unreachable":
        return "unreachable", snapshot
    return "ok", snapshot


_PHASE_WORDS = {
    "ready": "正在运行",
    "starting": "正在启动",
    "draining": "正在排空",
    "stopping": "正在停止",
    "stopped": "正在退出",
    "failed": "启动失败",
    "unreachable": "状态暂不可用",
    "none": "没有运行",
}


def _phase_word(phase) -> str:
    return _PHASE_WORDS.get(str(phase or ""), "状态未知")


def _shown_host(host: str) -> str:
    return "127.0.0.1" if str(host) in ("0.0.0.0", "::", "") else str(host)


def _print_endpoints(host: str, port: int, indent: str = "        ") -> None:
    shown = _shown_host(host)
    _log("%s管理后台   : http://%s:%d/admin" % (indent, shown, int(port)))
    _log("%s托管网关   : http://%s:%d/v1/chat/completions" % (indent, shown, int(port)))


def _print_manual_recovery(pid, port: int, *, reason: str) -> None:
    """归属不清 / 旧版无协议：保留原实例，并把手动处置方式说清楚。"""
    shown = ("pid %d" % pid) if pid else "（未取到 pid）"
    _log("[main] %s" % reason)
    _log("[main]   原实例 %s 已保留，本次不做任何结束动作。" % shown)
    _log("[main]   手动处置（确认可以中断在途工作后再执行其中一条）：")
    if os.name == "nt":
        _log("[main]     - 面板/服务管理器里停止本服务，或执行 stop-service.bat")
        _log("[main]     - taskkill /PID %s /T /F" % (pid or "<pid>"))
    else:
        _log("[main]     - systemctl stop workbuddy2api（或宝塔面板「停止」）")
        _log("[main]     - kill -TERM %s，等它自己退出后再启动" % (pid or "<pid>"))
    _log("[main]   或改用其它端口：python main.py --port %d" % (int(port) + 1))
    _log("[main]   确认要强制接管（会中断在途工作）：python main.py --force")

#: 宝塔面板的 pid 文件路径。写在这里，面板「停止」按钮才杀得对进程。
#: Windows 上没有该目录树（`/www/...` 会被解析成 `<盘符>:\www\...`，
#: 于是在项目之外凭空造目录），改用项目内的隐藏文件。
_PIDFILE = (Path("/www/server/python_project/vhost/pids/workbuddy2api.pid")
            if os.name != "nt" else ROOT / ".workbuddy2api.pid")


def _write_pidfile() -> None:
    """把本进程真实 pid 写入 pid 文件（宝塔「停止」按钮据此结束服务）。"""
    try:
        _PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        _PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
        _log(f"[main] pid {os.getpid()} 已写入 {_PIDFILE}")
    except Exception:
        pass


def _remove_pidfile() -> None:
    """退出时清理 pid 文件；仅当文件里确实是本进程时才删，避免误删新实例的。"""
    try:
        if _PIDFILE.is_file() and _PIDFILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _PIDFILE.unlink()
    except Exception:
        pass


def _port_in_use(host: str, port: int) -> bool:
    """探测端口是否已被监听（用于启动前自检，避免撞端口后疯狂重启）。"""
    import socket
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((probe_host, port)) == 0


# ---------------------------------------------------------------------------
# 进程 / 端口识别（跨平台）
#
# 为什么需要这一层：原先的实现**只认 Linux** ——
#   * `_pid_listening_on()` 调用 `ss -lntpH`；
#   * `_is_our_instance()` 读 `/proc/<pid>/cwd`。
# 在 Windows 上前者抛异常（没有 ss）、后者恒为 False，于是
# 「被本项目的旧实例占用 → 接管」这条分支**永远不可能命中**：
# 第二次 `python main.py` 只会拿到一句「端口已被占用」然后退出 ——
# 明明是自己上次没关干净，却没有任何办法重启。
# ---------------------------------------------------------------------------

def _norm_text(s) -> str:
    """路径/命令行归一化，便于跨平台比较（统一斜杠、忽略大小写）。"""
    return str(s or "").replace("\\", "/").lower()


def _run_quiet(cmd: list[str], timeout: float = 15.0) -> str:
    """执行命令并返回 stdout；任何失败都返回空串。

    识别进程失败不该让「启动」这件事崩掉，所以这里吞掉所有异常。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


#: PowerShell：列出「pid<TAB>命令行」。整表一次取出，而不是逐个 pid 起进程 ——
#: 每次 PowerShell 启动要几百毫秒，逐个查会慢到无法接受。
_PS_LIST_PROCS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process | "
    'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
)


def _listener_pids(port: int) -> list[int]:
    """返回监听指定端口的 pid 列表（Windows: netstat → CIM；macOS: lsof；Linux: ss）。"""
    if os.name == "nt":
        # netstat 最快且系统自带；解析不出来再退回 CIM（更准但慢）
        return _listener_pids_netstat(port) or _listener_pids_cim(port)
    if sys.platform == "darwin":
        # macOS 没有 ss（Linux 专用），用系统自带的 lsof
        return _listener_pids_lsof(port)
    return _listener_pids_ss(port)


def _listener_pids_lsof(port: int) -> list[int]:
    """macOS：`lsof -t` 列出监听该端口的 pid。"""
    out = _run_quiet(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], timeout=10.0)
    pids: list[int] = []
    for line in out.splitlines():
        s = line.strip()
        if s.isdigit():
            pid = int(s)
            if pid not in pids:
                pids.append(pid)
    return pids


def _listener_pids_ss(port: int) -> list[int]:
    import re as _re
    out = _run_quiet(["ss", "-lntpH", f"sport = :{port}"], timeout=5.0)
    pids: list[int] = []
    for m in _re.finditer(r"pid=(\d+)", out):
        pid = int(m.group(1))
        if pid not in pids:
            pids.append(pid)
    return pids


def _listener_pids_netstat(port: int) -> list[int]:
    """解析 `netstat -ano` 的 LISTENING 行（Windows）。

    形如：  TCP    0.0.0.0:8790    0.0.0.0:0    LISTENING    12345
    """
    out = _run_quiet(["netstat", "-ano", "-p", "TCP"], timeout=10.0)
    want = f":{port}"
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        proto, local, _remote, state, pid_s = parts
        if proto.upper() != "TCP" or not state.upper().startswith("LISTEN"):
            continue
        # 用 endswith 而非等值：本地地址可能是 0.0.0.0:8790 或 [::]:8790
        if not local.endswith(want):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid not in pids:
            pids.append(pid)
    return pids


def _listener_pids_cim(port: int) -> list[int]:
    out = _run_quiet(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         f"Get-NetTCPConnection -LocalPort {port} -State Listen "
         "-ErrorAction SilentlyContinue | "
         "Select-Object -ExpandProperty OwningProcess"],
        timeout=20.0)
    pids: list[int] = []
    for line in out.splitlines():
        s = line.strip()
        if s.isdigit():
            pid = int(s)
            if pid not in pids:
                pids.append(pid)
    return pids


def _cmdline_of(pid: int) -> str:
    """取某进程的命令行（取不到返回空串，不抛异常）。"""
    if os.name != "nt":
        if sys.platform == "darwin":
            # macOS 没有 /proc，用系统自带的 ps
            return _run_quiet(
                ["ps", "-ww", "-p", str(pid), "-o", "command="], timeout=10.0).strip()
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return f.read().replace(b"\0", b" ").decode("utf-8", "ignore").strip()
        except Exception:
            return ""
    out = _run_quiet(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"])
    if out.strip():
        return out.strip()
    # 老系统上 wmic 仍在，作为兜底
    out = _run_quiet(["wmic", "process", "where", f"processid={pid}",
                      "get", "commandline", "/value"], timeout=10.0)
    for line in out.splitlines():
        if line.lower().startswith("commandline="):
            return line.split("=", 1)[1].strip()
    return ""


def _is_our_instance(pid: int) -> bool:
    """该 pid 是否就是**本项目**的进程（宁松勿严：认出自己人才敢动手）。

    两个判据，任一命中即可：
      * Linux：`/proc/<pid>/cwd` 指向本项目根目录；
      * 通用 ：命令行里出现本项目根目录的绝对路径。

    第二条是关键 —— uvicorn 子进程的命令行形如
    `<项目>/.venv/Scripts/python.exe -m uvicorn admin.server:app ...`，
    本身就带着项目路径，所以**在 Windows 上也能认出来**。
    """
    if os.name != "nt":
        try:
            if os.path.realpath(f"/proc/{pid}/cwd") == os.path.realpath(str(ROOT)):
                return True
        except Exception:
            pass
    cmd = _cmdline_of(pid)
    return bool(cmd) and _norm_text(ROOT) in _norm_text(cmd)


def _terminate_tree(pid: int) -> None:
    """结束一个进程及其子进程（跨平台，尽力而为，失败不抛）。"""
    if os.name == "nt":
        # /T 连子进程一起收；必须带 /F —— 控制台进程对不带 /F 的 taskkill
        # 通常不响应（会回一句 could not be terminated）。
        _run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=20.0)
        return
    # POSIX：优先整组结束（子进程以 start_new_session 启动、自成进程组）
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except Exception:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                return
            except Exception:
                return
        time.sleep(1.0 if sig == signal.SIGTERM else 0.3)


def _wait_port_released(host: str, port: int, timeout: float) -> bool:
    """等端口释放；返回是否真的释放了。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _port_in_use(host, port):
            return True
        time.sleep(0.3)
    return not _port_in_use(host, port)


#: 重启场景下，等待旧实例释放端口的最长时间（秒）。
#: 宝塔面板的「重启」是 stop → sleep(1) → start：只要旧进程优雅退出略慢于 1 秒，
#: 新进程启动时端口就仍被占用。给一段等待窗口，重启才不会误判失败。
_RESTART_WAIT = 25.0


#: 本服务独有的命令行特征：uvicorn 的 ASGI 目标。
#: 比「路径里含项目名」更硬 —— 即使解释器在项目目录之外（系统 Python、
#: managed venv）也能认出这是我们自己的 uvicorn，而不是别人的服务。
_OUR_UVICORN_TARGET = "admin.server:app"

#: 本项目的入口脚本。仅当命令行里**同时**出现项目根目录与其中之一时，
#: 才认定是「我们的进程」。单看路径不够：在项目目录下开着的编辑器、终端、
#: 其它工具链，命令行里同样会带这个路径。
_OUR_ENTRY_SCRIPTS = ("main.py", "converter.py", "admin/server.py")


def _looks_like_our_service(cmd: str) -> bool:
    """命令行是否属于**本项目**的服务进程（严格判据，宁可漏杀不可误杀）。

    命中任一：
      1. 含 uvicorn 目标 `admin.server:app` —— 最强特征，与路径无关；
      2. 命令行以 **python 解释器** 开头，且含项目根目录与本项目入口脚本名。

    为什么不能只看「路径出现在命令行里」：实测踩过两次 ——
      * 在项目目录下启动的编辑器 / 终端 / node 工具链，命令行里也含该路径；
      * **外面那层 shell**：`powershell -Command "...Start-Process python main.py..."`，
        命令行同时含项目路径与 `main.py`，结果把「启动服务的那个 shell」杀掉了。
    这个判据会用来决定**结束进程**，误杀的代价不可逆，所以必须严。
    """
    c = (cmd or "").replace("\\", "/")
    if not c:
        return False
    if _OUR_UVICORN_TARGET in c:
        return True
    if _norm_text(ROOT) not in _norm_text(c):
        return False
    low = c.lower()
    if not any(s in low for s in _OUR_ENTRY_SCRIPTS):
        return False
    # 必须真由 python 解释器执行。cmd/powershell/bash 这层「包装器」不算 ——
    # 杀掉它等于杀掉用户自己的终端。
    return _is_python_exe(_first_token(c))


def _is_python_exe(token: str) -> bool:
    """该可执行文件名是否像 python 解释器（python / python3.13 / pythonw …）。"""
    name = token.replace("\\", "/").rsplit("/", 1)[-1].strip('"').lower()
    name = name[:-4] if name.endswith(".exe") else name
    return name.startswith("python") or name.startswith("pypy")


def _first_token(cmd: str) -> str:
    """取命令行首个 token，正确处理带空格的引号路径。"""
    s = (cmd or "").lstrip()
    if s.startswith('"'):
        end = s.find('"', 1)
        return s[1:end] if end > 0 else s
    return s.split(" ", 1)[0]


def _ancestor_pids() -> set[int]:
    """本进程的所有祖先 pid。**绝不能**结束它们 —— 那是用户的终端/启动器。"""
    out: set[int] = set()
    try:
        if os.name == "nt":
            raw = _run_quiet(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-CimInstance Win32_Process | "
                 'ForEach-Object { "$($_.ProcessId) $($_.ParentProcessId)" }'],
                timeout=25.0)
            parent: dict[int, int] = {}
            for line in raw.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    parent[int(parts[0])] = int(parts[1])
            cur = os.getpid()
            for _ in range(64):
                nxt = parent.get(cur)
                if not nxt or nxt in out or nxt == cur:
                    break
                out.add(nxt)
                cur = nxt
        else:
            cur = os.getpid()
            for _ in range(64):
                if sys.platform == "darwin":
                    # macOS 没有 /proc，用 ps 取父 pid
                    raw = _run_quiet(
                        ["ps", "-o", "ppid=", "-p", str(cur)], timeout=5.0).strip()
                    ppid = int(raw) if raw.isdigit() else 0
                else:
                    with open(f"/proc/{cur}/status", encoding="utf-8") as f:
                        ppid = 0
                        for line in f:
                            if line.startswith("PPid:"):
                                ppid = int(line.split()[1])
                                break
                if not ppid or ppid in out or ppid == cur:
                    break
                out.add(ppid)
                cur = ppid
    except Exception:
        pass
    return out


def _describe_pid(pid: int) -> str:
    cmd = _cmdline_of(pid)
    if not cmd:
        return f"    pid {pid}: (取不到命令行)"
    if len(cmd) > 200:
        cmd = cmd[:197] + "..."
    return f"    pid {pid}: {cmd}"



def _next_delay(rc: int, ran: float, crashes: int) -> tuple[float, int]:
    """下一次重启前的退避秒数与本代的连续崩溃计数。

    计划重启码（75，运行器受理的重启/更新）不退避、也不计崩溃：那是用户要的
    动作，不是故障。意外退出沿用原退避策略（1 → 2 → 4 …，上限 60 秒），
    稳定运行满 60 秒后计数清零 —— 真 bug 的死循环不会刷爆 CPU/日志。
    """
    crashes = int(crashes or 0)
    if int(rc) == PLANNED_RESTART_CODE:
        return 0.0, crashes
    if ran < 60:
        crashes += 1
    else:
        crashes = 0
    if crashes <= 1:
        return 1.0, crashes
    return min(60.0, 2.0 ** (crashes - 1)), crashes


def _orphan_wait(args) -> float:
    """等待孤儿排空的上限：默认给足，但绝不靠强杀收场。"""
    try:
        value = float(getattr(args, "wait_timeout", 0) or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value if value > 0 else ORPHAN_DRAIN_WAIT


def _orphan_child_pid(record: dict) -> int:
    """这代记录对应的孩子 pid；记录里没写就按控制目录在命令行里找回。"""
    try:
        pid = int(record.get("child_pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid and _pid_alive(pid):
        return pid
    marker = str(record.get("control_dir") or "")
    for candidate in _find_pids_by_marker(marker):
        if _pid_alive(candidate):
            return candidate
    return 0


def _drain_orphan(port: int, *, origin: str, wait: float) -> str:
    """处理"所有者已死、孩子还活着"的孤儿。

    返回 none / stale / drained / draining / unreachable。绝不强杀：只请求它
    正常排空，确认进程真的退出后才允许接管端口。控制通道失联时如实返回
    unreachable —— "问不到"永远不等于"可以安全杀掉"。
    """
    generation = GenerationRecord(port)
    record = generation.read()
    if not record:
        return "none"
    instance = record.get("instance_id") or ""
    pid = _orphan_child_pid(record)
    if not pid:
        generation.clear(instance_id=instance, child_pid=record.get("child_pid"))
        return "stale"
    client = _owner_client(record)
    if client is None:
        _log("[main] 发现上一代 worker（pid %d），但控制凭证缺失：保留它，不做处置。" % pid)
        return "unreachable"
    try:
        snapshot = client.status()
    except Exception as exc:  # noqa: BLE001 - 只是这次问不到
        _log("[main] 上一代 worker（pid %d）控制通道暂时不可用（%s）。"
             % (pid, type(exc).__name__))
        return "unreachable"
    phase = (snapshot or {}).get("phase")
    if phase == "unreachable":
        _log("[main] 上一代 worker（pid %d）状态暂不可用，端口仍被它持有。" % pid)
        return "unreachable"
    _log("[main] 发现支持控制协议的孤儿：pid %d，阶段 %s，代号 %s"
         % (pid, phase, instance or "-"))
    _log("[main]   请求它正常排空（来源 %s），排完确认退出后才接管端口…" % origin)
    operation = "orphan-stop-" + secrets.token_hex(6)
    try:
        client.begin("stop", operation, None, timeout=10.0)
    except control.ControlUnavailable as exc:
        _log("[main]   控制通道在排空请求上失联（%s）：保留孤儿，不接管。" % exc.reason)
        return "unreachable"
    except control.ControlRefused as exc:
        _log("[main]   孤儿拒绝了排空请求（%s）：按它自己的状态继续观察。" % exc.reason)
    deadline = time.monotonic() + max(1.0, float(wait))
    last_note = 0.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            break
        now = time.monotonic()
        if last_note == 0.0 or now - last_note >= 2.0:
            last_note = now
            live = {}
            try:
                live = client.status()
            except Exception:  # noqa: BLE001 - 排空中问不到很正常
                live = {}
            _log("[main]   孤儿仍在排空 · 剩余 %s 个请求、%s 个后台任务 · "
                 "阶段 %s · operation_id=%s"
                 % (live.get("active_requests"), live.get("active_jobs"),
                    live.get("phase") or phase, operation))
        time.sleep(0.5)
    if _pid_alive(pid):
        _log("[main]   孤儿在 %.0f 秒内没有退出：本次不接管，也不强杀。"
             % max(1.0, float(wait)))
        return "draining"
    generation.clear(instance_id=instance, child_pid=pid)
    _log("[main]   孤儿已正常退出（pid %d），端口释放后即可接管。" % pid)
    return "drained"


class Supervisor:
    """进程所有者：为每一代准备控制端点，按退出码决定要不要开下一代。

    职责边界（对齐计划的"谁决定什么"）：
      * 业务什么时候能停，由 worker 按自己的生命周期决定（封口 → 排空 → 停服）；
      * 所有者只据退出码与 IPC 回执判断这次退出是计划重启还是意外；
      * 信号处理器只记录意图，真正的关闭与清理只在主循环 / finalize 里做一次。

    控制端点从启动到退出地址稳定：孩子还没创建、正在准备重生、正在退出的空档
    里，本地 --stop / --status 一样能找到并问到状态。
    """

    def __init__(self, *, host: str, port: int, app_target: str, args=None) -> None:
        _load_control()
        self.host = str(host)
        self.port = int(port)
        self.app_target = str(app_target)
        self.args = args
        self.root = ROOT
        self.identity = secrets.token_hex(8)
        self.deploy_dir = _deployment_dir(self.port)
        self.sup_dir = self.deploy_dir / SUPERVISOR_DIR_NAME
        self.registry = OwnerRegistry(self.port)
        self.generations = GenerationRecord(self.port)
        self._started_at = time.time()
        self._lock = threading.RLock()
        self._intent = "run"
        self._action = None
        self._operation_id = None
        self._operation_since = None
        self._restart_from_instance = None
        self._force_requested = False
        self._force_done = False
        self._generation = 0
        self._instance_id = None
        self._child_pid = None
        self._child_status = {}
        self._phase_hint = "starting"
        self._crashes = 0
        self._popen = None
        self._owner = None
        self._gen_started = 0.0
        self._last_poll = 0.0
        self._stop_sent_for = None
        self._logfiles = []
        self._control = None
        self._supervisor_key_hex = None
        self._finished = False
        self._finalized = False
        self._exit_code = EXIT_OK

    # -- 只读信息 -----------------------------------------------------------
    @property
    def child_pid(self):
        return self._child_pid

    def control_address(self) -> str:
        try:
            return control.control_address(self.sup_dir)
        except Exception:  # noqa: BLE001 - 诊断字段，取不到就留空
            return ""

    def snapshot(self) -> dict:
        with self._lock:
            child = dict(self._child_status or {})
            phase = child.get("phase") or self._phase_hint
            if self._finished:
                phase = "stopped"
            elif self._popen is None and self._action == "stop":
                phase = "stopping"
            elapsed = 0
            if self._operation_since is not None:
                elapsed = int(max(0.0, time.monotonic() - self._operation_since) * 1000)
            return {
                # 契约字段（与 /api/app/lifecycle 同名同义，脚本可直接读）
                "phase": phase,
                "action": self._action,
                "instance_id": child.get("instance_id") or self._instance_id or self.identity,
                "operation_id": self._operation_id,
                "active_requests": child.get("active_requests"),
                "active_jobs": child.get("active_jobs"),
                "elapsed_ms": elapsed,
                "can_restart": not self._finished,
                "can_force": bool(self._action and self._operation_id
                                  and not self._force_done),
                # 监督器扩展字段：只给本机 CLI/脚本看，不进 HTTP 契约。
                "owner_pid": os.getpid(),
                "owner_identity": self.identity,
                "child_pid": self._child_pid,
                "generation": self._generation,
                "host": self.host,
                "port": self.port,
                "intent": self._intent,
                "control_address": self.control_address(),
                "deployment": str(self.deploy_dir),
                "supervisor": "main.py",
            }

    # -- 控制协议 -----------------------------------------------------------
    def dispatch(self, message: dict) -> dict:
        """控制线程入口：只改内存状态，绝不在控制线程里做阻塞的进程操作。"""
        reason = control.validate_message(message)
        if reason:
            return {"ok": False, "accepted": False, "reason": "invalid_message",
                    "message": reason, "snapshot": self.snapshot()}
        kind = message["kind"]
        if kind == "STATUS":
            return self._receipt(accepted=True, duplicate=True)
        if kind == "BEGIN":
            return self._request_action(message["action"], message["operation_id"],
                                        message.get("source") or "ipc")
        if kind == "FORCE":
            return self._request_force(message)
        # RESUME 只对 upgrade_hold 有意义，所有者这一层没有可撤销的预排空。
        return self._refuse("unsupported_action")

    def _receipt(self, *, accepted: bool, duplicate: bool, reason=None,
                 action=None) -> dict:
        snapshot = self.snapshot()
        return {
            "ok": bool(accepted),
            "accepted": bool(accepted),
            "duplicate": bool(duplicate),
            "reason": reason or None,
            "action": action or snapshot.get("action"),
            "operation_id": snapshot.get("operation_id"),
            "instance_id": snapshot.get("instance_id"),
            "source": "supervisor",
            "snapshot": snapshot,
        }

    def _refuse(self, reason: str) -> dict:
        return self._receipt(accepted=False, duplicate=False, reason=reason)

    def request_stop(self, source: str = "local") -> dict:
        """请求停止整个所有者（先排空 worker，再退出主循环）。"""
        return self._request_action("stop", "stop-" + secrets.token_hex(6),
                                    source=source)

    def request_restart(self, source: str = "local") -> dict:
        """请求只重启 worker：所有者继续活着，开完下一代继续服务。"""
        return self._request_action("restart", "restart-" + secrets.token_hex(6),
                                    source=source)

    def _request_action(self, action: str, operation_id: str,
                        source: str = "") -> dict:
        if action not in ("stop", "restart"):
            return self._refuse("unsupported_action")
        with self._lock:
            if self._finished:
                return self._refuse("already_stopped")
            if self._action is not None:
                if self._operation_id == operation_id and self._action == action:
                    return self._receipt(accepted=True, duplicate=True, action=action)
                return self._refuse("conflict")
            self._action = action
            self._operation_id = operation_id
            self._operation_since = time.monotonic()
            self._force_requested = False
            self._force_done = False
            if action == "stop":
                self._intent = "stop"
            else:
                self._restart_from_instance = (
                    (self._child_status or {}).get("instance_id") or self._instance_id)
            _log("[main] 已受理%s请求 operation_id=%s（来源 %s）"
                 % ("停止" if action == "stop" else "重启", operation_id,
                    source or "local"))
            return self._receipt(accepted=True, duplicate=False, action=action)

    def _request_force(self, message: dict) -> dict:
        with self._lock:
            if self._finished:
                return self._refuse("already_stopped")
            if not self._action or self._operation_id != message.get("operation_id"):
                return self._refuse("not_current_operation")
            if self._force_done or self._force_requested:
                return self._receipt(accepted=True, duplicate=True)
            current = self._instance_id or self.identity
            given = message.get("instance_id")
            if given and given not in (current, self.identity):
                return self._refuse("stale_generation")
            remaining = self._child_status or {}
            self._force_requested = True
            _log("[main] 收到强制结束请求 operation_id=%s（当前剩余 %s 个请求、"
                 "%s 个后台任务）" % (self._operation_id,
                                    remaining.get("active_requests"),
                                    remaining.get("active_jobs")))
            return self._receipt(accepted=True, duplicate=False)

    # -- 登记 ---------------------------------------------------------------
    def write_registry(self, *, phase_hint=None) -> None:
        """登记自己（原子替换）：身份、工作目录、端口、受保护 IPC 地址、本代信息。"""
        if phase_hint:
            self._phase_hint = phase_hint
        self.registry.write({
            "version": 1,
            "kind": "workbuddy2api-owner",
            "owner_pid": os.getpid(),
            "owner_identity": self.identity,
            "root": str(self.root),
            "workdir": os.getcwd(),
            "host": self.host,
            "port": self.port,
            "control_dir": str(self.sup_dir),
            "control_address": self.control_address(),
            "control_family": control.control_family(),
            "authkey": self._supervisor_key_hex,
            "generation": self._generation,
            "child_pid": self._child_pid,
            "instance_id": self._instance_id,
            "app_target": self.app_target,
            "intent": self._intent,
            "phase_hint": self._phase_hint,
            "python": PY,
            "started_at": self._started_at,
            "updated_at": time.time(),
        })

    # -- 信号 ---------------------------------------------------------------
    def install_signals(self) -> None:
        """只装"记录意图"的处理器：信号上下文里不做任何清理或结束动作。"""
        def handler(signum, _frame):
            self.request_stop(source="signal-%d" % int(signum))

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError, RuntimeError):
                continue

    # -- 主循环 -------------------------------------------------------------
    def run(self) -> int:
        self._start_control()
        self.install_signals()
        self.write_registry(phase_hint="starting")
        _write_pidfile()
        _log("[main] 所有者已启动：pid %d，身份 %s，控制端点随时可用"
             % (os.getpid(), self.identity))
        try:
            while not self._finished:
                try:
                    self._tick()
                except KeyboardInterrupt:
                    self.request_stop(source="keyboard")
                if not self._finished:
                    time.sleep(0.2)
        finally:
            code = self.finalize()
        return code

    def _start_control(self) -> None:
        prepared = control.prepare_control_secrets(self.sup_dir, supervised=True)
        self._supervisor_key_hex = prepared[control.AUTHKEY_ENV]
        self._control = control.WorkerControlServer(
            self.sup_dir, bytes.fromhex(self._supervisor_key_hex), self.dispatch)
        self._control.start()

    def _tick(self) -> None:
        if self._popen is None:
            if self._intent == "stop":
                self._finished = True
                self._exit_code = EXIT_OK
                return
            if not self._spawn_generation():
                self._finished = True
                self._exit_code = EXIT_FAILURE
            return
        self._refresh_child_status()
        code = self._popen.poll()
        if code is not None:
            self._handle_child_exit(code)
            return
        self._send_worker_intent()
        self._apply_force()
        self._maybe_finish_restart()

    def _launch_child(self, command: list, env: dict):
        logfile = LOGS / "admin.log"
        interactive = False
        try:
            interactive = bool(sys.stdout) and sys.stdout.isatty()
        except Exception:
            interactive = False
        try:
            if interactive:
                popen = subprocess.Popen(
                    command, cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True, text=True, bufsize=1)
                threading.Thread(target=_pump, args=(popen.stdout, logfile, "admin"),
                                 daemon=True).start()
            else:
                handle = open(logfile, "a", encoding="utf-8", errors="replace")
                self._logfiles.append(handle)
                popen = subprocess.Popen(
                    command, cwd=str(ROOT), env=env,
                    stdout=handle, stderr=subprocess.STDOUT,
                    start_new_session=True, text=True, bufsize=1)
        except Exception as exc:  # noqa: BLE001 - 启动失败要给可读原因
            _log("[main] ❌ 启动 worker 失败：%s" % exc)
            return None
        return popen

    def _spawn_generation(self) -> bool:
        """开新一代：先登记这代的 IPC 地址与密钥，再 Popen，最后补 pid。"""
        self._generation += 1
        instance = "gen-" + secrets.token_hex(8)
        gen_dir = self.deploy_dir / ("gen-%s" % instance.rsplit("-", 1)[-1])
        try:
            prepared = control.prepare_control_secrets(gen_dir, instance_id=instance,
                                                       supervised=True)
        except Exception:  # noqa: BLE001 - 私有目录/密钥准备失败就不要起孩子
            _log("[main] ❌ 准备控制通道失败，本代不启动")
            return False
        record = {
            "version": 1,
            "kind": "workbuddy2api-generation",
            "instance_id": instance,
            "child_pid": None,
            "generation": self._generation,
            "control_dir": str(gen_dir),
            "authkey": prepared[control.AUTHKEY_ENV],
            "host": self.host,
            "port": self.port,
            "app_target": self.app_target,
            "owner_pid": os.getpid(),
            "owner_identity": self.identity,
            "spawned_at": time.time(),
        }
        # 先写记录：Popen 与"补 pid"之间被强杀时，下一位启动者仍能凭控制目录
        # 在命令行里找回这个孩子。
        self.generations.write(record)
        command = _worker_command(host=self.host, port=self.port,
                                  app_target=self.app_target, control_dir=gen_dir)
        env = os.environ.copy()
        env.update(prepared)
        _log("[main] 启动第 %d 代 worker：%s" % (self._generation, " ".join(command)))
        popen = self._launch_child(command, env)
        if popen is None:
            self.generations.clear(instance_id=instance, child_pid=None)
            return False
        record["child_pid"] = popen.pid
        self.generations.write(record)
        self._popen = popen
        self._child_pid = popen.pid
        self._instance_id = instance
        self._gen_started = time.monotonic()
        self._stop_sent_for = None
        self._force_requested = False
        self._force_done = False
        self._last_poll = 0.0
        self._child_status = {
            "phase": "starting", "action": None, "instance_id": instance,
            "operation_id": None, "active_requests": None, "active_jobs": None,
            "elapsed_ms": 0, "can_restart": True, "can_force": False,
        }
        try:
            self._owner = control.WorkerOwner(gen_dir, prepared[control.AUTHKEY_ENV],
                                              timeout=1.5, popen=popen)
        except ValueError:
            self._owner = None
        self.write_registry(phase_hint="starting")
        _write_pidfile()
        _log("[main] 第 %d 代 worker 已启动：pid %d，代号 %s"
             % (self._generation, popen.pid, instance))
        return True

    def _refresh_child_status(self) -> None:
        owner = self._owner
        if owner is None:
            return
        now = time.monotonic()
        if now - self._last_poll < CHILD_POLL_INTERVAL:
            return
        self._last_poll = now
        snapshot = owner.status(timeout=1.5)
        with self._lock:
            self._child_status = snapshot
        phase = snapshot.get("phase")
        if phase and phase not in ("unreachable", "stopped") \
                and phase != self._phase_hint:
            self.write_registry(phase_hint=phase)

    def _send_worker_intent(self) -> None:
        action = self._action
        owner = self._owner
        if action not in ("stop", "restart") or owner is None:
            return
        if self._stop_sent_for == self._operation_id:
            return
        phase = (self._child_status or {}).get("phase")
        if phase in ("draining", "stopping"):
            self._stop_sent_for = self._operation_id
            return
        operation = self._operation_id or ("%s-%s" % (action, secrets.token_hex(6)))
        try:
            receipt = owner.begin(action, operation, None, timeout=10.0)
        except control.ControlUnavailable as exc:
            _log("[main] 控制通道暂时不可用（%s）：继续等这一代给出结果" % exc.reason)
            return
        except control.ControlRefused as exc:
            _log("[main] worker 拒绝了 %s 意图（%s）：以它自己的状态为准"
                 % (action, exc.reason))
            self._stop_sent_for = self._operation_id
            return
        self._stop_sent_for = self._operation_id
        _log("[main] 已向 worker 递送 %s 意图（worker 回执 operation_id=%s）"
             % (action, receipt.get("operation_id")))

    def _apply_force(self) -> None:
        if not self._force_requested or self._popen is None:
            return
        if self._popen.poll() is not None:
            return
        remaining = self._child_status or {}
        _log("[main] 执行强制结束：pid %s，剩余 %s 个请求、%s 个后台任务"
             "（operation_id=%s）" % (self._child_pid,
                                    remaining.get("active_requests"),
                                    remaining.get("active_jobs"),
                                    self._operation_id))
        self._force_requested = False
        self._force_done = True
        if self._child_pid:
            _terminate_tree(int(self._child_pid))

    def _maybe_finish_restart(self) -> None:
        if self._action != "restart":
            return
        snapshot = self._child_status or {}
        if snapshot.get("phase") != "ready":
            return
        instance = snapshot.get("instance_id")
        if self._restart_from_instance and instance == self._restart_from_instance:
            return
        with self._lock:
            self._action = None
            self._operation_id = None
            self._operation_since = None
            self._restart_from_instance = None
        self.write_registry(phase_hint="ready")
        _log("[main] 新一代已就绪：instance_id=%s（第 %d 代）"
             % (instance, self._generation))

    def _handle_child_exit(self, code: int) -> None:
        ran = time.monotonic() - self._gen_started
        generation = self._generation
        instance = self._instance_id
        child_pid = self._child_pid
        planned = int(code) == PLANNED_RESTART_CODE
        try:
            self.generations.clear(instance_id=instance, child_pid=child_pid)
        except Exception:  # noqa: BLE001 - 清理失败不影响换代
            pass
        self._popen = None
        self._owner = None
        self._child_pid = None
        self._stop_sent_for = None
        self._child_status = {"phase": "stopped", "action": None,
                              "instance_id": instance, "operation_id": None,
                              "active_requests": 0, "active_jobs": 0,
                              "elapsed_ms": 0, "can_restart": True,
                              "can_force": False}
        if planned and self._intent != "stop":
            self._crashes = 0
            # 计划重启的意图在这一代已经落实：必须立刻清掉动作，否则新一代会被
            # 同一份意图再排空一次 —— 那会变成"重启风暴"（实测 10 代连环退出）。
            with self._lock:
                restarted = self._action == "restart"
                self._action = None
                self._operation_id = None
                self._operation_since = None
                self._restart_from_instance = None
            _log("[main] 第 %d 代 worker 已退出 code=%d（计划重启），跳过崩溃退避，"
                 "立即启动下一代%s" % (generation, code, "（重启请求已完成）" if restarted else ""))
            self._spawn_generation()
            return
        if planned:
            _log("[main] 第 %d 代 worker 已退出 code=%d（计划重启），"
                 "但所有者已决定停机" % (generation, code))
            self._finished = True
            self._exit_code = EXIT_OK
            return
        if self._intent == "stop" or self._action == "stop":
            _log("[main] 第 %d 代 worker 已退出 code=%d（停止流程内），不再拉起"
                 % (generation, code))
            self._finished = True
            self._exit_code = EXIT_OK
            return
        delay, self._crashes = _next_delay(int(code), ran, self._crashes)
        _log("[main] ⚠️ 第 %d 代 worker 已退出 (code=%d, 存活 %.0fs)，判定为崩溃，"
             "%s 后自动重启（第 %d 次）…"
             % (generation, code, ran, ("%.0fs" % delay) if delay else "立即",
                self._crashes or 1))
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline and self._intent != "stop":
            time.sleep(0.1)
        if self._intent == "stop":
            self._finished = True
            self._exit_code = EXIT_OK
            return
        self._spawn_generation()

    def finalize(self) -> int:
        """唯一的清理点：停控制端点、按身份清登记/代记录/PID 文件（幂等）。"""
        with self._lock:
            if self._finalized:
                return self._exit_code
            self._finalized = True
            self._finished = True
        control_server = self._control
        self._control = None
        if control_server is not None:
            try:
                control_server.stop()
            except Exception:  # noqa: BLE001 - 关端点失败不影响退出
                pass
        popen = self._popen
        child_pid = self._child_pid
        self._popen = None
        if popen is not None and popen.poll() is None:
            # 走到这里说明主循环已决定退出（例如收到强退确认）但孩子还在：
            # 这是我们自己持有的进程树，只结束这一个。
            if child_pid:
                _terminate_tree(int(child_pid))
            try:
                popen.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.generations.clear(instance_id=self._instance_id,
                                   child_pid=child_pid or (popen.pid if popen else 0))
        except Exception:  # noqa: BLE001
            pass
        try:
            self.registry.clear(owner_pid=os.getpid(), identity=self.identity)
        except Exception:  # noqa: BLE001
            pass
        _remove_pidfile()
        for handle in self._logfiles:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass
        self._logfiles = []
        _log("[main] 所有者已退出：pid %d，身份 %s，退出码 %d"
             % (os.getpid(), self.identity, self._exit_code))
        return self._exit_code



# ---------------------------------------------------------------------------
# 本地命令行：--status / --stop / --restart
#
# 原则：先问所有者状态，再决定做什么；"监听这个端口"永远不能单独用来推导
# 进程归属，任何强退都必须绑定当前操作并显式确认。
# ---------------------------------------------------------------------------

def _resolve_port_arg(args):
    value = getattr(args, "port", None)
    return int(value) if value is not None else None


def _default_port() -> int:
    try:
        return int(os.getenv("ADMIN_PORT", "8790"))
    except (TypeError, ValueError):
        return 8790


def _select_owner(port):
    """返回 (选中的所有者登记, 本部署所有活着的主体)。

    多实例同时运行且没指定端口时不猜：让调用方用 --port 说明要对谁操作。
    """
    live = [record for record in _iter_registries()
            if _record_is_ours(record) and _owner_process_matches(record)]
    if port is not None:
        for record in live:
            try:
                recorded = int(record.get("port") or 0)
            except (TypeError, ValueError):
                recorded = 0
            if recorded == int(port):
                return record, live
        return None, live
    if len(live) == 1:
        return live[0], live
    return None, live


def _status_payload(record, port) -> dict:
    if not record:
        return {
            "phase": "none", "action": None, "instance_id": None,
            "operation_id": None, "active_requests": None, "active_jobs": None,
            "elapsed_ms": 0, "can_restart": False, "can_force": False,
            "owner_pid": None, "child_pid": None, "generation": 0,
            "host": None, "port": int(port), "control": "none",
            "intent": None, "control_address": None, "registry": None,
        }
    state, snapshot = _query_owner(record)
    payload = dict(snapshot or {})
    payload.update({
        "owner_pid": record.get("owner_pid"),
        "child_pid": payload.get("child_pid", record.get("child_pid")),
        "generation": payload.get("generation", record.get("generation")),
        "host": record.get("host"),
        "port": int(record.get("port") or port),
        "control": state,
        "control_address": record.get("control_address"),
        "workdir": record.get("workdir"),
        "registry": record.get("_path"),
        "deployment": str(_deployment_dir(int(record.get("port") or port))),
    })
    if state != "ok":
        payload["phase"] = "unreachable"
        payload.setdefault("can_force", False)
        payload.setdefault("can_restart", False)
    return payload


def _cmd_status(args) -> int:
    port_arg = _resolve_port_arg(args)
    record, live = _select_owner(port_arg)
    if record is None and len(live) > 1 and port_arg is None:
        _log("[main] 本部署有多个所有者，请用 --port 指定要查哪一个：")
        for item in live:
            _log("        --port %s（pid %s）" % (item.get("port"), item.get("owner_pid")))
        return EXIT_REFUSED
    if record is not None:
        port = int(record.get("port") or port_arg or _default_port())
    else:
        port = int(port_arg or _default_port())
    if getattr(args, "json", False):
        # 只输出一行 JSON：把第三方初始化时可能的 print（admin 包初始化会打
        # 环境告警）改道到 stderr，否则脚本解析会被这行噪音带偏。
        with contextlib.redirect_stdout(sys.stderr):
            payload = _status_payload(record, port)
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    else:
        payload = _status_payload(record, port)
        _log("[main] 端口 %d：%s（阶段 %s）"
             % (port, _phase_word(payload.get("phase")), payload.get("phase")))
        if record is not None:
            _log("        owner pid %s · 第 %s 代 · 孩子 pid %s"
                 % (payload.get("owner_pid"), payload.get("generation"),
                    payload.get("child_pid")))
            if payload.get("operation_id"):
                _log("        进行中的操作 operation_id=%s · 剩余 %s 个请求、%s 个后台任务"
                     % (payload.get("operation_id"), payload.get("active_requests"),
                        payload.get("active_jobs")))
            _print_endpoints(record.get("host") or "127.0.0.1", port)
    if record is None:
        return EXIT_NO_OWNER
    if payload.get("control") != "ok":
        return EXIT_UNREACHABLE
    if payload.get("phase") == "ready":
        return EXIT_OK
    return EXIT_BUSY


def _stop_owner(record, args) -> int:
    """停止**整个所有者**：先记录停机意图，再请 worker 排空，等它真退出。"""
    port = int(record.get("port") or _default_port())
    host = record.get("host") or "127.0.0.1"
    owner_pid = int(record.get("owner_pid") or 0)
    client = _owner_client(record, timeout=2.0)
    if client is None:
        _log("[main] 既有实例的控制凭证缺失：保留它，不做处置。")
        return EXIT_UNREACHABLE
    force = bool(getattr(args, "force", False))
    confirm = bool(getattr(args, "confirm", False))
    try:
        current = client.status()
    except Exception as exc:  # noqa: BLE001 - 只是这次问不到
        _log("[main] 控制通道暂时不可用（%s）：保留原实例，不做破坏性处置。"
             % type(exc).__name__)
        return EXIT_UNREACHABLE
    if (current or {}).get("phase") == "unreachable":
        _log("[main] 所有者状态暂不可用（pid %d）：保留它，不做破坏性处置。" % owner_pid)
        return EXIT_UNREACHABLE
    operation = (current or {}).get("operation_id")
    if force:
        remaining = (current or {})
        if not confirm:
            _log("[main] 强制结束必须显式确认。当前操作 operation_id=%s，"
                 "剩余 %s 个请求、%s 个后台任务"
                 % (operation, remaining.get("active_requests"),
                    remaining.get("active_jobs")))
            _log("[main]   确认要中断这些工作再执行："
                 "python main.py --stop --port %d --force --confirm" % port)
            return EXIT_REFUSED
        if not operation:
            _log("[main] 当前没有进行中的操作，不需要强制结束。"
                 "普通停止：python main.py --stop --port %d" % port)
            return EXIT_REFUSED
        try:
            client.force(operation, remaining.get("instance_id") or "", True)
        except control.ControlUnavailable as exc:
            _log("[main] 控制通道暂时不可用（%s）：保留原实例。" % exc.reason)
            return EXIT_UNREACHABLE
        except control.ControlRefused as exc:
            _log("[main] 强制结束被拒绝（%s）：以所有者当前状态为准。" % exc.reason)
            return EXIT_REFUSED
        _log("[main] 已请求强制结束 operation_id=%s（绑定当前代 %s）"
             % (operation, remaining.get("instance_id") or "-"))
    else:
        try:
            receipt = client.begin("stop", "stop-" + secrets.token_hex(6))
        except control.ControlUnavailable as exc:
            _log("[main] 控制通道暂时不可用（%s）：保留原实例，不做破坏性处置。"
                 % exc.reason)
            return EXIT_UNREACHABLE
        except control.ControlRefused as exc:
            snapshot = exc.snapshot or {}
            _log("[main] 所有者拒绝了停止请求（%s）：当前操作 operation_id=%s、阶段 %s"
                 % (exc.reason, snapshot.get("operation_id"), snapshot.get("phase")))
            _log("[main]    → 等它完成，或确认后强退："
                 "python main.py --stop --port %d --force --confirm" % port)
            return EXIT_REFUSED
        operation = receipt.get("operation_id") or operation
        _log("[main] 已受理停止请求 operation_id=%s（所有者 pid %d，第 %s 代）"
             % (operation, owner_pid, (current or {}).get("generation") or "-"))

    started = time.monotonic()
    limit = 0.0
    try:
        limit = float(getattr(args, "wait_timeout", 0) or 0)
    except (TypeError, ValueError):
        limit = 0.0
    last_note = 0.0
    while True:
        if not _pid_alive(owner_pid):
            break
        now = time.monotonic()
        if last_note == 0.0 or now - last_note >= 1.0:
            last_note = now
            live = {}
            try:
                live = client.status()
            except Exception:  # noqa: BLE001 - 退出边缘问不到很正常
                live = {}
            _log("[main]   %s · 剩余 %s 个请求、%s 个后台任务 · 已等待 %.0f 秒 · "
                 "operation_id=%s"
                 % (_phase_word(live.get("phase")), live.get("active_requests"),
                    live.get("active_jobs"), now - started, operation))
        if limit and (now - started) > limit:
            _log("[main] 仍在排空：没有强退，也没有结束任何进程。"
                 "稍后再查： python main.py --status --port %d" % port)
            return EXIT_BUSY
        time.sleep(0.5)
    if not _wait_port_released(host, port, 10.0):
        _log("[main] 所有者进程已退出，但端口 %d 仍被占用：请检查是否有其它程序在监听。"
             % port)
        return EXIT_UNMANAGED
    _log("[main] 已停止（所有者 pid %d 退出，端口 %d 已释放）" % (owner_pid, port))
    return EXIT_OK


def _cmd_stop(args) -> int:
    port_arg = _resolve_port_arg(args)
    _load_control()
    record, live = _select_owner(port_arg)
    if record is not None:
        return _stop_owner(record, args)
    if len(live) > 1 and port_arg is None:
        _log("[main] 本部署有多个所有者，请用 --port 指定要停哪一个：")
        for item in live:
            _log("        --port %s（pid %s）" % (item.get("port"), item.get("owner_pid")))
        return EXIT_REFUSED
    port = int(port_arg or _default_port())
    outcome = _drain_orphan(port, origin="stop", wait=_orphan_wait(args))
    if outcome == "drained":
        _wait_port_released("127.0.0.1", port, 10.0)
        _log("[main] 已停止（上一代 worker 已正常排空退出，端口 %d 已释放）" % port)
        return EXIT_OK
    if outcome == "draining":
        _log("[main] 上一代 worker 仍在排空：没有强退，稍后再查："
             "python main.py --status --port %d" % port)
        return EXIT_BUSY
    if outcome == "unreachable":
        _log("[main] 上一代 worker 还活着但状态暂不可用：保留它，不做处置。")
        return EXIT_UNREACHABLE
    _log("[main] 没有找到运行中的所有者（端口 %d）" % port)
    return EXIT_NO_OWNER


def _restart_owner(record, args) -> int:
    """只重启 worker：所有者继续活着，旧代排空退出后开下一代。"""
    port = int(record.get("port") or _default_port())
    client = _owner_client(record, timeout=2.0)
    if client is None:
        _log("[main] 既有实例的控制凭证缺失：保留它，不做处置。")
        return EXIT_UNREACHABLE
    try:
        before = client.status()
    except Exception as exc:  # noqa: BLE001
        _log("[main] 既有实例状态暂不可用（%s）：保留它，不做处置。"
             % type(exc).__name__)
        return EXIT_UNREACHABLE
    if (before or {}).get("phase") == "unreachable":
        _log("[main] 既有实例状态暂不可用：保留它，不做处置。")
        return EXIT_UNREACHABLE
    old_instance = (before or {}).get("instance_id")
    operation = "restart-" + secrets.token_hex(6)
    try:
        receipt = client.begin("restart", operation)
    except control.ControlUnavailable as exc:
        _log("[main] 控制通道暂时不可用（%s）：保留原实例。" % exc.reason)
        return EXIT_UNREACHABLE
    except control.ControlRefused as exc:
        snapshot = exc.snapshot or {}
        _log("[main] 所有者拒绝了重启请求（%s）：当前操作 operation_id=%s、阶段 %s"
             % (exc.reason, snapshot.get("operation_id"), snapshot.get("phase")))
        _log("[main]    → 等它完成，或停止整个所有者："
             "python main.py --stop --port %d" % port)
        return EXIT_REFUSED
    operation = receipt.get("operation_id") or operation
    _log("[main] 已受理重启请求 operation_id=%s（原 instance_id=%s）"
         % (operation, old_instance or "-"))
    deadline = time.monotonic() + _orphan_wait(args)
    last_note = 0.0
    while True:
        if not _pid_alive(int(record.get("owner_pid") or 0)):
            _log("[main] 所有者已退出：本次重启没有完成，服务当前不在运行。")
            return EXIT_BUSY
        current = {}
        try:
            current = client.status()
        except Exception:  # noqa: BLE001 - 换代空档里问不到很正常
            current = {}
        phase = current.get("phase")
        if phase == "ready" and current.get("instance_id") != old_instance:
            _log("[main] 新一代已就绪：instance_id=%s（第 %s 代）"
                 % (current.get("instance_id"), current.get("generation")))
            _print_endpoints(record.get("host") or "127.0.0.1", port)
            return EXIT_OK
        now = time.monotonic()
        if last_note == 0.0 or now - last_note >= 1.0:
            last_note = now
            _log("[main]   %s · 剩余 %s 个请求、%s 个后台任务 · operation_id=%s"
                 % (_phase_word(phase), current.get("active_requests"),
                    current.get("active_jobs"), operation))
        if now > deadline:
            _log("[main] 重启仍在进行（没有强退）。稍后查看："
                 "python main.py --status --port %d" % port)
            return EXIT_BUSY
        time.sleep(0.5)


def _report_existing(record, args) -> int:
    """第二次普通启动：显示"既有实例正在…"与连接信息后返回，绝不动它。"""
    port = int(record.get("port") or _default_port())
    state, snapshot = _query_owner(record)
    phase = (snapshot or {}).get("phase") if state == "ok" else "unreachable"
    _log("[main] 既有实例%s（pid %s，第 %s 代，阶段 %s）"
         % (_phase_word(phase), record.get("owner_pid"),
            (snapshot or {}).get("generation") or "-", phase))
    _print_endpoints(record.get("host") or "127.0.0.1", port)
    if state != "ok":
        _log("[main]   控制通道暂时不可用：状态暂不可用。未做任何处置，原实例继续运行。")
        _log("[main]   稍后再查： python main.py --status --port %d" % port)
        return EXIT_UNREACHABLE
    operation = (snapshot or {}).get("operation_id")
    if operation:
        _log("[main]   进行中的操作 operation_id=%s（剩余 %s 个请求、%s 个后台任务）"
             % (operation, (snapshot or {}).get("active_requests"),
                (snapshot or {}).get("active_jobs")))
    _log("[main]   停止服务  : python main.py --stop --port %d" % port)
    _log("[main]   重启 worker: python main.py --restart --port %d" % port)
    _log("[main] 未启动新的实例。")
    if phase == "ready":
        return EXIT_OK
    return EXIT_BUSY


def _handle_busy_port(host: str, port: int, force: bool = False) -> int:
    """端口被占时的处置：只做诊断，不据"监听此端口"推导归属。

    本项目的活实例与支持协议的孤儿已经在更早的分支处理过；走到这里还占着端口
    的，要么是别的程序，要么是没有控制协议的旧版进程 —— 两种都**不默认杀**。
    返回 EXIT_OK 表示可以继续启动，其它值表示本次到此为止。
    """
    if not _port_in_use(host, port):
        return EXIT_OK
    pids = _listener_pids(port)
    if not pids:
        _log("❌ 端口 %d 已被占用，但无法识别占用进程的 pid（可能权限不足，"
             "试试以管理员/root 运行）。" % port)
        _log("    → 或改用其他端口： python main.py --port %d" % (port + 1))
        return EXIT_PORT_FOREIGN
    commands = {pid: _cmdline_of(pid) for pid in pids}
    ancestors = _ancestor_pids() | {os.getpid()}
    danger = [pid for pid in pids if pid in ancestors]
    if danger:
        _log("❌ 端口 %d 的占用进程 %s 是本进程的祖先（终端/启动器），"
             "拒绝结束以免杀掉你自己。" % (port, danger))
        _log("    → 请手动处理，或改用其他端口。")
        return EXIT_PORT_FOREIGN
    ours = [pid for pid in pids if _looks_like_our_service(commands.get(pid, ""))]
    if force:
        _log("[main] ⚠️  --force：结束端口 %d 上的占用进程 %s（会中断在途工作）"
             % (port, pids))
        for pid in pids:
            _log("    → 结束 pid %d" % pid)
            _terminate_tree(pid)
        if not _wait_port_released(host, port, _RESTART_WAIT):
            _log("❌ 端口 %d 在 %.0fs 内仍未释放，本次不启动。"
                 % (port, _RESTART_WAIT))
            return EXIT_PORT_FOREIGN
        _log("[main] 端口已释放，继续启动。")
        return EXIT_OK
    if ours:
        _print_manual_recovery(
            ours[0], port,
            reason="端口 %d 仍被本项目的旧实例占用（没有控制协议，"
                   "无法请求它正常排空）。" % port)
        return EXIT_UNMANAGED
    _log("❌ 端口 %d 已被占用，无法启动（占用者不是本项目进程）。" % port)
    for pid in pids:
        _log(_describe_pid(pid))
    _log("    → 确认它确实该停，再强制接管： python main.py --force")
    _log("    → 或改用其他端口： python main.py --port %d" % (port + 1))
    _log("    提示：同一端口只应由一个管理器负责（systemd 或宝塔面板，二选一）。")
    return EXIT_PORT_FOREIGN


def _cmd_start(args) -> int:
    """普通启动 / --restart：先问所有者状态，再决定是报状态、重启还是启动。"""
    host, port = _resolve_host_port(args)
    live = _find_live_owner(port)
    if live is not None:
        if bool(getattr(args, "restart", False)):
            return _restart_owner(live, args)
        return _report_existing(live, args)
    removed = _clear_stale_registries(port)
    if removed:
        _log("[main] 清理了已退出的所有者登记：%s" % ", ".join(removed))
    origin = "restart" if bool(getattr(args, "restart", False)) else "start"
    outcome = _drain_orphan(port, origin=origin, wait=_orphan_wait(args))
    if outcome == "unreachable":
        _print_manual_recovery(
            None, port,
            reason="上一代 worker 还活着，但控制通道暂时不可用（状态暂不可用）。")
        return EXIT_UNREACHABLE
    if outcome == "draining":
        _log("[main] 上一代 worker 仍在排空：本次不接管，也不强杀。")
        _log("[main]   稍后再试，或查看状态： python main.py --status --port %d" % port)
        return EXIT_BUSY
    if outcome == "drained":
        if not _wait_port_released(host, port, _RESTART_WAIT):
            _print_manual_recovery(
                None, port,
                reason="上一代已退出，但端口 %d 在 %.0fs 内仍被占用。"
                       % (port, _RESTART_WAIT))
            return EXIT_UNMANAGED
        _log("[main] 已接管端口 %d。" % port)
    busy = _handle_busy_port(host, port, force=bool(getattr(args, "force", False)))
    if busy != EXIT_OK:
        return busy

    # 部署安全检查（沿用原行为：只告警，不阻止启动）
    secret = os.getenv("ADMIN_JWT_SECRET", "")
    if not secret or secret.startswith("workbuddy-admin-jwt-secret-please-change"):
        _log("⚠️  未设置强 ADMIN_JWT_SECRET，将使用默认弱密钥 —— "
             "部署请务必通过环境变量覆盖！")
    if not os.getenv("ADMIN_PASSWORD", "") or not os.getenv("ADMIN_USERNAME", ""):
        _log("⚠️  未配置 ADMIN_USERNAME / ADMIN_PASSWORD，后台登录将返回 503。")
        _log("    请在 .env 中设置这两项后重启（已不再提供 admin/admin123 默认口令）。")

    supervisor = Supervisor(host=host, port=port, app_target=_app_target(), args=args)
    _log("[main] 单端口服务启动中：")
    _print_endpoints(host, port)
    _log("[main] 按 Ctrl+C 停止（会先排空在途请求，不强退）。")
    return supervisor.run()


def main(argv=None) -> int:
    # 依赖自检：当前解释器缺包则自动切换到带依赖的虚拟环境
    # （修复系统 Python 缺 pymysql 导致启动即崩退出）
    py = _interpreter_with_deps()
    if py is None:
        _log("❌ 当前 Python 缺少依赖：" + ", ".join(_REQUIRED_DEPS))
        _log("   请先安装：pip install -r requirements.txt，"
             "或激活已装好依赖的虚拟环境后再运行。")
        return EXIT_MISSING_DEPS
    if py != sys.executable:
        _log("[main] 当前解释器缺依赖，自动改用虚拟环境：%s" % py)
        forward = list(sys.argv[1:] if argv is None else argv)
        os.execv(py, [py, os.path.abspath(__file__)] + forward)

    ap = argparse.ArgumentParser(
        description="workbuddy2api 一键启动（单端口：管理后台 + 内嵌网关）")
    # 默认值设为 None：用来区分"命令行显式指定"与"没写、应回落到 .env"。
    # 若默认写成 0.0.0.0/8790，就分不清用户是否真的要这个值，也就无法让
    # 命令行压过 .env（见 _resolve_host_port）。
    ap.add_argument("--host", default=None,
                    help="监听地址（默认取 ADMIN_HOST，再默认 0.0.0.0）")
    ap.add_argument("--port", type=int, default=None,
                    help="服务端口（默认取 ADMIN_PORT，再默认 8790）")
    ap.add_argument("--restart", action="store_true",
                    help="对运行中的本项目实例请求正常重启（旧代排空退出后再开下一代）；"
                         "没有实例时等价于普通启动")
    ap.add_argument("--force", action="store_true",
                    help="强制：启动时结束占用端口的进程；与 --stop 一起用时需要 --confirm")
    ap.add_argument("--confirm", action="store_true",
                    help="与 --stop --force 一起用：确认中断当前一代的在途工作")
    ap.add_argument("--stop", action="store_true",
                    help="停止本部署运行中的所有者（先排空，不强杀）")
    ap.add_argument("--status", action="store_true",
                    help="查询本部署所有者状态（脚本探活用；配 --json 输出一行 JSON）")
    ap.add_argument("--json", action="store_true",
                    help="与 --status 一起用：只输出一行 JSON")
    ap.add_argument("--wait-timeout", type=float, default=0.0,
                    help="等待排空/重启的最长秒数；0（默认）表示不限，绝不自动强退")
    args = ap.parse_args(argv)

    if args.stop:
        return _cmd_stop(args)
    if args.status:
        return _cmd_status(args)
    return _cmd_start(args)

if __name__ == "__main__":  # pragma: no cover - 脚本入口
    sys.exit(main())
