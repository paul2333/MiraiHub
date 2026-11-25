"""后台任务执行器：把耗时批量操作从 HTTP 请求里挪出去。

为什么需要
----------
批量做任务 / 猫猫旅行是「串行 + 限速」的：每个账号要等 accept 落库、
逐次触发、复查进度。实测一个账号约 27 秒，xx 个账号就是 6 分钟以上。

这类操作如果直接在请求里同步跑：
  * 请求会一直挂到跑完，nginx 的 proxy_read_timeout（默认 60s）先到点，
    前端直接吃 504，体感就是「点了没反应」；
  * 虽然 FastAPI 给同步端点配了线程池（40），单个批量只占 1 个线程，
    不会拖垮并发，但用户体验依然很差。

做法
----
POST 立即返回 job_id，真正的执行放到守护线程里；前端拿 job_id 轮询进度。
同一个 job 键（如 "cat_travel"）复用一个 runner，因此「重复点击」不会
叠起多个并发批量 —— 第二个请求会直接收到「正在执行」，而不是排队再跑一遍。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger("admin.jobrunner")

#: 已完成任务在内存里保留多久（秒），供前端取最终结果
_KEEP_DONE_SECONDS = 1800

#: 封口后拒绝新派发时写进 Job.error 的一行原因（前端直接显示）。
DRAINING_MESSAGE = "服务正在排空（service_draining），本次后台任务未启动"


def _lifecycle():
    """进程生命周期单例。

    延迟导入：本模块被路由层在很早的阶段导入，而 admin.lifecycle 会拉
    admin.env_guard（读配置）。放到调用点既避免导入顺序敏感，也让测试可以
    只替换单例状态。
    """
    from admin.lifecycle import get_lifecycle

    return get_lifecycle()


def admission_closed() -> bool:
    """此刻是否已经封口（draining / stopping / stopped / failed）。

    「脱离请求」的新工作一律在启动线程或调度 task **之前**问这一句：封口之后
    起一个没被计数的线程，等于让进程在它跑完之前退出（审计/记账就会半截）。
    """
    try:
        return _lifecycle().snapshot()["phase"] not in ("starting", "ready")
    except Exception:  # noqa: BLE001 - 生命周期坏了不能反过来卡住业务
        logger.debug("读取生命周期阶段失败，按未封口处理", exc_info=True)
        return False


def take_job_lease(*, parent=None):
    """为**脱离请求**的工作取一个 job 租约；已封口时返回 None。

    调用顺序是契约：

    1. 必须在启动线程 / 调度 task **之前**调用；
    2. 如果这份工作是从某个已准入请求派生出来的（parent 传那条请求的租约），
       必须在**释放父租约之前**调用 —— Lifecycle.try_admit 在它自己的锁内
       完成「阶段判定 + 登记」，所以子租约一旦登记成功，父租约随后被外层
       中间件释放也不会出现「计数瞬间归零」的窗口。这就是「先在锁内登记
       子租约、再释放父租约」。

    返回 None 表示服务已经封口：调用方必须放弃派发，**不许**运行一份没被
    计数的后台工作（否则排空会误判归零）。
    """
    if parent is not None and getattr(parent, "kind", "") != "request":
        # 只有请求租约才是「父」；其它种类（job）派生再派生时按独立工作处理。
        logger.debug("take_job_lease 的 parent 不是请求租约：%r", parent)
    try:
        child = _lifecycle().try_admit("job")
    except Exception:  # noqa: BLE001
        logger.exception("获取后台任务租约失败，按已封口处理")
        return None
    if child is None:
        logger.debug("服务已封口，派生工作未登记（父租约 %r 仍由调用方持有）", parent)
    return child


def release_job_lease(lease) -> None:
    """释放一份 job 租约（幂等；None 直接忽略）。"""
    if lease is None:
        return
    try:
        _lifecycle().finish(lease)
    except Exception:  # noqa: BLE001 - 释放失败只是计数不准，不能掩盖原始异常
        logger.exception("释放后台任务租约失败")

#: 进度里最多保留多少条账号明细。
#:
#: 账号池是几千~几万级：如果 `items` 无上限增长，每次轮询（1.5s）都要把
#: 几万条结果序列化一遍传给前端 —— 网络和后端 CPU 都被这条进度接口吃掉，
#: 界面看起来就是「卡死 + 不刷新」。这里只留最近 N 条用于滚动展示，
#: 完成数由 `done` 单独计数，进度百分比依旧准确。
_MAX_ITEMS = 500


class Job:
    """一次后台批量执行的进度与结果。"""

    def __init__(self, key: str, total: int, title: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.title = title
        self.total = total
        self.done = 0
        self.status = "running"          # running | finished | failed
        self.phase = "准备中"
        self.started_at = datetime.utcnow()
        self.finished_at: datetime | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.items: list[dict] = []      # 每个账号的实时结果
        self.current: str = ""           # 正在处理的账号名（心跳）
        self.beat_at: datetime | None = None
        #: 细粒度进度：当前账号已完成的任务（用于「正在做 xxx 3/15」）。
        #: 只有账号级进度时，一个账号要跑几分钟，界面会长时间不动 ——
        #: 用户以为卡死了。这里让前端能显示到任务级。
        self.task_total: int = 0
        self.task_done: int = 0
        self.task_current: str = ""
        self._lock = threading.Lock()

    def add_item(self, item: dict) -> None:
        """记录一个账号的结果（线程安全）。

        完成数以 `done` 为准（单调递增），`items` 只保留最近 `_MAX_ITEMS` 条，
        避免几万账号的明细把内存和进度接口撑爆。
        """
        with self._lock:
            self.items.append(item)
            if len(self.items) > _MAX_ITEMS:
                del self.items[:len(self.items) - _MAX_ITEMS]
            self.done += 1
            self.current = ""
            # 换账号了：任务级计数归零，重新数
            self.task_done = 0
            self.task_current = ""
            self.task_total = 0

    def beat(self, account: str = "") -> None:
        """心跳：表示「还活着，正在处理某个账号」。

        慢账号（上游要等几秒）如果什么都不上报，前端会一直显示同一个
        进度，看起来像卡死。心跳让界面能显示「正在处理 xxx」。
        """
        with self._lock:
            self.current = account
            self.beat_at = datetime.utcnow()
            self.task_done = 0
            self.task_current = ""
            self.task_total = 0

    def task_beat(self, account: str = "", item: dict | None = None) -> None:
        """细粒度心跳：一个任务处理完了。

        没这个的话，前端在一个账号上最多要等好几分钟都看不到任何变化。
        """
        with self._lock:
            self.task_done += 1
            self.beat_at = datetime.utcnow()
            if account:
                self.current = account
            if item:
                self.task_current = str(item.get("title") or item.get("task_code") or "")
            # 预估总数：账号级总数 × 每个账号约 15 个任务，仅用于显示「已做 N 个」
            self.task_total = max(self.task_total, self.task_done)

    def task_start(self, account: str, item: dict) -> None:
        """Expose the current stage before a slow upstream call starts."""
        with self._lock:
            self.current = account
            self.task_current = str(item.get("title") or item.get("task_code") or "")
            self.beat_at = datetime.utcnow()

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def set_done(self, done: int, note: str | None = None) -> None:
        """轻量进度更新：只改完成数（可选附带一行状态文字），不往 items 塞数据。

        导出 / 导入这类「逐条处理但不需要逐条结果」的场景必须用这个，
        否则把十万级结果全塞进 job.items 会把内存撑爆，而且前端也渲染不过来。
        snapshot() 的 percent 直接读 self.done，所以进度条照常工作。
        """
        with self._lock:
            self.done = done
            if note is not None:
                self.current = note
            self.beat_at = datetime.utcnow()

    def snapshot(self) -> dict:
        """给前端的进度快照（不含内部字段）。"""
        elapsed = ((self.finished_at or datetime.utcnow())
                   - self.started_at).total_seconds()
        return {
            "job_id": self.id,
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "total": self.total,
            "done": self.done,
            "elapsed_s": round(elapsed, 1),
            "percent": int(self.done / self.total * 100) if self.total else 0,
            "current": self.current,
            "task_done": self.task_done,
            "task_current": self.task_current,
            "items": list(self.items),
            # 明细被截断时前端需要知道：items 只是「最近 N 条」，不是全部
            "items_total": self.done,
            "items_truncated": self.done > len(self.items),
            "result": self.result,
            "error": self.error,
        }


class JobRunner:
    """按 key 管理后台任务；同 key 同时只允许一个在跑。"""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Job | None:
        with self._lock:
            self._purge_locked()
            return self._jobs.get(key)

    def by_id(self, job_id: str) -> Job | None:
        with self._lock:
            self._purge_locked()
            for j in self._jobs.values():
                if j.id == job_id:
                    return j
        return None

    def is_running(self, key: str) -> bool:
        j = self.get(key)
        return bool(j and j.status == "running")

    def _purge_locked(self) -> None:
        """清掉过期的已完成任务，避免内存无限增长。"""
        now = datetime.utcnow()
        dead = [
            k for k, j in self._jobs.items()
            if j.status != "running" and j.finished_at
            and (now - j.finished_at).total_seconds() > _KEEP_DONE_SECONDS
        ]
        for k in dead:
            self._jobs.pop(k, None)

    def start(self, key: str, total: int, worker: Callable[[Job], dict],
              title: str = "", *, parent_lease=None) -> Job:
        """启动一个后台任务。

        worker 收到 Job，负责逐个处理并调用 job.add_item() 上报进度，
        返回值会作为 job.result 存下来。

        ## 租约边界（优雅退出任务 2）

        * 线程**启动之前**先取 job 租约；线程启动失败、正常结束、抛异常都在
          _run 的同一次所有权里释放 —— 计数不会漏减也不会重复减。
        * 已经有同键任务在 running 时直接复用：复用**不新增计数**（否则点两次
          就会多算一个「后台任务」，排空永远等不完）。
        * 服务已封口（draining 及以后）时不再派发：返回一个 failed 的 Job 并
          写明原因，绝不启动一份没被计数的线程。
        * parent_lease：已准入请求派生出的必要工作在释放父租约之前调用时传入，
          保证「子租约先登记、父租约后释放」。

        变更记录：该文件的租约接入曾被工作区里的一次并发 git 操作（stash /
        checkout / cherry-pick）抹掉，此处按原设计恢复。
        """
        job = Job(key, total, title=title)
        with self._lock:
            self._purge_locked()
            running = self._jobs.get(key)
            if running and running.status == 'running':
                return running
            self._jobs[key] = job

        # 锁外取租约：try_admit 自己带锁，别在持有 JobRunner 锁时跨锁。
        lease = take_job_lease(parent=parent_lease)
        if lease is None and admission_closed():
            # 只有**真正封口**（draining / stopping / stopped / failed）才拒绝派发。
            # 注意 `try_admit` 只在 PHASE_READY 放行，所以「还没走完启动」
            # （starting，测试进程与 lifespan 完成前都处在这个阶段）也会拿到
            # None —— 那不是封口，不能把任务判死：以前这样做的后果是
            # `/api/accounts/export-async` 永远返回一个 failed 的 Job 且已被
            # 移出注册表，随后 `/job/{id}` 一律 404「任务不存在或已过期」。
            with self._lock:
                if self._jobs.get(key) is job:
                    self._jobs.pop(key, None)
            job.status = "failed"
            job.phase = "未启动"
            job.error = DRAINING_MESSAGE
            job.finished_at = datetime.utcnow()
            logger.info("服务已封口，拒绝派发后台任务 key=%s", key)
            return job

        def _run(job: Job, lease) -> None:
            # 租约跨线程边界**显式传入**：谁启动线程谁就交出所有权，线程自己
            # 在 finally 里释放（正常/异常同一处）。
            try:
                job.result = worker(job)
                job.status = "finished"
            except Exception as e:  # 后台线程里必须兜住，否则静默丢失
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
            finally:
                job.finished_at = datetime.utcnow()
                release_job_lease(lease)

        try:
            threading.Thread(target=_run, args=(job, lease), daemon=True,
                             name=f"wb-job-{key}").start()
        except BaseException:
            # 线程没起来：租约还在这条所有权上，就地释放再把异常抛回调用方。
            with self._lock:
                if self._jobs.get(key) is job:
                    self._jobs.pop(key, None)
            release_job_lease(lease)
            raise
        return job


#: 全局单例
RUNNER = JobRunner()


def wait_for(predicate: Callable[[], bool], timeout: float,
             interval: float = 0.4) -> bool:
    """轮询等待条件成立，最多等 timeout 秒。

    用来替代「固定 sleep N 秒」：上游快时立刻返回，慢时也不会误判。
    返回是否在超时前成立。
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if predicate():
                return True
        except Exception:
            pass  # 查询本身出错不算命中，继续试到超时
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)