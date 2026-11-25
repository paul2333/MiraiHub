"""WorkBuddy 专属能力的平台闸门 + 按平台收窄的模型白名单。

## 为什么需要它（平台隔离审查 P0-1 / P0-2，修复批次 3 的 D2 / D3）

1. **WorkBuddy 专属能力没有后端平台闸门**：成长计划、猫猫旅行、积分到期快照
   同步、封号复核都直接用 `backend.AccountSession`（WorkBuddy 协议）去处理
   「全部 active 账号」—— 于是 Trae / Qoder / Loomy 的凭证被拿去打腾讯的
   接口，失败后还会回写 `auth_json`、记失败计数，可能触发冷却。
   这里给出统一判定 `is_workbuddy()` / `workbuddy_cond()` / `require_workbuddy()`。

2. **WorkBuddy 主链路的模型白名单 / auto / 降级候选是跨平台的**：
   `routers/models._get_enabled_models / _get_free_models / _is_model_allowed`
   只按 `model_id` 看 `model_configs`，不看 `platform` —— 在 WorkBuddy 上
   禁用了 `glm-5.2`，只要 Trae 还启用着同名模型，WorkBuddy 请求照样放行；
   `auto` 还可能挑中 Qoder 的 `dfmodel` 发给腾讯上游。
   这里给出按平台收窄的三个等价函数（`platform` 默认 `workbuddy`），
   兼容历史上 `platform=''/NULL` 的行（老库回填前的 WorkBuddy 数据）。

落库平台的历史写法：`workbuddy`（规范）、`codebuddy`（导入入口接受的旧名）、
空串 / NULL（回填前的老数据）。三者都按 WorkBuddy 处理；比较一律小写。
"""
from __future__ import annotations

#: 视为 WorkBuddy 的落库平台写法（小写比较）。空串 = 历史未回填的老数据。
WORKBUDDY_STORAGES = ("workbuddy", "codebuddy", "")


def normalize_storage(platform) -> str:
    """落库平台名归一：去空白、小写；None -> ''。"""
    return str(platform or "").strip().lower()


def is_workbuddy(account_or_platform) -> bool:
    """账号（或落库平台字符串）是否属于 WorkBuddy。"""
    value = account_or_platform
    if not isinstance(value, str) and value is not None:
        value = getattr(value, "platform", "")
    return normalize_storage(value) in WORKBUDDY_STORAGES


def workbuddy_cond(column=None):
    """SQL 条件：该列属于 WorkBuddy（含历史空值 / codebuddy）。

    `column` 缺省为 `Account.platform`；`ModelConfig.platform` 等同样适用。
    """
    from sqlalchemy import func, or_

    if column is None:
        from admin.models import Account

        column = Account.platform
    return or_(column.is_(None),
               func.lower(func.trim(column)).in_(WORKBUDDY_STORAGES))


def filter_workbuddy(query, column=None):
    """给查询加 WorkBuddy 平台条件。"""
    return query.filter(workbuddy_cond(column))


def platform_cond(column, platform: str):
    """SQL 条件：该列等于指定落库平台。WorkBuddy 兼容历史写法。"""
    from sqlalchemy import func

    target = normalize_storage(platform)
    if target in WORKBUDDY_STORAGES:
        return workbuddy_cond(column)
    return func.lower(func.trim(column)) == target


def non_workbuddy_ids(db, ids) -> list[int]:
    """`ids` 里**不属于** WorkBuddy 的账号 id（不存在的 id 不算）。"""
    from admin.models import Account

    wanted = [int(i) for i in (ids or [])]
    if not wanted:
        return []
    rows = db.query(Account.id, Account.platform).filter(Account.id.in_(wanted)).all()
    return sorted(int(r[0]) for r in rows if not is_workbuddy(r[1]))


def reject_message(account_or_ids, feature: str = "该功能") -> str:
    """统一的 400 文案（前端直接展示）。"""
    if isinstance(account_or_ids, (list, tuple, set)):
        ids = list(account_or_ids)
        head = "、".join("#%s" % i for i in ids[:10])
        more = " 等 %d 个" % len(ids) if len(ids) > 10 else ""
        return "%s仅支持 WorkBuddy 账号：%s%s 不是 WorkBuddy 账号" % (feature, head, more)
    acc = account_or_ids
    plat = normalize_storage(getattr(acc, "platform", "")) or "未知"
    return "%s仅支持 WorkBuddy 账号（账号 #%s 属于 %s 平台）" % (
        feature, getattr(acc, "id", "?"), plat)


def require_workbuddy(account, feature: str = "该功能") -> None:
    """非 WorkBuddy 账号 -> HTTP 400（WorkBuddy 专属接口的单账号入口用）。"""
    if not is_workbuddy(account):
        from fastapi import HTTPException

        raise HTTPException(400, reject_message(account, feature))


def require_workbuddy_ids(db, ids, feature: str = "该功能") -> None:
    """显式传入的账号 id 里有非 WorkBuddy 的 -> HTTP 400（批量入口用）。"""
    bad = non_workbuddy_ids(db, ids)
    if bad:
        from fastapi import HTTPException

        raise HTTPException(400, reject_message(bad, feature))


# ---------------------------------------------------------------------------
# 按平台收窄的模型白名单（D3）
# ---------------------------------------------------------------------------
def _rows_for(db, platform: str):
    from admin.models import ModelConfig

    return (db.query(ModelConfig)
            .filter(platform_cond(ModelConfig.platform, platform)).all())


def _is_free_row(row) -> bool:
    """与 `routers/models._get_free_models` 同一判定：0 倍率或 NULL（未同步）才算免费。"""
    mult = getattr(row, "credit_multiplier", None)
    return mult is None or float(mult) == 0.0


def enabled_models(db, platform: str = "workbuddy") -> set[str]:
    """该平台**已启用**的 model_id 集合（WorkBuddy 兼容 platform 为空的历史行）。"""
    return {r.model_id for r in _rows_for(db, platform) if r.enabled == 1}


def free_models(db, platform: str = "workbuddy") -> set[str]:
    """该平台已启用且免费（0 倍率 / 未同步）的 model_id 集合（排除图像/视频模型）。"""
    try:
        from model_metadata import is_media_model
    except Exception:  # noqa: BLE001 - 元数据模块缺失时不过滤媒体模型
        def is_media_model(_mid):
            return False
    return {r.model_id for r in _rows_for(db, platform)
            if r.enabled == 1 and _is_free_row(r) and not is_media_model(r.model_id)}


def is_model_allowed(db, model_id: str, platform: str = "workbuddy") -> bool:
    """模型是否在**该平台**白名单内。

    该平台一条模型配置都没有时放行（与旧 `_is_model_allowed`「未配置任何规则
    即放行」同语义，只是作用域从全库收窄到本平台）。
    """
    rows = _rows_for(db, platform)
    if not rows:
        return True
    return model_id in {r.model_id for r in rows if r.enabled == 1}


#: 上线前的审计 SQL（见 fixlog_data_scoping.md）：找出「WorkBuddy 没启用、
#: 却因别的平台启用了同名模型而被放行」的模型，以及近 7 天实际走过的请求。
AUDIT_SQL = (
    "SELECT m.model_id, GROUP_CONCAT(DISTINCT m.platform) AS enabled_on "
    "FROM model_configs m WHERE m.enabled = 1 "
    "AND LOWER(COALESCE(m.platform, '')) NOT IN ('workbuddy', 'codebuddy', '') "
    "AND m.model_id NOT IN (SELECT w.model_id FROM model_configs w WHERE w.enabled = 1 "
    "AND LOWER(COALESCE(w.platform, '')) IN ('workbuddy', 'codebuddy', '')) "
    "GROUP BY m.model_id"
)
