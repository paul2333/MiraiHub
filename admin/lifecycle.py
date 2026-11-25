"""每个应用运行实例的生命周期：原子准入、在途计数、排空封口、阶段快照。

## 为什么需要这一层

原来的退出链路没有任何「还在干活的请求」的概念：重启请求确认后 1.5 秒直接
os._exit(0)，桌面 terminate 也会跳过应用排空。结果是**已经准入的模型流被拦腰截断**，
账本/审计只写了一半。要修这个问题，先得有一处权威计数，并且「还能不能接活」
与「记上这一笔」必须是同一个原子动作 —— 分开做就必然出现「刚判完 ready、还没
登记，就被 begin() 封口」的漏记请求，排空会以为已经归零。

## 关键不变量

* 阶段判定与加入在途集在同一把短锁里完成（try_admit / begin 共用这把锁）。
* 锁内只改状态、收集等待者；通知一律放到锁外（避免持锁调用别的代码）。
* wait_empty() 每次被唤醒都重新验谓词，不做「一次通知算数」的假设。
* 租约唯一且 finish() 幂等：计数永不为负，重复释放不会把别人的名额减掉。
* 线程里的完成通过 loop.call_soon_threadsafe 投递到事件循环（asyncio.Event
  不是线程安全的，不能直接 set）。
* begin() 对同一 operation_id 幂等（重复点击只有一个操作），不同意图抛
  LifecycleConflict 并带当前快照。
* reset() 每次 lifespan 启动重建 instance_id：同一个进程里重建 app（测试、
  uvicorn 复用进程）不能继承上一次的 draining；父进程注入了
  WB_LIFECYCLE_INSTANCE_ID 时采纳它，保证 worker 与控制通道对代号有唯一答案。
* resume() 只撤销「尚未提交停止的 upgrade_hold 预排空」，**不是通用取消键**：
  进程内的 begin("restart") / begin("stop") 之后即使计数归零也不许把准入改回
  ready —— 那种请求的语义是「要换代码/要退出」，复活它等于让两个代同时接单。
  停止一旦提交，阶段就是唯一真相，拒绝恢复 —— 不再额外存标志位。

行为规格以 docs/superpowers/plans/2026-11-22-graceful-shutdown-and-shell-CONTRACT.md
第 1、2 节为准；快照字段固定为 phase/action/instance_id/operation_id/
active_requests/active_jobs/elapsed_ms/can_restart/can_force，且**不允许**出现
请求原文、账号、密钥或本机路径。

## 排空拒绝与连接语义（客户端需要知道的事）

排空 503 发生在读完请求体之前：有的请求体是几十 MB 的上传，有的分块正文还没发完。
HTTP/1.x 下服务端没消费完请求体时无法保证同一条连接上的字节边界对齐，所以这条响应
显式带 Connection: close（HTTP/2 省略该头 —— 每条流自成边界，Connection 是禁止
发送的 hop-by-hop 头）：服务端发完就关，客户端应当**新建连接**再重试（按
Retry-After 的秒数等待）。这与本仓库既有的 429（admin/abuse_guard.py）和准入拒绝
是同一取舍：都不为一条拒绝去读完请求体，都靠关闭连接保证协议安全。

回归见 tests/test_graceful_lifecycle.py 的 DrainingConnectionTests：真实 uvicorn +
raw socket + chunked 请求，锁住「503 之后同一连接不会冒出 400，补发的正文不会被
当成新请求」。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Callable, NamedTuple

from admin import env_guard

logger = logging.getLogger("admin.lifecycle")

#: 阶段取值（契约固定）。
PHASE_STARTING = "starting"
PHASE_READY = "ready"
PHASE_DRAINING = "draining"
PHASE_STOPPING = "stopping"
PHASE_STOPPED = "stopped"
PHASE_FAILED = "failed"

PHASES = (PHASE_STARTING, PHASE_READY, PHASE_DRAINING, PHASE_STOPPING,
          PHASE_STOPPED, PHASE_FAILED)

#: 唯一允许 resume() 撤销的动作：部署预排空（升级闸门的 upgrade_hold）。
#: 收窄的原因：RESUME 是**升级二阶段**的回退通道（发布预算耗尽 / marker 过期），
#: 不是「撤销任意排空」的通用键。restart/stop/update/shutdown 已经被发起方认领，
#: 复活旧代会让两个代同时接单；控制通道那边的 _resume_refusal 也按同一规则拦。
RESUMABLE_ACTION = "upgrade_hold"

#: 可以被计数的租约种类：HTTP 请求与真正脱离请求的后台任务。
LEASE_KINDS = ("request", "job")

#: 无法再开始新排空操作的阶段（单向阶段，迟到调用不许把它们拉回 draining）。
_FINAL_PHASES = (PHASE_STOPPING, PHASE_STOPPED, PHASE_FAILED)

#: 排空拒绝的统一业务码（CONTRACT 第 2 节）。
DRAIN_BUSINESS_CODE = "service_draining"
DRAIN_MESSAGE = "服务正在重启或停止，请稍后重试"
RETRY_AFTER_SECONDS = 3


class LifecycleConflict(RuntimeError):
    """生命周期操作的冲突意图；snapshot 是冲突时刻的当前快照。"""

    def __init__(self, message: str, snapshot: dict[str, Any]):
        super().__init__(message)
        self.snapshot = dict(snapshot)


class Lease:
    """不透明租约：只保证 identity 与一次性释放。"""

    __slots__ = ("kind", "lease_id")

    def __init__(self, kind: str, lease_id: str):
        self.kind = kind
        self.lease_id = lease_id

    def __repr__(self) -> str:  # pragma: no cover - 排障用
        return f"<Lease {self.kind}:{self.lease_id[:8]}>"


#: 父进程为本代准备好的代号。与 CONTRACT 第 3 节 admin/lifecycle_control.INSTANCE_ENV
#: 同名同值（那边是控制通道的权威定义），这里只负责在 reset() 时读它。
INSTANCE_ENV = "WB_LIFECYCLE_INSTANCE_ID"


def _new_instance_id() -> str:
    """一代应用的代号：不透明随机串，不充当凭据，也不暴露任何本机信息。"""
    return secrets.token_hex(12)


def _adopted_instance_id() -> str:
    """本代代号：优先采纳父进程注入的 WB_LIFECYCLE_INSTANCE_ID。

    为什么必须采纳：进程所有者（桌面壳 / main.py 监督器）为每次启动准备 IPC 地址和
    代号，控制通道的 BEGIN/FORCE/RESUME 都要校验「这条请求属于哪一代」。worker 要是
    自己再随机一个，就会出现「父进程以为这是 A 代、worker 自称 B 代」——代号校验
    形同虚设，迟到的旧操作反而能命中新进程。没注入时（直接 uvicorn 启动、测试）
    才自己生成。
    """
    injected = (os.getenv(INSTANCE_ENV) or "").strip()
    return injected or _new_instance_id()


class Lifecycle:
    """一次应用启动自己的准入/计数/排空状态。

    实例由模块单例 get_lifecycle() 持有；测试与多应用场景可以直接新建。
    clock 是单调时钟的测试缝（默认 time.monotonic）；restart_available 供
    「没有重启所有者」的部署把 can_restart 置为 False（契约外的可选扩展，
    有默认值，不改变契约签名）。
    """

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 restart_available: bool = True):
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._instance_id = _new_instance_id()
        self._phase = PHASE_STARTING
        self._action: str | None = None
        self._operation_id: str | None = None
        self._active = {"request": 0, "job": 0}
        #: 未释放的租约：lease_id -> Lease。既是幂等依据，也是唯一的减值凭据。
        self._leases: dict[str, Lease] = {}
        self._sealed = False
        self._began_at: float | None = None
        self._cancel_requested = False
        self._force_requested = False
        self._failed_reason = ""
        #: 最近一次成功 resume 的 (operation_id, instance_id)：供 CLI/网络重试幂等。
        self._resumed: tuple[str, str] | None = None
        self._restart_available = bool(restart_available)
        #: id(asyncio.Event) -> (loop, event)。只在事件循环线程里注册/注销。
        self._waiters: dict[int, tuple[Any, asyncio.Event]] = {}

    # -- 快照 ---------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        with self._lock:
            return self._instance_id

    def _snapshot_locked(self) -> dict[str, Any]:
        elapsed = 0
        if self._began_at is not None:
            elapsed = max(0, int((self._clock() - self._began_at) * 1000))
        return {
            "phase": self._phase,
            "action": self._action,
            "instance_id": self._instance_id,
            "operation_id": self._operation_id,
            "active_requests": self._active["request"],
            "active_jobs": self._active["job"],
            "elapsed_ms": elapsed,
            "can_restart": self._can_restart_locked(),
            "can_force": self._can_force_locked(),
        }

    def _can_restart_locked(self) -> bool:
        """此刻是否还能受理一次受管重启。

        排空/停机过程中按钮不该再被按（界面已进入进度态），所以只有尚未封口
        的 starting/ready 才算可选；restart_available=False 的部署恒为 False。
        """
        return (self._restart_available and not self._sealed
                and self._phase in (PHASE_STARTING, PHASE_READY))

    def _can_force_locked(self) -> bool:
        """是否还能对当前操作发起一次确认过的强制结束。

        只在 draining 阶段为真：stopping 表示在途已经归零，界面上没有「强制结束
        本次排空」这个入口，也不需要它。
        """
        return (self._operation_id is not None
                and self._phase == PHASE_DRAINING
                and not self._force_requested)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    # -- 阶段 ---------------------------------------------------------------

    def reset(self, *, instance_id: str | None = None) -> None:
        """每次 lifespan 启动调用：换代、回 starting、清空操作与计数。

        代号来源优先级：显式参数 > 父进程注入的环境变量
        （WB_LIFECYCLE_INSTANCE_ID）> 自己随机。父进程注入这一档是为了让 worker 与
        控制通道对「这是哪一代」有唯一答案。
        """
        with self._lock:
            self._instance_id = str(instance_id) if instance_id else _adopted_instance_id()
            self._phase = PHASE_STARTING
            self._action = None
            self._operation_id = None
            self._active = {"request": 0, "job": 0}
            self._leases.clear()
            self._sealed = False
            self._began_at = None
            self._cancel_requested = False
            self._force_requested = False
            self._failed_reason = ""
            self._resumed = None
            waiters = self._collect_waiters_locked()
        self._wake(waiters)

    def mark_ready(self) -> dict[str, Any]:
        """starting -> ready；其它阶段调用只返回快照，不改变阶段。

        排空期间迟到的就绪信号（例如任务 2 里带超时的数据库就绪探测）必须
        被忽略，否则准入会被重新打开。
        """
        with self._lock:
            if self._phase == PHASE_STARTING and not self._sealed:
                self._phase = PHASE_READY
            return self._snapshot_locked()

    def mark_stopping(self) -> dict[str, Any]:
        """已排空、正在关监听/刷持久化：不再接新业务，也不再有强制入口的必要。"""
        with self._lock:
            if self._phase in _FINAL_PHASES:
                return self._snapshot_locked()
            self._phase = PHASE_STOPPING
            self._sealed = True
            return self._snapshot_locked()

    def mark_stopped(self) -> dict[str, Any]:
        with self._lock:
            self._phase = PHASE_STOPPED
            self._sealed = True
            return self._snapshot_locked()

    def mark_failed(self, reason: str = "") -> dict[str, Any]:
        """启动失败/进程异常退出。原因只进日志：快照字段是固定的，不许夹带。"""
        with self._lock:
            self._phase = PHASE_FAILED
            self._sealed = True
            self._failed_reason = str(reason or "")
            snapshot = self._snapshot_locked()
        if reason:
            logger.warning("生命周期进入 failed：%s", reason)
        return snapshot

    def set_restart_available(self, available: bool) -> None:
        """契约外扩展：无重启所有者的部署（容器/无人监督）把 can_restart 置 False。"""
        with self._lock:
            self._restart_available = bool(available)

    # -- 准入与释放 ---------------------------------------------------------

    def try_admit(self, kind: str = "request") -> Lease | None:
        """阶段判定与加入在途集在同一把短锁内完成；只有 ready 且未封口才放行。"""
        if kind not in LEASE_KINDS:
            raise ValueError(f"未知的租约种类：{kind!r}（只支持 {LEASE_KINDS}）")
        with self._lock:
            if self._sealed or self._phase != PHASE_READY:
                return None
            lease = Lease(kind, secrets.token_hex(8))
            self._leases[lease.lease_id] = lease
            self._active[kind] += 1
            return lease

    def finish(self, lease: Lease | None) -> dict[str, Any]:
        """幂等释放：只有真正登记过的租约才会减计数，计数永不为负。"""
        waiters: list[tuple[Any, asyncio.Event]] = []
        with self._lock:
            if isinstance(lease, Lease) and self._leases.pop(lease.lease_id, None) is not None:
                kind = lease.kind if lease.kind in self._active else "request"
                current = self._active[kind]
                self._active[kind] = current - 1 if current > 0 else 0
                if self._active["request"] == 0 and self._active["job"] == 0:
                    waiters = self._collect_waiters_locked()
            snapshot = self._snapshot_locked()
        self._wake(waiters)
        return snapshot

    # -- 排空操作 -----------------------------------------------------------

    def begin(self, action: str, operation_id: str, instance_id: str | None = None) -> dict[str, Any]:
        """在锁内封口并返回快照；同一 operation_id 幂等，冲突意图抛异常。"""
        if not action or not operation_id:
            raise ValueError("action 与 operation_id 必须非空")
        with self._lock:
            snapshot = self._snapshot_locked()
            if instance_id is not None and instance_id != self._instance_id:
                raise LifecycleConflict("请求的生命周期代号不是当前代", snapshot)
            if self._operation_id is not None:
                if self._operation_id == operation_id and self._action == action:
                    # 同一次操作：重复点击/重复投递都返回当前快照，不重置计时与计数。
                    return snapshot
                raise LifecycleConflict("已有进行中的生命周期操作", snapshot)
            if self._phase in _FINAL_PHASES:
                raise LifecycleConflict(
                    f"当前阶段 {self._phase} 不能开始新的排空操作", snapshot)
            self._sealed = True
            self._action = str(action)
            self._operation_id = str(operation_id)
            self._phase = PHASE_DRAINING
            self._began_at = self._clock()
            self._cancel_requested = False
            self._force_requested = False
            self._resumed = None
            return self._snapshot_locked()

    def force(self, operation_id: str, instance_id: str, confirm: bool) -> dict[str, Any]:
        """只允许作用于「当前代 + 当前进行中的操作」，否则抛 LifecycleConflict。"""
        with self._lock:
            snapshot = self._snapshot_locked()
            if confirm is not True:
                raise LifecycleConflict("强制结束需要显式确认", snapshot)
            if not instance_id or instance_id != self._instance_id:
                raise LifecycleConflict("强制结束绑定的生命周期代号不是当前代", snapshot)
            if not operation_id or operation_id != self._operation_id:
                raise LifecycleConflict("强制结束绑定的操作不是进行中的操作", snapshot)
            if self._phase != PHASE_DRAINING:
                raise LifecycleConflict(
                    f"当前阶段 {self._phase} 没有可强制结束的排空", snapshot)
            self._cancel_requested = True
            self._force_requested = True
            return self._snapshot_locked()

    def resume(self, operation_id: str, instance_id: str | None = None) -> dict[str, Any]:
        """撤销一次**尚未提交停止的 upgrade_hold**，重新打开准入。

        这是升级预排空（action="upgrade_hold"）的回退通道：发布方撤销、部署预算
        耗尽或租期到期时，旧代必须能立刻恢复接单，而不是永久封口。

        **只对 upgrade_hold 生效**（`RESUMABLE_ACTION`）：restart / stop / update /
        shutdown 等动作一旦 begin() 过，本方法一律抛 LifecycleConflict —— 那些操作
        已经被发起方认领（要换代码 / 要退出），复活旧代等于让两个代同时接单。
        控制通道的 `_resume_refusal` 也按同一规则先拦一道，这里守住公共 API 的边界，
        避免绕过控制通道的进程内直接调用把它当通用取消键。

        停止一旦提交（mark_stopping/mark_stopped）或已经 failed，回退同样失效 ——
        判断依据只看阶段与动作，不额外存标志位，避免"标志位和阶段不一致"这种
        第二份真相。

        与 begin() 对称：校验代号、校验操作号，冲突一律抛 LifecycleConflict 并带
        当前快照（调用方据此决定回 409 还是重试）。
        """
        with self._lock:
            snapshot = self._snapshot_locked()
            if instance_id is not None and instance_id != self._instance_id:
                raise LifecycleConflict("恢复请求的生命周期代号不是当前代", snapshot)
            current_operation = str(operation_id or "")
            if self._operation_id is None:
                # 已经恢复过：CLI 重试 / 网络重发的同一条 RESUME 必须幂等返回，
                # 不能因为"操作已清空"就报冲突（调用方会以为回退失败而重复处置）。
                if (self._phase == PHASE_READY and self._resumed is not None
                        and self._resumed == (current_operation, self._instance_id)):
                    return snapshot
                raise LifecycleConflict("没有与 operation_id 对齐的进行中操作", snapshot)
            if self._operation_id != current_operation:
                raise LifecycleConflict("没有与 operation_id 对齐的进行中操作", snapshot)
            if self._action != RESUMABLE_ACTION:
                # 公共 API 自己守边界：RESUME 只撤销预排空，不是通用取消键。
                raise LifecycleConflict(
                    "只有尚未提交的 upgrade_hold 可以撤销准入"
                    f"（当前动作：{self._action or '无'}）", snapshot)
            if self._phase != PHASE_DRAINING:
                raise LifecycleConflict(
                    f"当前阶段 {self._phase} 已提交停止，不能恢复准入", snapshot)
            self._action = None
            self._operation_id = None
            self._phase = PHASE_READY
            self._sealed = False
            self._began_at = None
            self._cancel_requested = False
            self._force_requested = False
            self._resumed = (current_operation, self._instance_id)
            return self._snapshot_locked()

    def request_cancel(self) -> bool:
        """worker 侧强制中断意图；返回是否首次置位。"""
        with self._lock:
            if self._cancel_requested:
                return False
            self._cancel_requested = True
            return True

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    # -- 等待 ---------------------------------------------------------------

    async def wait_empty(self) -> None:
        """等到 active_requests 与 active_jobs 都归零；唤醒后重验谓词。"""
        loop = asyncio.get_running_loop()
        while True:
            event = asyncio.Event()
            with self._lock:
                if self._active["request"] == 0 and self._active["job"] == 0:
                    return
                self._waiters[id(event)] = (loop, event)
            try:
                await event.wait()
            finally:
                with self._lock:
                    self._waiters.pop(id(event), None)

    # -- 内部：等待者登记与线程安全唤醒 -------------------------------------

    def _collect_waiters_locked(self) -> list[tuple[Any, asyncio.Event]]:
        return list(self._waiters.values())

    def _wake(self, waiters: list[tuple[Any, asyncio.Event]]) -> None:
        """锁外唤醒。可能从工作线程调用，所以必须经 call_soon_threadsafe。"""
        for loop, event in waiters:
            try:
                if loop.is_closed():
                    continue
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # 事件循环已经关掉（进程正在退出）：等待者随循环一起消失。
                continue


# ---------------------------------------------------------------------------
# 排空豁免表（逐条写理由）
# ---------------------------------------------------------------------------


class DrainExemption(NamedTuple):
    """一条排空豁免：方法集合 + 精确路径或前缀 + 为什么可以不算业务。"""

    methods: frozenset[str]
    path: str
    prefix: bool
    reason: str


#: 读写方法集合。GET 自动包含 HEAD（Starlette 的路由也是这么配的）。
_READ = frozenset({"GET", "HEAD"})
_POST = frozenset({"POST"})

#: 默认纳入：**没有**写在这里的「方法 + 路径」都会占用一个在途租约。
#: 禁止按 /api/* 或 text/event-stream 一概排除 —— 那会把真实的业务写请求当成观察通道放走。
DRAIN_EXEMPTIONS: tuple[DrainExemption, ...] = (
    # ---- CORS 预检：path 为空串 + prefix 即「任意路径」，但只对 OPTIONS 生效 ----
    DrainExemption(frozenset({"OPTIONS"}), "", True,
                   "CORS 预检：OPTIONS 不触发业务、不写库，且必须由内层 CORSMiddleware "
                   "正常回 CORS 头；排空时给它 503 会让浏览器把可重试的服务未就绪读成 "
                   "CORS 失败。"),
    # ---- 独立观察探针：它们本身就是排空状态的读数，计入在途会把自己锁死 ----
    DrainExemption(_READ, "/api/app/ready", False,
                   "同源就绪探针：等待页、升级闸门与前端都靠它判断新旧代；若计入在途，"
                   "排空期间探针永远读到「还有请求」，自己把自己卡住。"),
    DrainExemption(_READ, "/api/app/version", False,
                   "版本/存活探针：壳与反代在排空期间仍要能显示版本和阶段；纯只读、"
                   "不触发上游、不写库。"),
    DrainExemption(_READ, "/api/app/desktop-ready", False,
                   "桌面壳的 nonce 就绪探针：父进程靠它判断本代能否导航；必须能在排空"
                   "期间被调用，否则界面拿不到真实阶段（nonce 校验仍由原路由执行）。"),
    DrainExemption(_READ, "/api/app/lifecycle", False,
                   "生命周期状态读取：查状态不应该延长排空，否则观察动作本身让服务停不下来。"),
    # ---- 会话核验与本地凭据交换 ----
    DrainExemption(_READ, "/api/login/session", False,
                   "会话核验 + 本地凭据交换（GET 只续期 Cookie，不写业务数据）：排空期间"
                   "壳要用它确认会话是否还有效；计入在途会让壳等到自己结束才能换票。"),
    DrainExemption(_READ, "/api/accounts/probe", False,
                   "首屏启动探针：O(1) 只验凭据、不碰账号表、不调上游；属于会话核验，"
                   "不是业务读取。"),
    # ---- 生命周期操作本身（计入在途会让重启请求成为最后一个请求）----
    DrainExemption(_POST, "/api/maintenance/restart", False,
                   "生命周期操作（发起重启）：它自己就是排空的触发者；如果占用租约，"
                   "wait_empty() 永远等不到零，服务永远停不下来。"),
    DrainExemption(_POST, "/api/maintenance/restart/force", False,
                   "生命周期操作（强制结束本次排空）：同上，且强退必须在计数归零之前也能用。"),
    # ---- 观察流：长连接，由各自的生成循环自己收尾 ----
    DrainExemption(_READ, "/api/logs/requests/stream", False,
                   "请求动态观察流（SSE，可能持续到用户关页）：计入在途会让排空永远不到零；"
                   "排空时由该生成循环自己检测 drain 后结束，不靠租约。"),
    DrainExemption(_READ, "/api/logs/usage/stream", False,
                   "用量观察流（SSE）：同上，属观察通道而非业务；排空时由生成循环主动收尾。"),
    # ---- 静态页面与资源：排空期间前端还要靠它们渲染等待页 ----
    DrainExemption(_READ, "/admin", False,
                   "控制台文档导航（HTML 壳）：排空期间浏览器要能加载页面并在原位显示进度。"),
    DrainExemption(_READ, "/admin/", False, "同上（带斜杠的文档导航）。"),
    DrainExemption(_READ, "/admin/assets/", True,
                   "控制台静态资源（CSS/JS/图标）：纯文件读取，不触发业务；等待层与"
                   "重连脚本都靠它。"),
    DrainExemption(_READ, "/static/", True,
                   "静态页面与资源（中性登录壳、join 页、图标）：同上，纯文件读取。"),
    DrainExemption(_READ, "/join", False, "自助上传页面导航（HTML 壳，不是业务写请求）。"),
    DrainExemption(_READ, "/join/", False, "同上（带斜杠）。"),
    DrainExemption(_READ, "/j", False, "短链入口的页面导航（HTML 壳）。"),
    DrainExemption(_READ, "/", False, "根路径到 /admin 的重定向（文档导航）。"),
)


def is_drain_exempt(path: str, method: str = "GET") -> bool:
    """这条「方法 + 路径」是否不计入在途。默认不豁免。"""
    verb = (method or "GET").upper()
    normalized = path or "/"
    # 精确项容忍一个尾斜杠：Starlette 默认 redirect_slashes，/x/ 与 /x 是同一
    # 路由；307 跳转本身很便宜，真正要放行的是跳转后的那次请求。
    stripped = normalized.rstrip("/") or "/"
    for exemption in DRAIN_EXEMPTIONS:
        if verb not in exemption.methods:
            continue
        if exemption.prefix:
            if normalized.startswith(exemption.path):
                return True
        elif stripped == (exemption.path.rstrip("/") or "/"):
            return True
    return False


# ---------------------------------------------------------------------------
# 排空拒绝响应（原生 ASGI，不读请求体）
# ---------------------------------------------------------------------------


def drain_envelope(path: str) -> dict[str, Any]:
    """按协议挑信封（CONTRACT 第 2 节）。"""
    normalized = (path or "/").rstrip("/") or "/"
    if normalized == "/mcp":
        # MCP 走 JSON-RPC：HTTP 层明确 503，不为取 RPC id 预读请求体。
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32000, "message": DRAIN_BUSINESS_CODE}}
    if normalized == "/v1/messages" or normalized.startswith("/v1/messages/"):
        # Anthropic Messages：流式错误事件同形。
        return {"type": "error",
                "error": {"type": DRAIN_BUSINESS_CODE, "message": DRAIN_MESSAGE}}
    # OpenAI 兼容路径（/v1/chat/completions、/v1/completions、/v1/responses …）
    # 与其它管理台接口共用 error object 形状。
    return {"error": {"message": DRAIN_MESSAGE, "type": DRAIN_BUSINESS_CODE,
                      "code": DRAIN_BUSINESS_CODE}}


def cors_headers_for(origin: str | None) -> dict[str, str]:
    """给生命周期自己生成的响应补 CORS 头。

    层序要求 CORSMiddleware 在生命周期**内层**，排空 503 在它之前就返回，
    内层看不到这条请求。不自己补的话，浏览器会把「服务正在重启」读成 CORS
    失败而不是可重试的服务未就绪。
    """
    if not origin:
        return {}
    from admin.config import settings

    allowed = [str(item) for item in (getattr(settings, "CORS_ORIGINS", None) or [])]
    if not allowed:
        return {}
    if "*" in allowed:
        return {"access-control-allow-origin": "*"}
    if origin in allowed:
        return {"access-control-allow-origin": origin, "vary": "Origin"}
    return {}


def _header_value(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


async def send_draining_response(scope: dict[str, Any], send) -> None:
    """送一条排空拒绝：503 + Retry-After + no-store + 协议信封。

    只读请求头（Origin），**不读请求体**，也不缓存任何输出。

    ## 为什么必须显式下发 Connection: close

    排空拒绝发生在读完请求体之前（有的请求体是几十 MB 的上传，有的根本还没发完）。
    HTTP/1.1 语义上，服务端在没有消费完请求体时**不能**保证这条连接上的字节边界还
    对齐 —— 客户端要是把剩下的分块/正文继续写进来，同一连接上的下一个请求就可能被
    解析成残帧（h11 会回 400）。所以这里显式声明连接关闭：服务端发完 503 就关，
    后续字节一律丢弃，客户端必须重新建连再重试（配合 Retry-After 正好）。
    这是与既有 429（abuse_guard）和准入拒绝同类的平台取舍：都不为一条拒绝去读完
    请求体，都靠关闭连接保证协议安全。
    """
    body = json.dumps(drain_envelope(scope.get("path") or "/"),
                      ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"retry-after", str(RETRY_AFTER_SECONDS).encode("ascii")),
    ]
    # 请求体没读完就回答：HTTP/1.x 必须声明关闭，避免同一连接上的后续字节被当成
    # 新请求（h11 会回 400）。HTTP/2 每条流自成边界，Connection 是禁止发送的
    # hop-by-hop 头，所以只在 HTTP/1.x 上加。
    if str(scope.get("http_version") or "1.1").startswith("1."):
        headers.append((b"connection", b"close"))
    for name, value in cors_headers_for(_header_value(scope, b"origin")).items():
        headers.append((name.encode("latin-1"), value.encode("latin-1")))
    try:
        await send({"type": "http.response.start", "status": 503,
                    "headers": headers})
        await send({"type": "http.response.body", "body": body})
    except (OSError, RuntimeError) as exc:  # 客户端已断开：没有别的可做
        logger.debug("排空拒绝响应发送失败（客户端可能已断开）：%s", exc)


class LifecycleMiddleware:
    """原生 ASGI 生命周期边界：原子准入 + 在途租约 + 排空拒绝。

    不读请求体、不缓存输出：业务分支把 receive/send 原样交给内层应用，只在
    finally 里释放租约，所以既有的限流、超时、取消与计费语义完全不变。

    层序（外层在前）：封禁/IP 防护 -> 生命周期 -> TimingMiddleware -> 准入队列
    -> 业务 dispatch。注册见 admin/server.py。
    """

    def __init__(self, app, lifecycle: Lifecycle | None = None):
        self.app = app
        self._lifecycle = lifecycle

    @property
    def lifecycle(self) -> Lifecycle:
        return self._lifecycle if self._lifecycle is not None else get_lifecycle()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            # WebSocket / lifespan 等非 HTTP scope 原样透传。
            return await self.app(scope, receive, send)

        path = scope.get("path") or "/"
        method = (scope.get("method") or "GET").upper()
        if is_drain_exempt(path, method) or env_guard.is_prod_blocked(path):
            # 豁免路径照常服务；生产封禁路径放行到内层旧策略，确保仍是 404。
            return await self.app(scope, receive, send)

        state = self.lifecycle
        lease = state.try_admit("request")
        if lease is None:
            return await send_draining_response(scope, send)
        try:
            await self.app(scope, receive, send)
        finally:
            # 应用清理与审计收尾都在这之后才结束，租约此时才释放。
            state.finish(lease)


# ---------------------------------------------------------------------------
# 进程单例
# ---------------------------------------------------------------------------

_SINGLETON: Lifecycle | None = None
_SINGLETON_LOCK = threading.Lock()


def get_lifecycle() -> Lifecycle:
    """进程内单例（首次调用创建）。

    中间件每个请求都要取一次，所以走「先读、没有才抢锁」的快路径：正常情况
    只是一次属性读，不引入额外锁竞争。
    """
    global _SINGLETON
    singleton = _SINGLETON
    if singleton is not None:
        return singleton
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = Lifecycle()
        return _SINGLETON


async def wait_empty() -> None:
    """模块级便捷函数：等到进程单例的两种计数都归零。"""
    await get_lifecycle().wait_empty()
