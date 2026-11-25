"""代理凭证的加密存储 + 日志脱敏。

## 为什么必须加密（2026-11-06 用户强调）

> 这个安全性一定要做好，这个很危险。

代理 URL 里通常带账密：`http://user:pass@host:port`。它落库就是**明文
凭据**，而这张表会被备份、会被导出、会被截图、会被 `/api/proxies` 读。
一条泄露就等于整批出口被白嫖，而且这些出口往往绑定着付费的住宅/隧道资源。

所以：

1. **落库加密**：`proxy_endpoints.url` 存 `enc:v1:<base64>`，
   key 由 `ADMIN_JWT_SECRET` 派生（换 secret = 旧密文失效，符合预期）。
2. **API 永远打码**：面板只看到 `http://user:***@host:port`，
   要看原文只能重新填一遍（不提供「查看密码」）。
3. **日志脱敏**：`last_error` / 异常文本里出现的 `user:pass@` 一律替换成
   `***`，避免排障时把凭据打进日志文件。
4. **环境变量引用**：URL 写成 `env:PROXY_MAIN` 时直接从环境变量读，
   **完全不落库** —— 最敏感的场景连密文都不要有。

## 迁移兼容

`decrypt()` 遇到不带 `enc:v1:` 前缀的值原样返回，所以老数据（明文）
不会因为升级而失效；写入时会自动升级成密文。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re

_logger = logging.getLogger("admin.secret_box")

#: 密文前缀。改这个等于让所有旧密文失效，不要动。
MAGIC = "enc:v1:"

#: 环境变量引用的前缀：`env:NAME`
ENV_PREFIX = "env:"

_AAD = b"proxy-endpoint-url-v1"

#: 用途区分（第 4 轮 R4-BE-03）：AAD 绑定密文用途，防止把一类密文挪到另一类字段里
#: 也能解开。代理 URL 继续用原 AAD（旧密文零迁移）；`api_keys.key_full` 用独立 AAD。
AAD_PROXY_URL = _AAD
AAD_KEY_FULL = b"api-key-full-v1"


def _aad_bytes(aad) -> bytes:
    if aad is None:
        return _AAD
    if isinstance(aad, str):
        return aad.encode()
    return bytes(aad)


def _legacy_fallback_enabled() -> bool:
    """解密失败时是否回落旧 AAD（`proxy-endpoint-url-v1`）。

    第 3 轮写入的 key_full 密文用的是代理 AAD，所以默认开；启动迁移
    （keys.migrate_key_full）把它们改写成新 AAD 后，可设 `SECRET_BOX_LEGACY_AAD=0` 关掉。
    """
    return (os.getenv("SECRET_BOX_LEGACY_AAD", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _key() -> bytes:
    """从服务密钥派生 32 字节 AES key。

    刻意不用随机 salt：这是**配置解密**不是用户密码，同一份部署必须能
    解同一份密文。换 `ADMIN_JWT_SECRET` 会让旧密文解不开 —— 这也是
    预期行为（轮换密钥 = 强制重填代理）。
    """
    from admin.config import settings

    secret = str(getattr(settings, "JWT_SECRET", "") or "dev-insecure")
    return hashlib.sha256(("proxy-box|" + secret).encode()).digest()


def encrypt(plaintext: str, aad=None) -> str:
    """明文 URL → `enc:v1:<b64(iv|tag|ciphertext)>`。

    空值、已加密的值原样返回（幂等）。`aad` 缺省 = 代理 URL 用途（与旧版一致）。
    """
    text = str(plaintext or "")
    if not text or text.startswith(MAGIC):
        return text
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = os.urandom(12)
        blob = AESGCM(_key()).encrypt(iv, text.encode(), _aad_bytes(aad))
        return MAGIC + base64.b64encode(iv + blob).decode()
    except Exception as exc:  # noqa: BLE001 - 加密不可用时**不能静默存明文**
        _logger.error("代理 URL 加密失败，拒绝以明文落库：%s", exc)
        raise


def decrypt_ex(value: str, aad=None) -> tuple[str, bytes | None]:
    """解密并返回 (明文, 实际匹配的 AAD)。明文输入 → (原值, None)；失败 → ("", None)。

    指定的 `aad` 解不开、且它不是旧 AAD 时，回落旧 AAD 再试一次（兼容第 3 轮
    用代理 AAD 写的 key_full），由 `SECRET_BOX_LEGACY_AAD` 控制。
    """
    text = str(value or "")
    if not text.startswith(MAGIC):
        return text, None
    want = _aad_bytes(aad)
    candidates = [want]
    if want != _AAD and _legacy_fallback_enabled():
        candidates.append(_AAD)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = base64.b64decode(text[len(MAGIC):])
        iv, body = blob[:12], blob[12:]
        box = AESGCM(_key())
    except Exception as exc:  # noqa: BLE001
        _logger.warning("密文解码失败（数据损坏）：%s", exc)
        return "", None
    last_exc = None
    for cand in candidates:
        try:
            return box.decrypt(iv, body, cand).decode(), cand
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    _logger.warning("解密失败（密钥轮换或数据损坏）：%s", last_exc)
    return "", None


def decrypt(value: str, aad=None) -> str:
    """`enc:v1:` 密文 → 明文；明文原样返回（迁移兼容）。`aad` 缺省 = 代理 URL 用途。"""
    return decrypt_ex(value, aad)[0]


def needs_rewrap(value: str, aad) -> bool:
    """密文能解开、但用的是旧 AAD（不是 `aad`）→ 需要按新用途重新加密。"""
    plain, used = decrypt_ex(value, aad)
    return bool(plain) and used is not None and used != _aad_bytes(aad)


def resolve(value: str) -> str:
    """取**可直接使用**的 URL：`env:NAME` → 环境变量，密文 → 解密。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(ENV_PREFIX):
        name = text[len(ENV_PREFIX):].strip()
        return str(os.getenv(name, "") or "").strip()
    return decrypt(text)


#: `scheme://user:pass@host` 里的 `user:pass` 段
_CRED_PATTERN = re.compile(r"(://[^:/@\s]+:)[^@\s]*(@)")
#: 裸的 `user:pass@host:port`（没有 scheme 时也会出现）
_BARE_CRED_PATTERN = re.compile(r"\b([\w.\-]+:)[^\s@/]+(@[\w.\-]+)")


def scrub(text: str) -> str:
    """把文本里的代理账密替换成 `***`。

    用途：写 `last_error`、打日志、拼异常信息。**任何可能被落库或写文件的
    字符串都要过一遍这里** —— 排障时最容易顺手把整条 URL 塞进错误消息。
    """
    value = str(text or "")
    if not value:
        return value
    value = _CRED_PATTERN.sub(r"\1***\2", value)
    value = _BARE_CRED_PATTERN.sub(r"\1***\2", value)
    return value


def mask(url: str) -> str:
    """展示用打码：`http://user:***@host:port`。"""
    return scrub(url)