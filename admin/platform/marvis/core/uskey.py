"""Marvis uskey —— 纯 Python 本地 mint（唯一签名路径）。

## 只有一条路径

算法全在 `core/uskey_local.py`：三段（TEA 双反馈 → 异或掩 → AES-128-CBC）
已经逆完，4/4 条真实 uskey 逐字节复现；`dev18` 用 `os.urandom(18)` 全随机
上游也是 HTTP 200（2026-11-18 实测 1.90 s，真实出字）。所以本模块只做三件事：
拼 bus_info、按账号取设备串、调 :func:`uskey_local.mint_uskey`。

**2026-11-20 起只有这一条路**：仓库里没有任何外部签名服务、DLL 或
子进程代签，本模块也不再有任何 HTTP 回落。签不出来就抛 :class:`MintError`，
上层当「平台暂时不可用」处理 —— **绝不**退化成随机 uskey：
那会把「签名不可用」变成「4100404 风控」，还会把风控计数算到账号头上。

## 历史坑（别再踩）

当年手工拼 `magic + F(bus_info) + 随机尾` 100% 撞 `4100404`，
是因为 TLV 里还有 md5(bus_info)、两处 qimei36、15 字节进程常量
和 TEA/异或/AES 三层；**不是**"必须由 SDK 出"。现在三层都在
`uskey_local.py` 里，且偏移有校准常量（`OFF_*`），别手调。

## 铁律

* 签名失败就抛异常，**绝不**填充随机字节 —— 见上面那段。
* 每组 `(cid, rid, nonce, uskey)` 只能用一次；撞 4100404 必须**整组换新**。
* `qimei36` 必须与签这块 uskey 时用的那串**完全一致**（它写进 TLV 两处）；
  优先 qimei 真值表，没有才按账号派生；不允许共享常量。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import uuid

logger = logging.getLogger("admin.platform.marvis.uskey")

#: Beacon 的业务 id —— 只作服务端路由标识，不参与运算。
BUSINESS_ID = "7800255"

#: uskey 的 base64 头（= magic `0ac0d50c2111` 的前 8 字符）。只做体检。
USKEY_HEAD = "CsDVDCE"

#: qimei36 派生：每个账号一台"虚拟设备"。
#:
#: 2026-11-18 事故：网关曾用同一个**真机** qimei36 常量给 8 个账号签名，
#: 8 个不同 guid 共用一个 qimei => 上游把这台"设备"整体标记，连真客户端
#: 一起 4100404。真机常量已删除，**不要把它写回代码**。
#:
#: 形态（2026-11-18 由三个真实样本逆出）：
#:   字符 0-23   (字节 0-11 ) 12 字节设备摘要，随环境变
#:   字符 24-27  (字节 12-13) '3000' —— 跨环境恒定，是**格式标记**，不是数据
#:   字符 28-35  (字节 14-17) 4 字节尾段，可变
#: 旧实现是 md5()[:34] + "ab12"：'3000' 格式标记被冲掉（只有 1/65536 概率撞上），
#: 且 8 个账号尾段全是 ab12 —— 格式非法 + 尾段聚簇，等于自报"假的"。
#:
#: 现在：sha256("mh-qimei:" + 账号标识) 铺成 24hex + '3000' + 8hex。
#: 同一账号永远同一串，不同账号必然不同串，且结构合法。
DEVICE_TAG = "mh-qimei:"
#: 字符 24-27 的格式标记：三个真实样本全带，缺了就不是合法 qimei36。
DEVICE_MARK = "3000"

#: 字节下限 = 真品口径 **376**（504 字符 base64）。
#:
#: 历史：300 → 240 是因为旧实现漂移出 264 字节的包，账号 190 打过去居然 200，
#: 于是把阈值降下来"放行"。2026-11-18 真客户端 200 抓包证明真品是 376 字节，
#: 264 那版是「旧形态 + 仿的身份」（SDK 1.0.0.0 / node.exe / Not Specified、
#: 没有 320 字节业务 TLV），后来上游收紧即全线 4100404。
#: 现在本地 mint 只会产出 376，任何短包都是 bug，必须**在本地拦死**，
#: 别拿 403 去污染号池。要临时放行用 MARVIS_USKEY_MIN_BYTES 覆盖。
USKEY_MIN_BYTES = int(os.environ.get("MARVIS_USKEY_MIN_BYTES") or 376)


class MintError(RuntimeError):
    """本地 mint 不可用，且调用方要求 **fail-closed**（不许用随机字节顶替）。

    ## 为什么需要这个异常（fail-closed 的理由）

    签名不可用时如果「随机填充 376 字节」，看上去网关还活着，实际后果：

      · 上游一定回 `4100404 请求被风控拦截`；
      · 上层会把这个 4100404 记成**当前账号**的失败并挂冷却 ——
        一个环境问题被写成「账号被风控」，用户去换号、重扫码、换 Key，
        全是无效动作；
      · 连撞几个号，整池进入风控冷却，网关自己开始回 `503 accounts_cooling`，
        真正的故障点被彻底掩盖。

    fail-closed 的做法是：**签不出来就别发请求**，把原因直说。
    """

    def __init__(self, message: str, *, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


#: 测试与离线环境的注入点：`set_signer(lambda **kw: {...})`。
_signer = None


def set_signer(fn=None):
    """注入/清空签名实现（`fn(**kwargs) -> {"uskey": ..., "qimei36": ...}`）。"""
    global _signer
    _signer = fn
    return _signer


def fail_closed() -> bool:
    """签名不可用时是否 **fail-closed**（默认开启，与 endpoints.uskey_fail_closed 同义）。

    `MARVIS_USKEY_FAIL_CLOSED=0` 可退回旧行为（随机填充）。线上主路径要的是
    「宁可 503，也不要拿随机指纹去撞风控并污染账号池」；只有探针要复现
    「随机 uskey → 4100404」的历史现象时才关。
    """
    raw = (os.getenv("MARVIS_USKEY_FAIL_CLOSED") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def new_identity() -> dict:
    """每请求一组全新的 `(conversation_id, response_id, nonce)`。

    `conversation_id` 形如 `conv_` + 32 hex（与客户端抓包一致），
    `response_id` / `nonce` 是 uuid4。
    """
    return {
        "conversation_id": "conv_" + secrets.token_hex(16),
        "response_id": str(uuid.uuid4()),
        "nonce": str(uuid.uuid4()),
    }


def bus_info(*, agent_tag: str, conversation_id: str, response_id: str,
             scene: str, model: str, nonce: str, openid: str) -> str:
    """bus_info 七字段**固定顺序**拼接。

    顺序或分隔符错一个字符，`F(bus_info)` 就错，整块 uskey 被上游拒掉。
    这七项必须与 header / 请求体**一字不差**（model 取请求体原文）。
    """
    return ("marvisAgentTag=" + str(agent_tag) +
            "&marvisConversationID=" + str(conversation_id) +
            "&marvisResponseID=" + str(response_id) +
            "&marvisScene=" + str(scene) +
            "&model=" + str(model) +
            "&nonce=" + str(nonce) +
            "&originOpenID=" + str(openid))


def _validate(payload: dict) -> dict:
    """校验产物；不合法就抛 —— **绝不**兜底造一个 uskey。"""
    if not isinstance(payload, dict):
        raise MintError("签名结果不是 JSON 对象")
    uskey = str(payload.get("uskey") or "")
    qimei36 = str(payload.get("qimei36") or "")
    if not uskey:
        raise MintError("没有返回 uskey：%s" % payload.get("error") or payload)
    try:
        raw = base64.b64decode(uskey, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise MintError("返回的 uskey 不是合法 base64") from exc
    if len(raw) < USKEY_MIN_BYTES:
        raise MintError("返回的 uskey 太短（%d 字节 < %d；uskeyLen=%d qimei36=%s）" % (len(raw), USKEY_MIN_BYTES, len(uskey), qimei36[:8]))
    if not uskey.startswith(USKEY_HEAD):
        # 不硬失败：magic 变了说明算法有变，记一笔就够。
        logger.warning("uskey 头不是预期的 %s：前 8 字符 %s",
                       USKEY_HEAD, uskey[:8])
    if len(qimei36) != 36:
        logger.warning("qimei36 长度异常：%d", len(qimei36))
    return {"uskey": uskey, "qimei36": qimei36,
            "uskey_bytes": len(raw), "uskey_len": len(uskey),
            "bus_info": str(payload.get("busInfo") or ""),
            "ms": payload.get("ms")}


def qimei_for(account_key: str) -> str:
    """按账号派生**结构合法**的 qimei36：24hex 摘要 + '3000' + 8hex 尾段。

    同一账号恒定，不同账号互不相同。⚠ 这不是真机 qimei：真机值由设备采集
    （SMBIOS UUID / 磁盘序列号 / MAC / BSSID / GPU / CPU / 分辨率 / locale /
    计算机名）后注册得到，见 qimei 真值表；这里只是格式合法的占位值。
    """
    h = hashlib.sha256((DEVICE_TAG + str(account_key)).encode('utf-8')).hexdigest()
    value = h[:24] + DEVICE_MARK + h[24:32]
    assert len(value) == 36 and value[24:28] == DEVICE_MARK, value
    return value


def account_key_of(acct) -> str:
    """账号记录 -> 设备派生键：优先 uid，其次 id，最后 openid。"""
    acct = acct or {}
    for field in ('uid', 'id', 'openid'):
        value = str(acct.get(field) or '').strip()
        if value:
            return value
    return ''


def qimei36(account_key: str = '') -> str:
    """本地签名用的设备串：MARVIS_QIMEI36 > 按账号派生。

    这串必须与 header 的 Ual-Access-MarvisExt.qimei36 以及 TLV 里两处
    （OFF_QIMEI1 / OFF_QIMEI2）完全一致，所以只有这一个来源。

    没有账号标识时**直接报错**：绝不回退到共享常量 —— 多账号共用一个
    qimei 正是 2026-11-18 设备簇被上游整体标记的原因。
    """
    value = (os.environ.get('MARVIS_QIMEI36') or '').strip()
    if len(value) == 36:
        return value
    key = str(account_key or '').strip()
    if key:
        return qimei_for(key)
    raise ValueError(
        'qimei36 需要账号标识：设备串必须按账号派生，不允许使用共享常量')


def _local_sign(*, device_key: str = '', **kwargs) -> dict:
    """纯本地 mint 一块 uskey（两次系统调用：urandom + 时间戳，不碰网络）。

    算法、偏移、常量全部在 `core/uskey_local.py`；这里只负责把 bus_info 拼对、
    把 `qimei36` 带出去，并复用 :func:`_validate` 的字节下限体检。
    """
    from .uskey_local import mint_uskey
    info = bus_info(**kwargs)
    device = qimei36(device_key)
    value = mint_uskey(device, info, os.urandom(18))
    return _validate({"uskey": value, "qimei36": device,
                      "busInfo": info, "ms": 0})


def sign(*, agent_tag: str, conversation_id: str, response_id: str,
         scene: str, model: str, nonce: str, openid: str,
         account_key: str = '') -> dict:
    """取一块 uskey：**只有本地 mint 一条路**。

    返回值：`{"uskey", "qimei36", "uskey_len", "uskey_bytes", "bus_info", "ms"}`。

    顺序：(1) 注入的 `_signer`（测试/离线）；(2) `_local_sign()`。
    失败抛 :class:`MintError`（配置类错误抛 `ValueError`），
    上层当「平台暂时不可用」处理，**不是**「账号坏了」。
    """
    kwargs = dict(agent_tag=agent_tag, conversation_id=conversation_id,
                  response_id=response_id, scene=scene, model=model,
                  nonce=nonce, openid=openid)
    if _signer is not None:
        return _signer(**kwargs)
    return _local_sign(device_key=account_key, **kwargs)


# --- qimei_table override ---
#: 真实 qimei 表：每台"虚拟设备"由真实 MAC（换 MAC 即换 qimei）算出；
#: 这些值全部格式合法且从未被上游看到过。有值就用真值，没值才退回派生
#: （派生值会自报"假的"）。
_QIMEI_TABLE_CACHE = None


def _qimei_table() -> dict:
    """真值表读路径：经 qimei_assign 加载，顺带校验 2 号/台硬约束。

    池里 owners>2 时 QimeiPoolError 必须往外抛（启动时 install() 也会先校验），
    不能像普通读表失败那样吞掉。
    """
    global _QIMEI_TABLE_CACHE
    if _QIMEI_TABLE_CACHE is None:
        try:
            from .qimei_assign import QimeiPoolError, load_state
        except Exception as exc:  # noqa: BLE001
            logger.warning("qimei_assign 不可用，退回直接读表：%s", exc)
            try:
                import json as _json
                import os as _os
                path = _os.path.join(_os.path.dirname(__file__), "qimei_table.json")
                with open(path, encoding="utf-8") as fh:
                    _QIMEI_TABLE_CACHE = {str(k).upper(): str(v)
                                          for k, v in _json.load(fh).items()}
                logger.info("qimei 真值表已加载（未经池校验）：%d 条",
                            len(_QIMEI_TABLE_CACHE))
            except Exception as exc2:  # noqa: BLE001
                logger.warning("qimei 真值表加载失败（退回派生）：%s", exc2)
                _QIMEI_TABLE_CACHE = {}
            return _QIMEI_TABLE_CACHE
        try:
            _QIMEI_TABLE_CACHE = dict(load_state().table)
        except QimeiPoolError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("qimei 真值表加载失败（退回派生）：%s", exc)
            _QIMEI_TABLE_CACHE = {}
        else:
            logger.info("qimei 真值表已加载：%d 条（含 pool 归一化）",
                        len(_QIMEI_TABLE_CACHE))
    return _QIMEI_TABLE_CACHE


_qimei_for_derived = qimei_for


def qimei_for(account_key: str) -> str:
    """优先用真值表（MAC 生成的真 qimei），没有才退回派生。"""
    key = str(account_key or "").upper()
    hit = _qimei_table().get(key)
    if hit:
        return hit
    return _qimei_for_derived(account_key)
