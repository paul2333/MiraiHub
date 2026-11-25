"""导出完整凭证的统一闸门（2026-11-24 第 3 轮，安全审查 S-4）。

原来 `/api/platforms/export-all` 和各平台 `GET /export` 大多**默认**就回完整凭证
（token / refresh_token / cookie），一次误点、一个被 XSS 拿到的会话就能整池外带。

现在的口径：
  * 默认只给脱敏清单；
  * 要完整凭证必须**同时**：显式参数（`mode=backup` / `mode=full` / `reveal=1` /
    `include_secrets=1`）+ 二次确认（请求头 `X-Export-Confirm: include-secrets`，
    或查询参数 `confirm=include-secrets`）。只有参数没有确认 → 428，
    前端据此弹「将导出完整凭证」确认框后重发；
  * 每次完整导出写一条审计（security.audit_secret_access）。

紧急回退：EXPORT_SECRETS_REQUIRE_CONFIRM=0 —— 显式参数即可拿完整凭证（不再要确认），
默认值仍是脱敏。
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException, Request

CONFIRM_HEADER = "X-Export-Confirm"
CONFIRM_VALUE = "include-secrets"
_MASK = "***"

#: 凭证类键名（小写、去掉 `_` / `-` 后比较）。
SENSITIVE_KEYS = frozenset({
    "token", "accesstoken", "refreshtoken", "idtoken", "sessiontoken", "authtoken",
    "session", "sessionkey", "sessionid", "sid", "apikey", "secret", "clientsecret",
    "password", "passwd", "pwd", "cookie", "cookies", "authorization", "auth",
    "bearer", "jwt", "credential", "credentials", "privatekey", "signature",
    "ticket", "otp", "code", "verifier", "codeverifier", "machinetoken",
})
#: 含这些片段的键也视为凭证（如 `x_csrf_token`、`wps_sid`）。
_SENSITIVE_PARTS = ("token", "secret", "password", "cookie", "session", "apikey")


def _require_confirm() -> bool:
    return (os.getenv("EXPORT_SECRETS_REQUIRE_CONFIRM", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def is_sensitive_key(key: Any) -> bool:
    norm = str(key or "").lower().replace("_", "").replace("-", "")
    if not norm:
        return False
    if norm in SENSITIVE_KEYS:
        return True
    return any(part in norm for part in _SENSITIVE_PARTS)


def _mask_value(value: Any) -> Any:
    if value is None or value == "" or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _MASK
    return _MASK


def sanitize(value: Any, _depth: int = 0) -> Any:
    """递归脱敏：dict / list 逐层下钻，凭证键的值一律换成 `***`。

    字符串里如果是 JSON 对象（各平台 `auth_json` 常以字符串存），会解析后再脱敏、
    以对象形式返回（脱敏清单不需要能回灌）。深度上限 12 防环。
    """
    if _depth > 12:
        return _MASK
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if is_sensitive_key(key) and not isinstance(item, (dict, list)):
                out[key] = _mask_value(item)
            elif is_sensitive_key(key):
                out[key] = sanitize(item, _depth + 1) if _depth < 12 else _MASK
                # 凭证容器（如 cookies: {...}）里的叶子全部打码
                out[key] = _mask_leaves(out[key])
            else:
                out[key] = sanitize(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(item, _depth + 1) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if len(text) >= 2 and text[0] in "{[" and text[-1] in "}]":
            import json
            try:
                parsed = json.loads(text)
            except ValueError:
                return value
            return sanitize(parsed, _depth + 1)
    return value


def _mask_leaves(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _mask_leaves(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_leaves(v) for v in value]
    return _mask_value(value)


def wants_secrets(*flags: Any) -> bool:
    """任一显式参数为真 → 调用方想要完整凭证。"""
    for flag in flags:
        if isinstance(flag, str):
            if flag.strip().lower() in ("1", "true", "yes", "on", "backup", "full"):
                return True
        elif flag:
            return True
    return False


def confirmed(request: Request | None) -> bool:
    if request is None:
        return False
    header = (request.headers.get(CONFIRM_HEADER) or "").strip().lower()
    query = str((request.query_params or {}).get("confirm") or "").strip().lower()
    return CONFIRM_VALUE in (header, query)


def guard_secret_export(request: Request | None, scope: str, want: bool) -> bool:
    """返回本次是否导出完整凭证；要了但没确认 → 428。放行时写审计。"""
    if not want:
        return False
    if _require_confirm() and not confirmed(request):
        raise HTTPException(status_code=428, detail={
            "code": "export_confirm_required",
            "message": "导出完整凭证需要二次确认：确认后请带上请求头 "
                       "X-Export-Confirm: include-secrets 重新请求",
            "confirm_header": CONFIRM_HEADER, "confirm_value": CONFIRM_VALUE,
        })
    try:
        from admin.security import audit_secret_access
        audit_secret_access(request, "credential_export", scope)
    except Exception:  # noqa: BLE001 - 审计失败不挡导出（已有确认）
        pass
    return True
