"""Persisted manual order is shared by the dashboard and the real selector."""
from admin.pool import expiry_key


def manual_priority(account):
    value = getattr(account, 'selection_priority', None)
    return value if value is not None else float('inf')


def ordered_accounts(accounts):
    return sorted(accounts, key=lambda a: (manual_priority(a), expiry_key(a),
                                          -int(a.balance_remain or 0), a.id))


# ---------------------------------------------------------------------------
# 按平台作用域保存选号顺序（修复批次 3 · D5，平台隔离审查 P0-5）
#
# 原来 `POST /api/credits/selection-order` 要求 `expected_ids` 等于**全库所有账号**
# （含其它平台、含停用）的排序，而前端传的是 `/api/credits/expiry` 返回的
# **当前平台作用域内、默认只含 active** 的 `selection_order` —— 只要有第二个平台
# 或有停用账号，「恢复自动选号」就**每次 409**。
# `PUT /selection-policy` 则把**所有平台**账号的 `selection_priority` 清空，
# 在 Trae 页面改策略会顺手抹掉 WorkBuddy 的手动顺序。
#
# 下面两个函数把这两件事都收到「当前平台作用域」里做：
#   * `expected_ids` 与作用域内「全部账号」或「仅 active」任一口径一致即可
#     （`/expiry` 的 include_disabled 两种取值都能对上）；
#   * manual 模式只要求覆盖那个口径内的账号；作用域内其余账号（如停用的）
#     依原有相对顺序排在后面，不被丢掉；
#   * automatic 模式只清作用域内账号的优先级。
# 不带平台作用域（老调用方 / 脚本）时与原行为一致：作用域 = 全库。
# ---------------------------------------------------------------------------
class SelectionOrderError(Exception):
    """保存选号顺序失败；`status` 为建议的 HTTP 状态码（409 / 400）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def scoped_accounts(db, platform: str = "", realm: str = "", *, lock: bool = False):
    """作用域内的账号（按 id 排序）；`platform` 为空 = 全库。"""
    from admin.models import Account
    from admin.platform import scope as platform_scope

    query = db.query(Account)
    if platform:
        query = platform_scope.filter_accounts(query, db, platform, realm)
    query = query.order_by(Account.id)
    if lock:
        query = query.with_for_update()
    return query.all()


def plan_order(accounts, mode: str, ids, expected_ids) -> dict:
    """纯函数：校验并算出每个账号的新优先级 `{account_id: priority | None}`。

    不碰数据库，便于单测。校验失败抛 `SelectionOrderError`。
    """
    ordered = ordered_accounts(accounts)
    all_ids = [a.id for a in ordered]
    active_ids = [a.id for a in ordered if (getattr(a, 'status', 'active') or 'active') == 'active']
    expected = [int(i) for i in (expected_ids or [])]
    if expected == all_ids:
        covered = all_ids
    elif expected == active_ids:
        covered = active_ids
    else:
        raise SelectionOrderError(409, '账号或排序已变化，请刷新后重试')
    if mode == 'manual':
        wanted = [int(i) for i in (ids or [])]
        if len(wanted) != len(covered) or set(wanted) != set(covered):
            raise SelectionOrderError(400, '排序必须包含全部账号且不能重复')
        positions = {account_id: index for index, account_id in enumerate(wanted)}
        # 作用域内没被覆盖到的账号（停用的）接在后面，保持原有相对顺序
        tail = [i for i in all_ids if i not in positions]
        for offset, account_id in enumerate(tail):
            positions[account_id] = len(wanted) + offset
        return positions
    return {account_id: None for account_id in all_ids}


def save_order(db, *, mode: str, ids, expected_ids, platform: str = "", realm: str = "") -> dict:
    """在作用域内保存手动顺序 / 恢复自动选号，并提交。

    返回 `{'ok', 'mode', 'ids'}`（`ids` 为作用域内保存后的调度顺序）。
    """
    accounts = scoped_accounts(db, platform, realm, lock=True)
    positions = plan_order(accounts, mode, ids, expected_ids)
    for account in accounts:
        account.selection_priority = positions.get(account.id)
    db.commit()
    return {'ok': True, 'mode': mode, 'ids': [a.id for a in ordered_accounts(accounts)]}


def clear_priorities(db, platform: str = "", realm: str = "") -> int:
    """只清作用域内账号的手动优先级（保存选号策略时用）。返回清掉的个数。不提交。"""
    accounts = scoped_accounts(db, platform, realm, lock=True)
    changed = 0
    for account in accounts:
        if account.selection_priority is not None:
            account.selection_priority = None
            changed += 1
    return changed
