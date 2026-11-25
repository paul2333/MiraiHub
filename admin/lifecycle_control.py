"""本机控制通道：worker 与"进程所有者"之间的排空 / 重启协商。

契约见 docs/superpowers/plans/2026-11-22-graceful-shutdown-and-shell-CONTRACT.md
第 3 节；行为语义见计划任务 3。

为什么需要它
------------
Web 上的"重启服务"以前是路由里 os._exit(0)：正在落库的工作被截断，父进程
也不知道这次退出是"用户要求的重启"还是"崩了"。这里把"要不要停"和"什么时候
停"变成一次**可观察、可回执**的协商：

1. 父进程（桌面壳 / main.py 监督器）为每一代孩子准备**仅本机可用**的端点与
   随机 authkey，只经进程环境交给孩子；
2. worker 在**接受业务之前**就建好端点，之后无论是 Web 路由还是父进程，都走
   同一套 BEGIN/FORCE/RESUME 逻辑（LocalControl）；
3. 权威结果永远来自 worker 在锁内返回的快照，父进程只负责按退出码决定
   要不要开下一代。

安全边界
--------
* 端点仅本机：Windows 用命名管道（AF_PIPE），Unix 用私有目录（0700）里的
  Unix 域套接字（0600）。
* 单条消息上限 64 KiB，连接与读取都有小超时；超时只意味着"暂时观测不到"，
  不代表进程已退出。
* authkey 只存在于进程环境 / 受保护运行目录，**绝不**进浏览器、HTTP 参数、
  响应体或日志。
* 控制连接与状态查询都不计入在途数量：只有 worker 自己会因为真实的业务请求
  或本地 job 改动计数。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import secrets
import subprocess
import threading
import time
from multiprocessing.connection import AuthenticationError, Client, Listener
from pathlib import Path

logger = logging.getLogger("admin.lifecycle_control")

#: 控制目录（保持名称与契约一致）。父进程创建、孩子只读。
CONTROL_DIR_ENV = "WB_LIFECYCLE_CONTROL_DIR"
#: 随机 authkey（hex）。只经父进程环境传入，禁止落日志。
AUTHKEY_ENV = "WB_LIFECYCLE_AUTHKEY"
#: 本代实例标识（随机不透明串，不充当凭据）。
INSTANCE_ENV = "WB_LIFECYCLE_INSTANCE_ID"
#: 由"会负责拉起下一代"的父进程置 1；没有它就说明本次运行没有重启所有者，
#: Web 重启必须在封口前返回 409 且 can_restart=false。
SUPERVISED_ENV = "WB_LIFECYCLE_SUPERVISED"

#: 单条 IPC 消息上限（契约要求 64 KiB）。
MAX_MESSAGE_BYTES = 64 * 1024
#: 连接 / 读取小超时。默认 2 秒：只够"本机 + 正常负载"，不掩盖真实失联。
DEFAULT_TIMEOUT = 2.0
#: 控制线程等待事件循环给出权威结果的上限；必须小于客户端读取超时，
#: 这样客户端总能收到一条"我暂时给不出答案"的回执，而不是干等。
DISPATCH_TIMEOUT = 1.5
#: 同时处理的控制连接上限（正常只有 1~2 条）。
MAX_CONTROL_CONNECTIONS = 8

#: 升级预排空（upgrade_hold）的租期上限：与升级标记的 180 秒硬约束一致。
#: 到期仍未被提交时，worker 核实自己还是"同一未提交 hold"就自动恢复准入，
#: 避免发布 CLI 崩溃后永久封口。
MAX_HOLD_LEASE = 180.0
#: 没带 hold_until 时用的默认租期。
DEFAULT_HOLD_LEASE = 180.0

#: Unix 私有目录里的套接字文件名。
SOCKET_NAME = "worker.sock"
#: Windows 命名管道名前缀（每代按控制目录派生，天然按"部署 + 端口"区分）。
PIPE_PREFIX = "wb-lifecycle-"

#: 允许的动作。upgrade_hold 是任务 7 的升级预排空：封口并等归零后**保持可查询**，
#: 只有发布方明确提交（同一操作从 hold 升级为 stop）才退出。
ALLOWED_ACTIONS = ("restart", "stop", "update", "upgrade_hold")
#: 计划重启用到的动作：父进程据此决定是否开下一代。
PLANNED_ACTIONS = ("restart", "update")
#: 封口之后的阶段。
SEALED_PHASES = ("draining", "stopping")

_KINDS = ("STATUS", "BEGIN", "FORCE", "RESUME")
_REQUIRED_FIELDS = {
    "STATUS": (),
    "BEGIN": ("action", "operation_id"),
    "FORCE": ("operation_id", "instance_id", "confirm"),
    "RESUME": ("operation_id", "instance_id"),
}
_OPTIONAL_FIELDS = {
    "STATUS": ("instance_id",),
    "BEGIN": ("instance_id", "source", "hold_until"),
    "FORCE": (),
    "RESUME": (),
}
#: 这些字段是"代号"，只校验形状，不当作凭据。
_TEXT_FIELDS = {"action": 32, "operation_id": 128, "instance_id": 128, "source": 32}


# ---------------------------------------------------------------------------
# 端点地址与环境变量
# ---------------------------------------------------------------------------

def control_family() -> str:
    """Windows 只有命名管道可用；其它平台一律 Unix 域套接字。"""
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def control_address(control_dir: Path) -> str:
    """由控制目录派生端点地址。

    每代一个目录 -> 每代一个端点。这样"前一代迟来的 force"连不到新代的
    端点；即使父进程复用了目录，instance_id 校验也会把它挡掉。
    """
    control_dir = Path(control_dir)
    if os.name == "nt":
        # 命名管道名不区分大小写、且必须落在 \\.\pipe\ 下；用目录路径的哈希
        # 保证"同一部署同一端口"稳定、"不同代不同目录"互不干扰。
        digest = hashlib.sha256(str(control_dir).lower().encode("utf-8")).hexdigest()[:20]
        return "\\\\.\\pipe\\" + PIPE_PREFIX + control_dir.name[:24] + "-" + digest
    return str(control_dir / SOCKET_NAME)


def default_control_dir(tag: str = "") -> Path:
    """按部署 / 端口区分的默认私有目录。

    Windows 落在当前用户的 LOCALAPPDATA 下（受限目录 + ACL），Unix 落在
    带 uid 的临时目录（0700）。tag 建议写成 <port>-<instance>。
    """
    if os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        base = root / "MiraiHub" / "run"
    else:
        base = Path(os.getenv("XDG_RUNTIME_DIR") or "/tmp") / ("miraihub-%d" % os.getuid())
    return base / (tag or "default")


def _restrict_windows_acl(path: Path) -> None:
    """把目录收敛到"当前用户可访问"（继承关闭，只授予本用户）。

    失败只是少一层加固，不影响功能，因此只记 debug —— 这里刻意不抛异常，
    免得在没有 icacls 的极简环境里让服务起不来。
    """
    user = os.getenv("USERNAME") or ""
    if not user:
        return
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", user + ":(OI)(CI)F"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("控制目录 ACL 收敛失败（忽略）")


def _secure_dir(path: Path) -> Path:
    """创建私有目录：Unix 0700；Windows 关继承 + 只授当前用户。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        _restrict_windows_acl(path)
    else:
        os.chmod(path, 0o700)
    return path


def _secure_socket(path: Path) -> None:
    """Unix 套接字文件 0600（目录已经 0700，双保险）。"""
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("控制套接字权限收敛失败（忽略）")


def prepare_control_secrets(control_dir: Path, instance_id: str | None = None, *,
                            supervised: bool = True) -> dict[str, str]:
    """为**一代** worker 准备私有目录与随机 authkey，返回要注入的环境变量。

    调用方（桌面父进程 / 监督器）拿着同一份 control_dir 与 authkey 构造
    WorkerOwner；孩子只从环境里读，不落盘、不进日志。
    """
    control_dir = _secure_dir(control_dir)
    if os.name != "nt":
        # 上一代若异常退出会留下套接字文件；先清掉，避免 bind 失败。
        try:
            (control_dir / SOCKET_NAME).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("清理旧控制套接字失败（忽略）")
    env = {
        CONTROL_DIR_ENV: str(control_dir),
        AUTHKEY_ENV: secrets.token_hex(32),
    }
    if instance_id:
        env[INSTANCE_ENV] = instance_id
    if supervised:
        env[SUPERVISED_ENV] = "1"
    return env


class ControlEnv:
    """worker 侧读到的控制通道配置（全部来自进程环境）。"""

    __slots__ = ("control_dir", "authkey", "instance_id", "supervised")

    def __init__(self, control_dir: Path | None, authkey: bytes | None,
                 instance_id: str | None, supervised: bool) -> None:
        self.control_dir = control_dir
        self.authkey = authkey
        self.instance_id = instance_id
        self.supervised = supervised

    @property
    def enabled(self) -> bool:
        return self.control_dir is not None and self.authkey is not None

    def __repr__(self) -> str:  # 永不带出 authkey
        return ("ControlEnv(control_dir=%r, authkey=<redacted>, instance_id=%r, "
                "supervised=%r)" % (str(self.control_dir), self.instance_id, self.supervised))


def read_control_env(env=None) -> ControlEnv:
    """从环境变量还原控制通道配置；缺失或损坏时 enabled 为 False。"""
    env = os.environ if env is None else env
    raw_dir = (env.get(CONTROL_DIR_ENV) or "").strip()
    raw_key = (env.get(AUTHKEY_ENV) or "").strip()
    instance = (env.get(INSTANCE_ENV) or "").strip() or None
    supervised = (env.get(SUPERVISED_ENV) or "").strip() == "1"
    key: bytes | None = None
    if raw_key:
        try:
            key = bytes.fromhex(raw_key)
        except ValueError:
            logger.warning("控制通道 authkey 形状非法，控制通道关闭")
            key = None
    return ControlEnv(Path(raw_dir) if raw_dir else None, key, instance, supervised)


# ---------------------------------------------------------------------------
# 消息编解码与校验
# ---------------------------------------------------------------------------

def encode_message(message: dict) -> bytes:
    """紧凑 JSON 编码；不加空白，保证体积最小。"""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_message(raw: bytes) -> dict:
    """解码并做形状校验；不合规抛 ValueError（调用方回 error 回执）。"""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("消息必须是字节串")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ValueError("消息超过 64 KiB 上限")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("消息不是合法 JSON") from exc
    if not isinstance(message, dict):
        raise ValueError("消息必须是 JSON 对象")
    return message


def validate_message(message: dict) -> str:
    """校验 kind / 字段集合 / 字段形状；返回空串表示通过。"""
    kind = message.get("kind")
    if kind not in _KINDS:
        return "未知的 kind"
    allowed = {"kind"}
    allowed.update(_REQUIRED_FIELDS[kind])
    allowed.update(_OPTIONAL_FIELDS[kind])
    extra = set(message) - allowed
    if extra:
        return "包含未定义字段"
    for name in _REQUIRED_FIELDS[kind]:
        if name not in message:
            return "缺少字段 " + name
    for name, limit in _TEXT_FIELDS.items():
        value = message.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value or len(value) > limit:
            return "字段 " + name + " 非法"
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            return "字段 " + name + " 含控制字符"
    if kind == "BEGIN" and message["action"] not in ALLOWED_ACTIONS:
        return "不支持的动作"
    if "hold_until" in message:
        if message.get("action") != "upgrade_hold":
            return "hold_until 只用于 upgrade_hold"
        value = message["hold_until"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "hold_until 必须是数字（epoch 秒）"
        if not math.isfinite(float(value)) or float(value) <= 0:
            return "hold_until 非法"
    if kind == "FORCE" and message.get("confirm") is not True:
        return "confirm 必须为 true"
    return ""


def _error_reply(reason: str, message: str = "", snapshot: dict | None = None) -> dict:
    return {"ok": False, "accepted": False, "reason": reason, "message": message,
            "snapshot": snapshot or {}}


# ---------------------------------------------------------------------------
# worker 侧：LocalControl（HTTP 路由与 IPC 共用的同一套判定）
# ---------------------------------------------------------------------------

class LocalControl:
    """进程内的权威状态机：Web 重启、父进程 BEGIN/FORCE/RESUME 都走这里。

    约定
    ----
    * 所有改变状态的方法都**在事件循环线程**上执行（HTTP handler 本来就在；
      IPC 由 dispatch() 用 loop.call_soon_threadsafe 投递）。
    * 权威快照始终来自 lifecycle（任务 1 的锁内结果）；这里只补两件
      lifecycle 不掌握的信息：本代有没有重启所有者（can_restart）与
      upgrade_hold 的提交/撤销。
    """

    def __init__(self, lifecycle, *, instance_id: str | None = None,
                 can_restart: bool = False, on_sealed=None, on_force=None,
                 on_resume=None) -> None:
        self._lifecycle = lifecycle
        self.instance_id = instance_id
        self.can_restart = bool(can_restart)
        self._on_sealed = on_sealed
        self._on_force = on_force
        self._on_resume = on_resume
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sealed = asyncio.Event()
        self._response_gate = asyncio.Event()
        self._gate_armed = False
        self._committed = asyncio.Event()
        self._resumed = asyncio.Event()
        self._op_action: str | None = None
        self._op_id: str | None = None
        self._op_source: str | None = None
        self._hold_committed = False
        self._hold_resumed = False
        self._hold_deadline: float | None = None
        self._hold_timer = None
        #: 最近一次成功 RESUME 的 operation_id：同一次撤销重复投递要幂等。
        self._resumed_op: str | None = None
        self._forced: tuple[str, str] | None = None

    # -- 生命周期 -----------------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def lifecycle(self):
        return self._lifecycle

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    # -- 快照 ---------------------------------------------------------------
    def current_instance_id(self) -> str | None:
        snapshot = self._lifecycle.snapshot()
        return snapshot.get("instance_id") or self.instance_id

    def acceptable_instance_ids(self) -> set[str]:
        """本代可以接受的两个代号。

        lifecycle 在 lifespan 启动时 reset() 会自己生成 instance_id，可能与
        拉起方注入的环境变量值不同；两者都属于"这一代"，都应该认。旧代的
        代号两个都不是 —— 迟到的 force 照样失效。
        """
        ids = {value for value in (self.current_instance_id(), self.instance_id) if value}
        return ids

    def _normalized_instance(self, given: str | None) -> str | None:
        """"认得的"代号一律归一化成 lifecycle 的权威值再往下传。"""
        current = self.current_instance_id()
        if not given:
            return current
        return current if given in self.acceptable_instance_ids() else given

    def snapshot(self) -> dict:
        snapshot = dict(self._lifecycle.snapshot())
        action = self._op_action or snapshot.get("action")
        if action == "upgrade_hold":
            # 预排空期间不接受普通重启，也没有"加速"的意义：要么提交要么撤销。
            snapshot["can_restart"] = False
            snapshot["can_force"] = False
        else:
            snapshot["can_restart"] = bool(snapshot.get("can_restart", True) and self.can_restart)
        if self._op_action and snapshot.get("operation_id") is None:
            snapshot["operation_id"] = self._op_id
        if self._op_action == "stop" and snapshot.get("action") == "upgrade_hold":
            # hold 已提交为停机：对外只报最终动作。
            snapshot["action"] = "stop"
            if snapshot.get("phase") == "draining":
                snapshot["phase"] = "stopping"
        return snapshot

    # -- 事件 ---------------------------------------------------------------
    @property
    def sealed(self) -> asyncio.Event:
        return self._sealed

    @property
    def resumed(self) -> asyncio.Event:
        return self._resumed

    @property
    def committed(self) -> asyncio.Event:
        return self._committed

    def effective_action(self) -> str | None:
        return self._op_action

    @property
    def hold_deadline(self) -> float | None:
        """当前 upgrade_hold 的租期（epoch 秒）；不是 hold 时为 None。"""
        return self._hold_deadline

    def _arm_hold_lease(self, deadline: float | None) -> None:
        """按租期排好"自动恢复准入"。只在事件循环线程上调用。"""
        self._cancel_hold_lease()
        self._hold_deadline = deadline
        if deadline is None or self._loop is None:
            return
        delay = max(0.0, deadline - time.time())
        self._hold_timer = self._loop.call_later(delay, self._hold_lease_expired)

    def _cancel_hold_lease(self) -> None:
        timer = self._hold_timer
        self._hold_timer = None
        self._hold_deadline = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001 - 取消失败不影响语义
                pass

    def _hold_lease_expired(self) -> None:
        """租期到期：核实仍是同一未提交 hold 才自动 RESUME（提交后禁用）。"""
        self._hold_timer = None
        self._hold_deadline = None
        if self._op_action != "upgrade_hold" or self._hold_committed or self._hold_resumed:
            return
        logger.warning("upgrade_hold 租期到期，核实同一未提交 hold 后自动恢复准入")
        receipt = self.resume(self._op_id or "", self.current_instance_id() or "")
        if not receipt.get("ok"):
            logger.error("自动恢复准入失败（%s）：本代仍封口，需要发布方手动处理",
                         receipt.get("reason"))

    def operation_id(self) -> str | None:
        return self._op_id

    # -- 受理 ---------------------------------------------------------------
    @staticmethod
    def _is_sealed(snapshot: dict) -> bool:
        return snapshot.get("phase") in SEALED_PHASES and bool(snapshot.get("operation_id"))

    def _receipt(self, snapshot: dict, *, accepted: bool, duplicate: bool,
                 reason: str = "", action: str | None = None) -> dict:
        return {
            "ok": bool(accepted),
            "accepted": bool(accepted),
            "duplicate": bool(duplicate),
            "reason": reason or None,
            "action": action or snapshot.get("action") or self._op_action,
            "operation_id": snapshot.get("operation_id") or self._op_id,
            "instance_id": snapshot.get("instance_id") or self.instance_id,
            "source": self._op_source,
            "snapshot": snapshot,
        }

    @staticmethod
    def _thickened(receipt: dict) -> dict:
        """加厚回执：快照字段铺平到顶层，同时保留嵌套 snapshot。

        IPC/CLI 直接把返回值当快照读（phase/instance_id/operation_id/active_*），
        WorkerOwner 仍能从 snapshot 里取到完整结构。
        """
        snapshot = dict(receipt.get("snapshot") or {})
        thickened = dict(snapshot)
        thickened.update({name: value for name, value in receipt.items()
                          if name != "snapshot"})
        thickened["snapshot"] = snapshot
        return thickened

    @staticmethod
    def _flatten(receipt: dict) -> dict:
        """把快照字段铺平到回执顶层：调用方可以直接当快照读（counts/is_idle）。"""
        flattened = dict(receipt.get("snapshot") or {})
        flattened.update({name: receipt.get(name) for name in
                          ("ok", "accepted", "duplicate", "reason", "action",
                           "operation_id", "instance_id", "source")})
        return flattened

    def _resume_refusal(self, snapshot: dict, operation_id: str,
                        instance_id: str | None) -> str | None:
        """RESUME 的前置判定：返回可判别原因，None 表示可以交给 lifecycle.resume。

        判定顺序固定，且**不依赖** lifecycle 的冲突文案（那是给人看的字符串）：
        代号 -> 已提交停止（本地 commit / 阶段已离开 draining）-> 同一操作 -> 是不是预排空。
        """
        if instance_id and instance_id not in self.acceptable_instance_ids():
            return "stale_generation"
        if self._hold_committed:
            # worker 本地已把同一 hold 提交为 stop（见 _commit_hold）。
            return "already_committed"
        phase = snapshot.get("phase")
        if phase is not None and phase != "draining":
            # lifecycle 已经离开 draining：mark_stopping/mark_stopped/mark_failed
            # 都意味着"停止已提交"，回退被禁用。
            return "stop_committed"
        current_op = snapshot.get("operation_id")
        if not current_op or current_op != operation_id:
            return "not_current_operation"
        if (self._op_action or snapshot.get("action")) != "upgrade_hold":
            return "not_a_hold"
        return None

    def pending_restart(self) -> dict | None:
        """已受理的 Web 重启回执（幂等判定用）：没有就返回 None。"""
        snapshot = self.snapshot()
        if self._is_sealed(snapshot) and snapshot.get("action") == "restart":
            return self._receipt(snapshot, accepted=True, duplicate=True, action="restart")
        return None

    def begin_restart(self, operation_id: str | None = None, *, source: str = "http") -> dict:
        """Web 重启：同一逻辑重启只产生**一个** operation_id（双击/重放幂等）。"""
        pending = self.pending_restart()
        if pending is not None:
            return pending
        op = operation_id or ("restart-" + secrets.token_hex(8))
        return self.begin("restart", op, None, source=source)

    def begin(self, action: str, operation_id: str, instance_id: str | None = None,
              *, source: str = "ipc", hold_until: float | None = None) -> dict:
        if action not in ALLOWED_ACTIONS:
            snapshot = self.snapshot()
            return self._receipt(snapshot, accepted=False, duplicate=False,
                                 reason="unsupported_action")
        hold_deadline = None
        if action == "upgrade_hold":
            now = time.time()
            requested = now + DEFAULT_HOLD_LEASE if hold_until is None else float(hold_until)
            if requested <= now:
                return self._receipt(self.snapshot(), accepted=False, duplicate=False,
                                     reason="hold_expired")
            # 与升级标记同一 180 秒上限：不给"永久封口"留口子。
            hold_deadline = min(requested, now + MAX_HOLD_LEASE)
        with self._lock:
            snapshot = self.snapshot()
            current_instance = snapshot.get("instance_id") or self.instance_id
            if instance_id and instance_id not in self.acceptable_instance_ids():
                # 迟到的一代：端点可能已经被下一代复用，这里再挡一次。
                return self._receipt(snapshot, accepted=False, duplicate=False,
                                     reason="stale_generation")
            instance_id = self._normalized_instance(instance_id)
            if self._is_sealed(snapshot):
                current_action = snapshot.get("action")
                current_op = snapshot.get("operation_id")
                if current_op == operation_id:
                    if current_action == action:
                        return self._receipt(snapshot, accepted=True, duplicate=True,
                                             action=current_action)
                    if current_action == "upgrade_hold" and action == "stop":
                        return self._commit_hold(snapshot, operation_id)
                    return self._receipt(snapshot, accepted=False, duplicate=False,
                                         reason="conflict", action=current_action)
                return self._receipt(snapshot, accepted=False, duplicate=False,
                                     reason="conflict", action=current_action)
            if snapshot.get("phase") in ("stopped", "failed"):
                return self._receipt(snapshot, accepted=False, duplicate=False,
                                     reason="already_stopped")
            try:
                self._lifecycle.begin(action, operation_id,
                                      instance_id=instance_id or current_instance)
            except Exception as exc:  # lifecycle 用异常报告冲突，这里统一转成回执
                conflict = _conflict_snapshot(exc)
                if conflict is None:
                    raise
                return self._receipt(conflict, accepted=False, duplicate=False,
                                     reason="conflict")
            self._op_action = action
            self._op_id = operation_id
            self._op_source = source
            self._hold_committed = False
            self._hold_resumed = False
            self._forced = None
            self._resumed_op = None
            self._arm_hold_lease(hold_deadline)
            # 回执用**合并后**的快照：upgrade_hold 期间没有可加速的操作、
            # 没有重启所有者时 can_restart=False，这些都要在回执里就可见。
            receipt = self._receipt(self.snapshot(), accepted=True, duplicate=False,
                                    action=action)
            self._sealed.set()
            if self._on_sealed is not None:
                self._on_sealed(action, operation_id, source)
            return receipt

    def _commit_hold(self, snapshot: dict, operation_id: str) -> dict:
        self._cancel_hold_lease()
        """hold 提交：同一操作从"预排空"升级为"停机"，不再调用 lifecycle.begin。

        lifecycle 对"已封口 + 换动作"一律冲突（契约第 1 节），而提交是发布方的
        一次性决定，所以由本层记下最终动作，排空观察者据此退出。
        """
        if self._hold_committed:
            return self._receipt(snapshot, accepted=True, duplicate=True, action="stop")
        self._hold_committed = True
        self._op_action = "stop"
        self._op_source = "commit"
        committed = dict(snapshot)
        committed["action"] = "stop"
        receipt = self._receipt(committed, accepted=True, duplicate=False, action="stop")
        self._committed.set()
        if self._on_sealed is not None:
            self._on_sealed("stop", operation_id, "commit")
        return receipt

    def force(self, operation_id: str, instance_id: str, confirm: bool = False) -> dict:
        snapshot = self.snapshot()
        if confirm is not True:
            return self._receipt(snapshot, accepted=False, duplicate=False,
                                 reason="confirm_required")
        if instance_id not in self.acceptable_instance_ids():
            return self._receipt(snapshot, accepted=False, duplicate=False,
                                 reason="stale_generation")
        instance_id = self._normalized_instance(instance_id) or instance_id
        if not snapshot.get("operation_id") or snapshot.get("operation_id") != operation_id:
            return self._receipt(snapshot, accepted=False, duplicate=False,
                                 reason="not_current_operation")
        if self._forced == (operation_id, instance_id):
            # 同一代同一操作重复强制：回同一份结果（幂等），不再动 lifecycle。
            return self._receipt(snapshot, accepted=True, duplicate=True)
        try:
            forced = self._lifecycle.force(operation_id, instance_id, True)
        except Exception as exc:
            conflict = _conflict_snapshot(exc)
            if conflict is None:
                raise
            return self._receipt(conflict, accepted=False, duplicate=False, reason="conflict")
        self._forced = (operation_id, instance_id)
        request_cancel = getattr(self._lifecycle, "request_cancel", None)
        if callable(request_cancel):
            try:
                # 幂等：lifecycle.force() 可能已经置过取消意图，这里只是兜底
                # 保证"worker 侧中断意图"这件事发生过一次。
                request_cancel()
            except Exception:  # 取消意图失败不该让回执失败
                logger.exception("request_cancel 失败（忽略）")
        receipt = self._receipt(dict(forced), accepted=True, duplicate=False)
        if self._on_force is not None:
            self._on_force(operation_id, instance_id, True)
        return receipt

    def resume(self, operation_id: str, instance_id: str) -> dict:
        """撤销**尚未提交停止**的 upgrade_hold，重新打开准入。

        权威判定在 lifecycle.resume()（同代 + 同一进行中操作 + 阶段仍是
        draining）；这里只做两件事：先把原因翻译成可判别值（stale_generation /
        stop_committed / not_current_operation / not_a_hold），再同步本地状态。
        旧代没有 lifecycle.resume 时如实回 resume_unsupported，调用方降级。
        """
        snapshot = self.snapshot()
        if instance_id and instance_id not in self.acceptable_instance_ids():
            return self._receipt(snapshot, accepted=False, duplicate=False,
                                 reason="stale_generation")
        if self._hold_resumed and self._resumed_op == operation_id:
            # 同一次撤销的重复投递：回同一份成功回执（幂等），不再动 lifecycle。
            return self._thickened(self._receipt(snapshot, accepted=True, duplicate=True,
                                                 action=snapshot.get("action")))
        reason = self._resume_refusal(snapshot, operation_id, instance_id)
        if reason:
            return self._receipt(snapshot, accepted=False, duplicate=False, reason=reason)
        resumer = getattr(self._lifecycle, "resume", None)
        if not callable(resumer):
            # 旧代能力：没有 resume() 就真的无法重新开准入，如实回报，
            # 让发布方走"恢复旧代码 + 重新 readiness"的兜底。
            return self._receipt(snapshot, accepted=False, duplicate=False,
                                 reason="resume_unsupported")
        try:
            resumed = resumer(operation_id, self._normalized_instance(instance_id))
        except Exception as exc:
            conflict = _conflict_snapshot(exc)
            if conflict is None:
                raise
            # lifecycle 再挡一次（并发时序）：仍然翻译成可判别原因。
            reason = self._resume_refusal(conflict, operation_id, instance_id) or "conflict"
            return self._receipt(conflict, accepted=False, duplicate=False, reason=reason)
        self._hold_resumed = True
        self._resumed_op = operation_id
        self._cancel_hold_lease()
        self._op_action = None
        self._op_id = None
        self._op_source = None
        self._forced = None
        receipt = self._receipt(dict(resumed), accepted=True, duplicate=False,
                                action=resumed.get("action"))
        receipt["operation_id"] = operation_id
        receipt["instance_id"] = resumed.get("instance_id") or self.instance_id
        self._resumed.set()
        if self._on_resume is not None:
            self._on_resume(operation_id, instance_id)
        # 加厚回执：phase/instance_id/operation_id/active_* 直接可读。
        return self._thickened(receipt)

    # -- HTTP 重启响应的发送闸门 -------------------------------------------
    def arm_response_gate(self) -> None:
        """受理走 HTTP 时，排空完成后要等原响应真正写出去再停服。"""
        self._gate_armed = True
        self._response_gate.clear()

    def release_response_gate(self) -> None:
        """响应已发出，或请求方断线（finally 路径）——解除等待，幂等。"""
        if not self._gate_armed:
            return
        self._gate_armed = False
        self._response_gate.set()

    async def wait_response_gate(self) -> None:
        if not self._gate_armed:
            return
        await self._response_gate.wait()

    # -- IPC 投递 -----------------------------------------------------------
    def dispatch(self, message: dict) -> dict:
        """控制线程入口：校验后把真正的处理投递到事件循环，自己只等结果。"""
        reason = validate_message(message)
        if reason:
            return _error_reply("invalid_message", reason, self._safe_snapshot())
        kind = message["kind"]
        handler = {
            "STATUS": self._ipc_status,
            "BEGIN": self._ipc_begin,
            "FORCE": self._ipc_force,
            "RESUME": self._ipc_resume,
        }[kind]
        return self._run_on_loop(handler, message)

    def _safe_snapshot(self) -> dict:
        try:
            return self.snapshot()
        except Exception:  # 快照失败不能把回执也带崩
            logger.exception("读取快照失败")
            return {}

    def _ipc_status(self, message: dict) -> dict:
        snapshot = self.snapshot()
        instance = message.get("instance_id")
        if instance and instance not in self.acceptable_instance_ids():
            return self._receipt(snapshot, accepted=False, duplicate=False,
                                 reason="stale_generation")
        return self._receipt(snapshot, accepted=True, duplicate=True)

    def _ipc_begin(self, message: dict) -> dict:
        return self.begin(message["action"], message["operation_id"],
                          message.get("instance_id"), source=message.get("source") or "ipc",
                          hold_until=message.get("hold_until"))

    def _ipc_force(self, message: dict) -> dict:
        return self.force(message["operation_id"], message["instance_id"],
                          message.get("confirm") is True)

    def _ipc_resume(self, message: dict) -> dict:
        return self.resume(message["operation_id"], message["instance_id"])

    def _run_on_loop(self, handler, message: dict) -> dict:
        loop = self._loop
        if loop is None or loop.is_closed():
            return _error_reply("not_ready", "worker 事件循环尚未就绪", self._safe_snapshot())
        future: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                future.set_result(handler(message))
            except BaseException as exc:  # noqa: BLE001 - 全量转成回执
                future.set_exception(exc)

        try:
            loop.call_soon_threadsafe(run)
        except RuntimeError:
            return _error_reply("not_ready", "worker 事件循环已关闭", self._safe_snapshot())
        try:
            return future.result(timeout=DISPATCH_TIMEOUT)
        except concurrent.futures.TimeoutError:
            return _error_reply("timeout", "worker 未在超时内给出结果", self._safe_snapshot())
        except Exception as exc:  # noqa: BLE001
            logger.exception("控制请求处理失败")
            return _error_reply("internal_error", type(exc).__name__, self._safe_snapshot())


def _conflict_snapshot(exc: BaseException) -> dict | None:
    """把 lifecycle 的冲突异常翻译成"当前快照"；不是冲突就返回 None。

    鸭子类型：admin.lifecycle 由任务 1 并行实现，这里不直接导入它的类，
    只认"带 snapshot 属性的冲突异常"（以及类名以 Conflict 结尾的异常）。
    """
    snapshot = getattr(exc, "snapshot", None)
    if isinstance(snapshot, dict):
        return snapshot
    if type(exc).__name__.endswith("Conflict"):
        return {}
    return None


# ---------------------------------------------------------------------------
# 进程内注册表：HTTP 路由与运行器共用同一个 LocalControl
# ---------------------------------------------------------------------------

_LOCAL: LocalControl | None = None
_LOCAL_LOCK = threading.Lock()


def set_local_control(control: LocalControl | None) -> None:
    global _LOCAL
    with _LOCAL_LOCK:
        _LOCAL = control


def get_local_control() -> LocalControl | None:
    return _LOCAL


def clear_local_control(control: LocalControl | None = None) -> None:
    """只清掉"还是自己"的那份，避免下一代启动时误删。"""
    global _LOCAL
    with _LOCAL_LOCK:
        if control is None or _LOCAL is control:
            _LOCAL = None


# ---------------------------------------------------------------------------
# worker 侧：控制端点
# ---------------------------------------------------------------------------

class WorkerControlServer:
    """每代一个的仅本机端点；I/O 全部在控制线程，不占事件循环。"""

    def __init__(self, control_dir: Path, authkey: bytes, dispatch, *,
                 read_timeout: float = DEFAULT_TIMEOUT,
                 max_message_bytes: int = MAX_MESSAGE_BYTES,
                 max_connections: int = MAX_CONTROL_CONNECTIONS) -> None:
        if not isinstance(authkey, (bytes, bytearray)) or not authkey:
            raise ValueError("authkey 必须是非空字节串")
        self.control_dir = Path(control_dir)
        self.authkey = bytes(authkey)
        self._dispatch = dispatch
        self._read_timeout = read_timeout
        self._max_message_bytes = max_message_bytes
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stopping = threading.Event()
        self._listener: Listener | None = None
        self._thread: threading.Thread | None = None
        self._address: str | None = None

    @property
    def address(self) -> str:
        return self._address or control_address(self.control_dir)

    def start(self) -> None:
        """建立端点。必须在"接受业务请求"之前调用。"""
        _secure_dir(self.control_dir)
        if os.name != "nt":
            try:
                (self.control_dir / SOCKET_NAME).unlink()
            except OSError:
                pass
        self._address = control_address(self.control_dir)
        self._listener = Listener(self._address, family=control_family(), backlog=16,
                                  authkey=self.authkey)
        if os.name != "nt":
            _secure_socket(self.control_dir / SOCKET_NAME)
        self._thread = threading.Thread(target=self._accept_loop,
                                        name="wb-lifecycle-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                logger.debug("关闭控制端点失败（忽略）")
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None
        self._listener = None
        if os.name != "nt":
            try:
                (self.control_dir / SOCKET_NAME).unlink()
            except OSError:
                pass

    # -- 内部 ---------------------------------------------------------------
    def _accept_loop(self) -> None:
        listener = self._listener
        while not self._stopping.is_set() and listener is not None:
            try:
                connection = listener.accept()
            except (OSError, EOFError):
                if self._stopping.is_set():
                    return
                time.sleep(0.05)
                continue
            except AuthenticationError:
                # authkey 不对的本地进程：直接丢掉，继续服务。
                continue
            except Exception:  # noqa: BLE001
                if self._stopping.is_set():
                    return
                logger.debug("控制连接 accept 失败（忽略）", exc_info=True)
                continue
            if self._stopping.is_set() or not self._slots.acquire(blocking=False):
                # 端点已经关了（进程正在退出）：这一代不再服务任何控制请求。
                _close_quietly(connection)
                continue
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def _handle(self, connection) -> None:
        try:
            # 客户端连上却不说话：读超时后直接丢，绝不阻塞别的连接。
            if not _poll(connection, self._read_timeout):
                return
            try:
                raw = connection.recv_bytes(self._max_message_bytes)
            except OSError:
                return
            try:
                message = decode_message(raw)
            except ValueError as exc:
                if not self._stopping.is_set():
                    _send(connection, _error_reply("invalid_message", str(exc)))
                return
            if self._stopping.is_set():
                # 端点关闭时 Windows 上可能还有一个"已完成的 accept"，
                # 它拿到的连接不再有权威状态可报，直接不发回执。
                return
            reply = self._dispatch(message)
            _send(connection, reply)
        except (OSError, EOFError):
            return
        except Exception:  # noqa: BLE001
            logger.exception("控制连接处理失败")
        finally:
            _close_quietly(connection)
            self._slots.release()


def _poll(connection, timeout: float) -> bool:
    try:
        return bool(connection.poll(timeout))
    except (OSError, EOFError):
        return False


def _send(connection, message: dict) -> None:
    try:
        connection.send_bytes(encode_message(message))
    except (OSError, EOFError):
        logger.debug("控制回执发送失败（对端已断开）")


def _close_quietly(connection) -> None:
    try:
        connection.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 父进程侧：WorkerOwner
# ---------------------------------------------------------------------------

class ControlError(RuntimeError):
    """控制通道错误基类。reason 可判别，snapshot 是当时最能说明问题的那份状态。"""

    def __init__(self, message: str, *, reason: str = "error",
                 snapshot: dict | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.snapshot = snapshot or {}


class ControlUnavailable(ControlError):
    """连不上 / 读超时 / 回执不可解析：只代表"暂时观测不到"，不代表进程已退出。"""


class ControlRefused(ControlError):
    """worker 明确拒绝（已有其它操作、代号不匹配、已提交等）。"""


def _authkey_bytes(value) -> bytes:
    """authkey 既接受十六进制文本也接受原始字节（部署 CLI 两种都可能给）。"""
    if isinstance(value, (bytes, bytearray)):
        key = bytes(value)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("authkey 不能为空")
        try:
            key = bytes.fromhex(text)
        except ValueError:
            key = text.encode("utf-8")
    if not key:
        raise ValueError("authkey 不能为空")
    return key


def _refusal_text(reply: dict) -> str:
    return "worker 拒绝了本次控制请求（%s）" % (reply.get("reason") or "refused",)


class WorkerOwner:
    """父进程（桌面壳 / 监督器 / 升级 CLI）用来发起、观察并等待同一代 worker。

    错误约定（与 scripts/upgrade_control.py 对齐）：
    * 连不上 / 超时 / 回执不可解析 -> 抛 ControlUnavailable（"暂时观测不到"）；
    * worker 明确拒绝（冲突、代号不匹配、已提交）-> 抛 ControlRefused；
    * status() 不抛异常：失联时返回 phase="unreachable" 的快照，
      **绝不会**伪装成 ready。

    popen 可以构造时给，也可以用 attach() 后补：Popen 句柄是"这一代"的唯一
    物理标识，真实退出码是最终事实。
    """

    def __init__(self, control_dir: Path, authkey, *, timeout: float = DEFAULT_TIMEOUT,
                 popen=None) -> None:
        self.control_dir = Path(control_dir)
        self.authkey = _authkey_bytes(authkey)
        self.timeout = timeout
        self._popen = popen

    # -- 句柄 ---------------------------------------------------------------
    def attach(self, popen) -> None:
        self._popen = popen

    @property
    def address(self) -> str:
        return control_address(self.control_dir)

    @property
    def family(self) -> str:
        return control_family()

    # -- 协议 ---------------------------------------------------------------
    def status(self, *, timeout: float | None = None) -> dict:
        """只返回快照本身（契约第 1 节的字段），失联时是 phase="unreachable"。"""
        try:
            reply = self.request({"kind": "STATUS"}, timeout or self.timeout)
        except ControlUnavailable as exc:
            return exc.snapshot or self.unreachable_snapshot(exc.reason)
        snapshot = reply.get("snapshot") if isinstance(reply, dict) else None
        if isinstance(snapshot, dict) and snapshot.get("phase"):
            return snapshot
        return self.unreachable_snapshot("invalid_reply")

    def begin(self, action: str, operation_id: str, instance_id: str | None = None, *,
              timeout: float = 10.0) -> dict:
        message = {"kind": "BEGIN", "action": action, "operation_id": operation_id}
        if instance_id:
            message["instance_id"] = instance_id
        return self._action_reply(message, timeout)

    def begin_upgrade_hold(self, operation_id: str, hold_until: float | None = None, *,
                           timeout: float = 10.0) -> dict:
        """BEGIN(action="upgrade_hold")，可带租期；成功时返回**加厚过的快照**。

        升级 CLI 把返回值当快照读（instance_id + 两类计数），所以这里把回执
        字段铺平在快照旁边，而不是再包一层。
        """
        message = {"kind": "BEGIN", "action": "upgrade_hold", "operation_id": operation_id}
        if hold_until is not None:
            message["hold_until"] = float(hold_until)
        result = self._action_reply(message, timeout, flatten=True)
        # 把实际生效的租期回给调用方（worker 可能按 180 秒上限收窄）。
        result["hold_until"] = message.get("hold_until")
        return result

    def force(self, operation_id: str, instance_id: str, confirm: bool, *,
              timeout: float = 10.0) -> dict:
        return self._action_reply({"kind": "FORCE", "operation_id": operation_id,
                                   "instance_id": instance_id,
                                   "confirm": bool(confirm)}, timeout)

    def resume(self, operation_id: str, instance_id: str, *, timeout: float = 10.0) -> dict:
        # 加厚快照：调用方（发布 CLI / 桌面）可直接当快照读，不必再拆一层。
        return self._action_reply({"kind": "RESUME", "operation_id": operation_id,
                                   "instance_id": instance_id}, timeout, flatten=True)

    def _action_reply(self, message: dict, timeout: float, *, flatten: bool = False) -> dict:
        reply = self.request(message, timeout)
        if not reply.get("ok"):
            raise ControlRefused(_refusal_text(reply),
                                 reason=reply.get("reason") or "refused",
                                 snapshot=reply.get("snapshot") or {})
        if not flatten:
            return reply
        snapshot = dict(reply.get("snapshot") or {})
        snapshot.update({name: reply.get(name) for name in
                         ("ok", "duplicate", "reason", "action", "operation_id",
                          "instance_id", "source")})
        return snapshot

    def request(self, message: dict, timeout: float | None = None) -> dict:
        """一次请求/回执。连接与读取都在**调用方线程**里做，且有独立超时。

        实现用一条守护线程承载 Client()（标准库的连接没有超时参数），
        调用方最多等 timeout 秒就抛 ControlUnavailable，绝不把 GUI/ASGI
        主线程粘住。
        """
        timeout = self.timeout if timeout is None else timeout
        try:
            payload = encode_message(message)
        except (TypeError, ValueError) as exc:
            raise ControlUnavailable("控制消息无法编码：%s" % (exc,),
                                     reason="invalid_message") from exc
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ControlUnavailable("控制消息超过 64 KiB 上限", reason="invalid_message")
        deadline = time.monotonic() + timeout
        result: dict = {}
        done = threading.Event()

        def run() -> None:
            connection = None
            try:
                connection = Client(self.address, family=self.family, authkey=self.authkey)
                connection.send_bytes(payload)
                remaining = max(0.0, deadline - time.monotonic())
                if not _poll(connection, remaining):
                    result["reason"] = "timeout"
                    return
                try:
                    raw = connection.recv_bytes(MAX_MESSAGE_BYTES)
                except OSError as exc:
                    result["reason"] = "invalid_reply"
                    result["message"] = str(exc)
                    return
                try:
                    result["reply"] = decode_message(raw)
                except ValueError as exc:
                    result["reason"] = "invalid_reply"
                    result["message"] = str(exc)
            except AuthenticationError:
                result["reason"] = "unauthorized"
            except (OSError, EOFError) as exc:
                result["reason"] = "unreachable"
                result["message"] = type(exc).__name__
            except Exception as exc:  # noqa: BLE001
                result["reason"] = "unreachable"
                result["message"] = type(exc).__name__
            finally:
                if connection is not None:
                    _close_quietly(connection)
                done.set()

        threading.Thread(target=run, name="wb-lifecycle-client", daemon=True).start()
        done.wait(timeout)
        reply = result.get("reply")
        if isinstance(reply, dict):
            return reply
        reason = result.get("reason") or "timeout"
        detail = result.get("message") or ""
        raise ControlUnavailable(
            "控制通道暂时不可用（%s%s）" % (reason, ("：" + detail) if detail else ""),
            reason=reason, snapshot=self.unreachable_snapshot(reason))

    def unreachable_snapshot(self, reason: str = "unreachable") -> dict:
        """失联时的可判别快照：phase="unreachable" 明确区别于任何真实阶段。"""
        return {
            "phase": "unreachable", "action": None, "instance_id": None,
            "operation_id": None, "active_requests": None, "active_jobs": None,
            "elapsed_ms": None, "can_restart": False, "can_force": False,
            "unreachable": True, "reason": reason,
        }

    # -- 终结 ---------------------------------------------------------------
    def wait_exit(self, *, timeout: float | None = None) -> int | None:
        """等这一代真的退出；超时返回 None（超时只代表没看到，不代表已退出）。"""
        popen = self._popen
        if popen is None:
            return None
        try:
            return popen.wait(timeout=timeout)
        except Exception:  # subprocess.TimeoutExpired 等
            return None

    def poll(self) -> int | None:
        popen = self._popen
        return None if popen is None else popen.poll()
