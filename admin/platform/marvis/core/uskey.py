"""Marvis uskey 签名侧车客户端 —— 生产口径。

## 两条路径：本地 mint（默认）→ 侧车（降级）

**2026-11-18 起 uskey 可以纯 Python 本地生成**，算法在 `core/uskey_local.py`：
三段（TEA 双反馈 → 异或掩 → AES-128-CBC）全部逆出来了，4/4 条真实 uskey
逐字节复现，且 `dev18` 用 `os.urandom(18)` 全随机也是上游 **HTTP 200**
（2026-11-18 实测 1.90 s，真实出字）。所以默认路径是 `_local_sign()`。

侧车（Wine + Windows node.exe，见 `deploy/marvis-uskey-sidecar/`）保留为
**降级路径**：本地 mint 抛异常时才 POST 它（`_http_sign()`）。要强制走侧车
（排障 / 对照实验）设 `MARVIS_USKEY_FORCE_SIDECAR=1`。

历史坑（别再踩）：当年手工拼 `magic + F(bus_info) + 随机尾` 100% 撞
`4100404`，是因为 TLV 里还有 md5(bus_info)、两处 qimei36、15 字节进程常量
和 TEA/异或/AES 三层；**不是**"必须由 SDK 出"。现在三层都在
`uskey_local.py` 里，且偏移有校准常量（`OFF_*`），别手调。

## 侧车当年的证据（降级路径仍然可用）

Wine 生成的 uskey 是 480 字符 / 360 字节（真机 504/376），`getQimei()` 报的是
Wine 虚拟设备指纹 `3aeda2c4343993fc64eec8c830001591ab11`；这一组直打
`/v1/chat/completions`（账号 190）**上游同样 200**。
⇒ 服务端认的是「uskey 内部自洽 + bus_info 与 header 一致」，不是真机指纹。

## 铁律

* 本地 mint 失败**才**回落侧车；两条都失败就抛 `SidecarError`，
  **绝不**退化成随机 uskey —— 那会把「签名不可用」变成「4100404 风控」，
  还会把风控计数算到账号头上。
* 每组 `(cid, rid, nonce, uskey)` 只能用一次；撞 4100404 必须**整组换新**。
* `qimei36` 必须与签这块 uskey 时用的那串**完全一致**（它写进 TLV 两处）；
  默认走内置常量，换设备串设 `MARVIS_QIMEI36`。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger("admin.platform.marvis.uskey")

#: 侧车地址。MiraiHub 跑在容器里时 `127.0.0.1` 是**容器自己**，
#: 必须走宿主 docker0 网关，所以默认值是 `172.17.0.1`。
#: 侧车与 MiraiHub 同 netns（host 网络 / 本机调试）时用
#: `MARVIS_USKEY_SIDECAR=http://127.0.0.1:8791/uskey` 覆盖。
SIDECAR_URL = (os.environ.get("MARVIS_USKEY_SIDECAR")
               or "http://172.17.0.1:8791/uskey").strip()

#: 侧车超时（秒）。侧车正常是毫秒级，5 s 只用来兜住「Wine 卡死」。
SIDECAR_TIMEOUT = float(os.environ.get("MARVIS_USKEY_TIMEOUT") or 5.0)

#: Beacon 的业务 id —— 侧车固定用它调 `getUskey`。
BUSINESS_ID = "7800255"

#: uskey 的 base64 头（= magic `0ac0d50c2111` 的前 8 字符）。只做体检。
USKEY_HEAD = "CsDVDCE"

#: qimei36 派生：每个账号一台"虚拟设备"。
#:
#: 2026-11-18 事故：网关曾用同一个**真机** qimei36 常量给 8 个账号签名，
#: 8 个不同 guid 共用一个 qimei => 上游把这台"设备"整体标记，连真客户端
#: 一起 4100404。真机常量已删除，**不要把它写回代码**。
#:
#: 形态（2026-11-18 由三个真实样本逆出：真机 / 侧车 / Wine）：
#:   字符 0-23   (字节 0-11 ) 12 字节设备摘要，随环境变
#:   字符 24-27  (字节 12-13) '3000' —— 跨环境恒定，是**格式标记**，不是数据
#:   字符 28-35  (字节 14-17) 4 字节尾段，可变
#:    真机 84097bfe4bec892cb32e99d7 | 3000 | 1bd1a611
#:    侧车 36aa5d5bcd60d5878553c2b5 | 3000 | 1851ab12
#:    Wine 3aeda2c4343993fc64eec8c8 | 3000 | 1591ab11
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
#: 历史：300 → 240 是因为侧车漂移出 264 字节的包，账号 190 打过去居然 200，
#: 于是把阈值降下来"放行"。2026-11-18 真客户端 200 抓包证明真品是 376 字节，
#: 264 那版是「旧形态 + 仿的身份」（SDK 1.0.0.0 / node.exe / Not Specified、
#: 没有 320 字节业务 TLV），后来上游收紧即全线 4100404。
#: 现在本地 mint 只会产出 376，任何短包都是 bug，必须**在本地拦死**，
#: 别拿 403 去污染号池。要临时放行用 MARVIS_USKEY_MIN_BYTES 覆盖。
USKEY_MIN_BYTES = int(os.environ.get("MARVIS_USKEY_MIN_BYTES") or 376)


class SidecarError(RuntimeError):
    """侧车不可用或返回不合法（上层按 `network` / 平台不可用处理）。"""


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
    """校验侧车回包；不合法就抛 —— **绝不**兜底造一个 uskey。"""
    if not isinstance(payload, dict):
        raise SidecarError("侧车回包不是 JSON 对象")
    uskey = str(payload.get("uskey") or "")
    qimei36 = str(payload.get("qimei36") or "")
    if not uskey:
        raise SidecarError("侧车没有返回 uskey：%s" % payload.get("error") or payload)
    try:
        raw = base64.b64decode(uskey, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SidecarError("侧车返回的 uskey 不是合法 base64") from exc
    if len(raw) < USKEY_MIN_BYTES:
        raise SidecarError("侧车返回的 uskey 太短（%d 字节 < %d；uskeyLen=%d qimei36=%s）" % (len(raw), USKEY_MIN_BYTES, len(uskey), qimei36[:8]))
    if not uskey.startswith(USKEY_HEAD):
        # 不硬失败：magic 变了说明 SDK 换了版本，记一笔就够。
        logger.warning("uskey 头不是预期的 %s：前 8 字符 %s",
                       USKEY_HEAD, uskey[:8])
    if len(qimei36) != 36:
        logger.warning("qimei36 长度异常：%d", len(qimei36))
    return {"uskey": uskey, "qimei36": qimei36,
            "uskey_bytes": len(raw), "uskey_len": len(uskey),
            "bus_info": str(payload.get("busInfo") or ""),
            "ms": payload.get("ms")}


def _http_sign(**kwargs) -> dict:
    """POST 侧车 `/uskey`。"""
    body = json.dumps({
        "agentTag": kwargs["agent_tag"],
        "conversationId": kwargs["conversation_id"],
        "responseId": kwargs["response_id"],
        "scene": kwargs["scene"],
        "model": kwargs["model"],
        "nonce": kwargs["nonce"],
        "openId": kwargs["openid"],
    }, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SIDECAR_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=SIDECAR_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise SidecarError("uskey 侧车 HTTP %d：%s" % (exc.code, detail)) from exc
    except Exception as exc:  # noqa: BLE001
        raise SidecarError(
            "uskey 侧车不可达（%s）：%s" % (SIDECAR_URL, exc)) from exc
    try:
        payload = json.loads(text or "{}")
    except ValueError as exc:
        raise SidecarError("uskey 侧车回包不是 JSON：%r" % text[:200]) from exc
    return _validate(payload)


def qimei_for(account_key: str) -> str:
    """按账号派生**结构合法**的 qimei36：24hex 摘要 + '3000' + 8hex 尾段。

    同一账号恒定，不同账号互不相同。⚠ 这不是真机 qimei：真机值由 qimei.dll
    采集整台设备向量（SMBIOS UUID / 磁盘序列号 / MAC / BSSID / GPU / CPU /
    分辨率 / locale / 计算机名）后经 /ola/v2 注册得到；这里只是格式合法的
    占位值，只换摘要不换源数据本身仍是"自相矛盾"信号。
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
    """取一块 uskey：**本地 mint 优先**，失败才回落侧车。

    返回值：`{"uskey", "qimei36", "uskey_len", "uskey_bytes", "bus_info", "ms"}`。

    顺序：(1) 注入的 `_signer`（测试/离线）；(2) `MARVIS_USKEY_FORCE_SIDECAR=1`
    强制侧车；(3) `_local_sign()`；(4) 本地异常 → `_http_sign()`。
    两条路都不可用抛 :class:`SidecarError`，上层当「平台暂时不可用」处理，
    **不是**「账号坏了」。
    """
    kwargs = dict(agent_tag=agent_tag, conversation_id=conversation_id,
                  response_id=response_id, scene=scene, model=model,
                  nonce=nonce, openid=openid)
    if _signer is not None:
        return _signer(**kwargs)
    if os.environ.get("MARVIS_USKEY_FORCE_SIDECAR") == "1":
        return _http_sign(**kwargs)
    try:
        return _local_sign(device_key=account_key, **kwargs)
    except ValueError:
        # 配置类错误（没给账号标识之类）不是「本地 mint 挂了」，直接抛，别掩盖真因。
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("本地 mint uskey 失败，回落侧车（%s）：%s",
                       SIDECAR_URL, exc)
        return _http_sign(**kwargs)


def health(timeout: float = 2.0) -> dict:
    """侧车体检（控制台/排障用）：`GET /health`。"""
    url = SIDECAR_URL[:-len("/uskey")] if SIDECAR_URL.endswith("/uskey") else SIDECAR_URL
    url = url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "url": url}



# --- qimei_table override (appended 2026-11-18) ---
#: 真实 qimei 表：每台"虚拟设备"由真实 MAC 经 qimei.dll 算出（见
#: /opt/marvis-wine/logs/MAC-SPOOF-FINDINGS.md）。MAC 是 qimei 三项散列输入之一，
#: 换 MAC 即换 qimei；这些值全部格式合法且从未被上游看到过。
#: 有值就用真值，没值才退回派生（派生值会自报"假的"）。
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
