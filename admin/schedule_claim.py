"""定时任务「先认领、再执行」（修复批次 3 · D6，审查 C-1）。

## 为什么需要它

调度器原来是「列出到期任务 → 执行 → 执行完再写 next_run_at」，中间没有任何认领：

  * 手动「执行」（`POST /api/schedules/{id}/run`）在请求线程里同步跑，和调度线程
    之间没有互斥，连点两下就是两次并发；
  * 多 worker、桌面版与服务器指向同一个库、换代时新旧进程重叠，都会让同一个
    到期任务被执行两次。

## 怎么做

每个任务一把**数据库 CAS 租约**，存在 `system_settings`（键 `schedule:claim:<id>`，
值 `{"owner","source","at","expires"}`），和成长巡检 `growth_plan._Lease` 同一套
写法：读当前值 → 未过期则认领失败 → 否则 `UPDATE ... WHERE value = 旧值`，
影响行数为 1 才算拿到。不加表、不加列，SQLite / MySQL 通用。

  * 调度线程：拿不到 = 别处正在跑，本轮跳过（不改 next_run_at）；
    拿到后**重新读一次** schedule，确认它仍然到期（防止别的进程刚跑完、
    已经把 next_run_at 推后）。
  * 手动执行：拿不到 = HTTP 409「任务正在执行」。
  * 执行结束（含异常）在 finally 里释放；进程崩溃时由 `expires` 兜底过期。

## 环境变量

  * `ADMIN_SCHEDULE_CLAIM`               默认 `1`；设 `0` 关闭认领（退回旧行为）
  * `ADMIN_SCHEDULE_CLAIM_TTL_SECONDS`   租约兜底时长，默认 7200（2 小时），
                                         范围 60 ~ 86400。只在进程崩溃、没来得及
                                         释放时起作用：最长晚这么久才会再跑。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

_logger = logging.getLogger("admin.schedule_claim")

KEY_PREFIX = "schedule:claim:"
#: 本进程标识（排障用：看是谁持有租约）
PROCESS_TAG = "%s-%s" % (os.getpid(), uuid.uuid4().hex[:8])


def enabled() -> bool:
    raw = os.getenv("ADMIN_SCHEDULE_CLAIM", "1").strip().lower()
    return raw not in ("0", "false", "off", "no")


def ttl_seconds() -> int:
    raw = os.getenv("ADMIN_SCHEDULE_CLAIM_TTL_SECONDS", "").strip()
    try:
        value = int(float(raw)) if raw else 7200
    except ValueError:
        value = 7200
    return max(60, min(86400, value))


def key_of(schedule_id) -> str:
    return KEY_PREFIX + str(int(schedule_id))


def expires_of(value) -> float:
    """租约值里的过期时间戳；坏数据 / 空 = 0（视为空闲）。"""
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get("expires") or 0)
    except (TypeError, ValueError):
        return 0.0


def holder_of(value) -> dict:
    """租约持有者信息（给 409 文案用）；没有则空 dict。"""
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class Claim:
    """一次成功的认领。`release()` 幂等；也可当上下文管理器用。"""

    def __init__(self, key: str, value: str, session_factory=None, *, noop: bool = False):
        self.key = key
        self.value = value
        self._factory = session_factory
        self.noop = noop
        self.released = False

    def release(self) -> None:
        if self.released or self.noop:
            self.released = True
            return
        self.released = True
        try:
            from admin.models import SystemSetting

            factory = self._factory or _default_factory()
            with factory() as db:
                (db.query(SystemSetting)
                 .filter(SystemSetting.key == self.key, SystemSetting.value == self.value)
                 .update({SystemSetting.value: "{}"}, synchronize_session=False))
                db.commit()
        except Exception:  # noqa: BLE001 - 释放失败只会让租约按 TTL 自然过期
            _logger.exception("释放定时任务租约失败 key=%s（将按 TTL 过期）", self.key)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()
        return False


def _default_factory():
    from admin.db import SessionLocal

    return SessionLocal


def try_claim(schedule_id, *, source: str = "scheduler", session_factory=None,
              now: float | None = None):
    """尝试认领一个定时任务。

    返回 `Claim`（拿到了，务必 release）或 `None`（别处正在执行）。
    认领关闭（`ADMIN_SCHEDULE_CLAIM=0`）时返回一个空操作的 `Claim`。
    数据库异常向上抛 —— 调用方决定是跳过（调度线程）还是 503（手动执行）。

    用**独立会话**做认领：不碰调用方会话里的事务状态，认领一提交别的进程
    立刻可见。
    """
    key = key_of(schedule_id)
    if not enabled():
        return Claim(key, "", session_factory, noop=True)
    from sqlalchemy.exc import IntegrityError

    from admin.models import SystemSetting

    factory = session_factory or _default_factory()
    moment = time.time() if now is None else float(now)
    with factory() as db:
        row = db.get(SystemSetting, key)
        if row is None:
            db.add(SystemSetting(key=key, value="{}"))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
            row = db.get(SystemSetting, key)
        db.refresh(row)
        previous = row.value
        if expires_of(previous) > moment:
            return None
        value = json.dumps({"owner": "%s:%s" % (PROCESS_TAG, uuid.uuid4().hex[:8]),
                            "source": str(source)[:20], "at": moment,
                            "expires": moment + ttl_seconds()}, ensure_ascii=False)
        changed = (db.query(SystemSetting)
                   .filter(SystemSetting.key == key, SystemSetting.value == previous)
                   .update({SystemSetting.value: value}, synchronize_session=False))
        db.commit()
    if not changed:
        return None
    return Claim(key, value, factory)


def current_holder(schedule_id, *, session_factory=None) -> dict:
    """当前持有者（未过期才返回），供 409 提示「谁在跑、什么时候开始的」。"""
    from admin.models import SystemSetting

    factory = session_factory or _default_factory()
    try:
        with factory() as db:
            row = db.get(SystemSetting, key_of(schedule_id))
            value = row.value if row is not None else ""
    except Exception:  # noqa: BLE001
        return {}
    if expires_of(value) <= time.time():
        return {}
    return holder_of(value)
