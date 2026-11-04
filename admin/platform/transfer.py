"""跨平台账号池「批量导入 / 导出」通用实现。

## 为什么有这个模块

WorkBuddy 账号池一直有完整的「备份迁移」能力（`/api/accounts/batch`
+ `/api/accounts/export`），其它平台（Trae / Qoder / Loomy）此前只有
单条 `import` —— 用户没法批量备份、也没法把导出文件再导回来。

各平台凭证的落库形态完全一致：`Account.auth_json` 存**凭证 JSON 原文**，
导出即原文、导入即 `import_record`，因此批量逻辑可以收拢到这里，
各平台 router 只需各加两个薄端点（并传入自己的 `import_record`）。

导出格式与 WorkBuddy 的 `format="backup"` 对齐（JSONL：每行一个凭证对象），
导入端同时接受 WorkBuddy 备份形态（`items[].auth_json` 字符串）与
通用形态（`items[].credential` 对象），两种文件可以互相通用。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from admin.models import Account


def export_accounts(db: Session, platform: str, realm: str = "",
                    ids: list[int] | None = None,
                    include_disabled: bool = False) -> dict:
    """导出某平台（可按区域收窄）的账号凭证，JSONL 就绪。

    返回结构与 WorkBuddy `/api/accounts/export` 一致：`items` 是
    凭证原文列表（每条都是合法 JSON），可直接 `"\n".join()` 落盘，
    也能原样传回 batch 导入 —— 导出文件就是导入文件。
    """
    query = db.query(Account).filter(Account.platform == platform)
    if realm:
        query = query.filter(Account.domain == realm)
    if ids:
        query = query.filter(Account.id.in_(ids))
    elif not include_disabled:
        query = query.filter(Account.status == "active")
    rows = query.order_by(Account.id).all()

    items: list[str] = []
    for account in rows:
        raw = (account.auth_json or "").strip()
        if not raw:
            continue
        try:
            json.loads(raw)
        except Exception:  # noqa: BLE001 - 跳过损坏记录，不让整个导出失败
            continue
        items.append(raw)

    return {
        "format": "backup",
        "platform_scope": f"{platform}:{realm}" if realm else platform,
        "total": len(rows),
        "count": len(items),
        "skipped": len(rows) - len(items),
        "items": items,
        # 附带摘要供前端展示（不含凭据）
        "summary": [
            {"id": a.id, "platform": a.platform or "", "name": a.name,
             "uid": a.uid, "status": a.status,
             "balance_total": a.balance_total, "balance_remain": a.balance_remain}
            for a in rows
        ],
    }


def batch_import(db: Session, items: list, import_one) -> dict:
    """逐条导入并归并成 `{added, updated, errors}`。

    `import_one(record) -> bool created`（抛异常视为该条失败）。
    每条独立 try：坏一条不能拖垮整批 —— 与 WorkBuddy 批量导入一致。

    条目形态二选一：
      * `{"credential": {...}}` / `{"credential": "<json 字符串>"}`（通用）
      * 凭证对象直接平铺（ WorkBuddy 备份里常见 `auth_json` 字符串形态）
    """
    added = updated = 0
    errors: list[dict] = []
    for index, item in enumerate(items or []):
        try:
            record = _credential_of(item)
            if import_one(record):
                added += 1
            else:
                updated += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            errors.append({"index": index, "error": str(exc)[:200]})
    return {"added": added, "updated": updated, "errors": errors}


def _credential_of(item) -> dict:
    """从批量条目里取凭证对象；字符串形态自动 `json.loads`。"""
    if isinstance(item, dict):
        raw = item.get("credential", item.get("auth_json", item))
    else:
        raw = item
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError("凭证 JSON 解析失败") from exc
    if not isinstance(raw, dict):
        raise ValueError("凭证必须是对象")
    return raw


def existed_uid(db: Session, platform: str, record: dict) -> bool:
    """按 uid 预判「这条凭证库里有没有」——用于 added/updated 归并。

    Qoder / Loomy 的 `import_record` 不区分新建与更新，这里在导入前
    查一次 (platform, uid)；uid 取不到时按「新建」计。
    """
    uid = str(record.get("uid") or record.get("user_id")
              or record.get("userid") or "")
    if not uid:
        return False
    return (db.query(Account.id)
            .filter(Account.platform == platform, Account.uid == uid)
            .first()) is not None
