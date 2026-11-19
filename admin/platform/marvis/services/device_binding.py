"""账号 ↔ 真实设备的绑定（**方案骨架，2026-11-18；本轮不接线、不部署**）。

## 为什么需要它（缺陷 B：8 个号共用 1 个 qimei36，却有 8 个随机 guid）

线上现状：

  · MARVIS_QIMEI36 没设 → uskey.qimei36() 全部退回内置常量
    QIMEI36_FALLBACK = 84097bfe...bd1a611（core/uskey.py:66 / uskey_local.py:390）；
  · 而 guid 是 services/accounts.py:437 / 645 在「凭证没带 guid」时补的
    uuid.uuid4() —— 每个号一个新的随机 UUID；
  · 结果：8 个账号 = 1 个设备串 + 8 个不同 guid。

真机不可能长这样：一个物理设备 = 一组 (guid, qimei36)，两者同源、一荣俱荣。
上游拿到「同一台设备报出 8 个不同 guid」这种形状，等于把「这是一个账号簇」
直接写在脸上 —— 这就是教科书级的簇特征。

## 本轮范围（重要）

  ✅ 只提供**纯函数骨架** + 设计文档（docs/marvis-device-binding.md）；
  ❌ 不改 accounts 表 schema（绑定信息塞进已有的 auth_json，见下）；
  ❌ 不接线：本模块**没有任何生产调用点**，import 它不会改变任何行为；
  ❌ 不改 QIMEI36_FALLBACK 常量（那一串是签名素材，动了连 200 都不会有）。

## 数据落点：auth_json 里加一个 device 键

    auth_json = {
        "openid": "...", "access_token": "...", "refresh_token": "...",
        "guid": "...",                  # 已有列（历史遗留的账号级 guid）
        "device": {                     # ← 新增：设备绑定（本模块负责）
            "guid": "...",              # 该号**登录时**所用设备的 guid
            "qimei36": "...",           # 同一台设备的 getQimei()（36 hex）
            "bound_at": 1763500000,     # 绑定时刻（unix 秒）
            "source": "login",          # login / manual / migrated
        },
    }

为什么不用新列：schema 变更要走迁移（本轮明确不做），而 auth_json 本来就是
「凭证 + 设备串」的落点（services/accounts.py 全程按它序列化），加一个键是
纯 JSON 层面的事，回滚 = 删掉这个键。

## 接线顺序（下一轮做，本轮只留接口）

  1. 登录落地（services/accounts.py 的 login/refresh 写回处）→ bind_device()；
  2. mint 前（core/endpoints.py::llm_headers 的调用方）→ require_device()，
     把返回的 guid / qimei36 显式传下去，**两处必须是同一份**；
  3. 没绑定的号 → 抛 DeviceBindingMissing，网关回
     400 checkin_required（文案：请重新扫码登录以采集设备指纹），
     **绝不**退回 QIMEI36_FALLBACK 硬发 —— 那正是这次把簇送进风控的动作。
"""
from __future__ import annotations

import time
import uuid

#: auth_json 里承载设备绑定的键名。
DEVICE_KEY = "device"
#: 一条绑定至少要有这两项，且必须**同源**（同一台设备采集）。
REQUIRED_FIELDS = ("guid", "qimei36")
#: 允许的绑定来源（写进 device.source，排障时一眼看出是真机采集还是历史迁移）。
SOURCES = ("login", "manual", "migrated")


class DeviceBindingMissing(Exception):
    """这个号没有可用的设备绑定 ⇒ 不许发 chat（对应网关的 checkin_required）。

    它是**拒绝发送**的信号，不是「换一个 fallback 继续」的信号：
    用共享常量 qimei36 + 随机 guid 硬发的代价已经付过一次（整个设备簇被风控）。
    """

    code = "checkin_required"

    def __init__(self, message: str = "", *, account_id: int = 0):
        super().__init__(message or "该账号没有设备绑定，需要重新扫码登录")
        self.message = str(message or "该账号没有设备绑定，需要重新扫码登录")
        self.account_id = int(account_id or 0)


def is_valid_guid(value: str) -> bool:
    """guid 是否可用：非空、长度合理、不是占位符。"""
    text = str(value or "").strip()
    if len(text) < 8 or len(text) > 64:
        return False
    return text.lower() not in ("0", "null", "none", "undefined", "unknown")


def is_valid_qimei36(value: str) -> bool:
    """qimei36 是否可用：必须是 36 个 hex 字符（18 字节，见 uskey_local.build_tlv）。"""
    text = str(value or "").strip()
    if len(text) != 36:
        return False
    try:
        bytes.fromhex(text)
    except ValueError:
        return False
    return True


def device_of(credential: dict) -> dict:
    """取 device 绑定；没有 / 畸形返回 {}。**绝不抛**（探针路径也要能用）。"""
    data = credential or {}
    raw = data.get(DEVICE_KEY) if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return normalize_device(raw)


def normalize_device(raw: dict) -> dict:
    """把任意来源的 device 规整成统一形状；缺字段 / 不合法返回 {}。"""
    if not isinstance(raw, dict):
        return {}
    guid = str(raw.get("guid") or "").strip()
    qimei36 = str(raw.get("qimei36") or "").strip()
    if not is_valid_guid(guid) or not is_valid_qimei36(qimei36):
        return {}
    try:
        bound_at = int(raw.get("bound_at") or 0)
    except (TypeError, ValueError):
        bound_at = 0
    source = str(raw.get("source") or "").strip()
    return {
        "guid": guid,
        "qimei36": qimei36,
        "bound_at": bound_at,
        "source": source if source in SOURCES else "unknown",
    }


def require_device(credential: dict, *, account_id: int = 0) -> dict:
    """mint 前的闸门：没有合法绑定就抛 DeviceBindingMissing。

    ⚠ 这里**不允许**任何 fallback（不许读 MARVIS_QIMEI36、不许读
    uskey.QIMEI36_FALLBACK、不许现编 guid）—— 缺绑定就是缺采集，
    正确动作是让这个号回到「需要扫码」的状态。
    """
    device = device_of(credential)
    if not device:
        raise DeviceBindingMissing(
            "该账号没有设备绑定（guid + qimei36 同源），需要重新扫码登录。",
            account_id=account_id)
    return device


def bind_device(credential: dict, *, guid: str, qimei36: str,
                source: str = "login", now: int | None = None) -> dict:
    """把一对设备串写进 credential["device"] 并返回它（**调用方负责 commit**）。

    写入前必须校验：guid 合法、qimei36 是 36 hex、来源在白名单里。
    不合格直接抛 ValueError —— 静默吞掉只会让「绑定成功」变成谎话。
    """
    if not is_valid_guid(guid):
        raise ValueError("guid 不合法：%r" % (guid,))
    if not is_valid_qimei36(qimei36):
        raise ValueError("qimei36 不合法（必须是 36 hex）：%r" % (qimei36,))
    if source not in SOURCES:
        raise ValueError("source 必须是 %r 之一：%r" % (SOURCES, source))
    device = {
        "guid": str(guid).strip(),
        "qimei36": str(qimei36).strip(),
        "bound_at": int(now if now is not None else time.time()),
        "source": source,
    }
    if not isinstance(credential, dict):
        raise TypeError("credential 必须是 dict")
    credential[DEVICE_KEY] = device
    return device


def new_guid_seed() -> str:
    """历史账号补齐时的**唯一**合法用法：先扫码、再采集。

    这里保留一个显式函数只是为了让 grep 好找 ——
    生产路径**不许**用它当「没 guid 就编一个」的兜底（那正是缺陷 B 的成因，
    见 services/accounts.py:437 的 uuid.uuid4()）。
    """
    return str(uuid.uuid4())


def device_fingerprint(device: dict) -> str:
    """设备指纹摘要（日志/审计用，**不可逆、不含明文**）。"""
    import hashlib

    guid = str((device or {}).get("guid") or "")
    qimei36 = str((device or {}).get("qimei36") or "")
    if not guid and not qimei36:
        return ""
    blob = (guid + "|" + qimei36).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]
