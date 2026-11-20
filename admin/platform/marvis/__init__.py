"""腾讯 Marvis 平台网关。

## 目录职责

    admin/platform/marvis/
      __init__.py            平台声明 + install()
      gateway.py             /v1/* 转发实现
      platform_gateway.py    Gateway 契约（账号池 / 任务 / 探测 / 卡片）
      core/endpoints.py      端点表
      core/client.py         HTTP 客户端
      services/accounts.py   凭证与账号池
      services/scan.py       本机扫描
      routers/accounts.py    /api/marvis/*

## 接入来源

拆包侦察结论见 `docs/marvis-recon.md`（如有）。
**实现前先跑 skill 里的脚本**：

    python admin/platform/skill/scripts/recon.py "<安装目录>"
    python admin/platform/skill/scripts/probe.py --recon recon-report.json
    python admin/platform/skill/scripts/creds.py --source "<unpacked 目录>"
"""
from __future__ import annotations

import logging

from admin.platform.gateway import AccountOps, ClientProbe, Gateway
from admin.platform.registry import PlatformSpec

logger = logging.getLogger("admin.platform.marvis")

PLATFORM = "marvis"
#: 落库区域（没有多区就留一个空串）
REALMS = ("cn", )
#: 区域 -> 对外平台 id
REALM_IDS = {"cn": "marvis"}


def _task_runners() -> dict:
    """本平台支持的定时任务：额度同步 + 续签保活。

    `marvis_keepalive` 是**必需**的：微信登录的 access_token 只活约 2 小时，
    没有这个任务，扫码授权完过两小时账号必然掉线（见 issue #41）。
    """
    from admin.platform.marvis import platform_gateway as _pg

    return {"marvis_refresh_quota": _pg._run_refresh_quota,
            "marvis_keepalive": _pg._run_keepalive}


def _validate_qimei_pool() -> None:
    """启动即校验 qimei 池硬约束：`owners > 2` 直接拒绝启动。

    为什么放在 install() 里而不是请求路径上：这套池台账的坏状态（一台"设备"
    挂 3 个号）**修不回来也不能带着跑** —— 它会一路把同一个设备串用到被上游
    整体标记。宁可容器起不来，让人先去修 JSON。

    排障时可设 `QIMEI_VALIDATE_ON_START=0` 跳过（只跳过校验，不改任何数据）。
    池文件不存在时等价于空池，校验直接通过。
    """
    import os as _os

    if (_os.environ.get("QIMEI_VALIDATE_ON_START") or "1").strip() == "0":
        logger.warning("QIMEI_VALIDATE_ON_START=0：跳过 qimei 池启动校验")
        return
    from admin.platform.marvis.core import qimei_assign

    qimei_assign.validate_or_raise()
    qimei_assign.install_orm_hooks()


def check_egress(strict: bool = False) -> bool:
    """启动期出口池自检：空池**不许静默**（静默退回直连比直接报错更难查）。

    为什么非要有这一条：`core/egress_pool.json` 不进 git（数据一律不进仓库，
    见 .gitignore 与 `core/egress.py` 的 docstring），于是**从全新 clone 构建的
    镜像天然没有这个文件** —— 池一空 `egress.pick()` 就返回 ""，请求退回平台
    出口 / 直连；出口 IP 一变上游就回 4100404，而错误面**完全看不出是「池丢了」**。
    5e4fb04 之后线上之所以还能跑，只是因为构建源 `rel2` 的工作副本里恰好还留着
    这个未跟踪文件 —— 那个隐式依赖必须被打断（`MARVIS_EGRESS_POOL` 内联注入）。

    三级判据：
      · 池非空 → INFO（日志里看得见条数，排障时一眼确认池真接上了）；
      · 池为空 → WARNING，**不 crash**：egress.py 的契约是「没有池照样跑」，
        其它平台、本地开发、纯前端调试都不该被一个可选加速项拖死；
      · 池为空且 `MARVIS_EGRESS_REQUIRED=1` 且 `strict=True`
        → 抛 RuntimeError：部署侧明确要求「没池就别起」，这时静默降级才是事故。

    `strict=True` 只由 FastAPI startup 用（`admin/server.py::_startup`）：
    `install()` 里的异常会被 `admin.platform.loader` 逐项容错吞掉，装在
    install() 里起不到「拒绝启动」的作用。

    返回池是否非空（便于测试与调用方决定后续行为）。
    """
    import os as _os

    from admin.platform.marvis.core import egress as _egress

    pool = _egress.exits()
    if pool:
        logger.info("出口池已就绪：%d 个出口（按账号 sha256 固定映射）", len(pool))
        return True

    message = (
        "出口池为空：egress.pick() 返回 \"\"，请求退回平台出口 / 直连。"
        "core/egress_pool.json 不进 git（全新 clone 默认没有这个文件），"
        "请用 MARVIS_EGRESS_POOL（内联 JSON 数组或逗号串）或 "
        "MARVIS_EGRESS_POOL_FILE（文件路径）注入；"
        "否则出口 IP 一变上游就回 4100404，且错误面看不出是池丢了。"
    )
    if strict and (_os.environ.get("MARVIS_EGRESS_REQUIRED") or "").strip() == "1":
        raise RuntimeError(
            message + "（MARVIS_EGRESS_REQUIRED=1：拒绝以降级形态启动）")
    logger.warning(message)
    return False


def install() -> None:
    """登记 PlatformSpec（幂等）。"""
    # 出口池自检排第一：它决定了所有出口请求是走池还是裸奔直连，
    # 池空必须在上线后第一屏日志里就能看见（见 check_egress 的 docstring）。
    check_egress()
    _validate_qimei_pool()
    from admin.platform import registry as _registry

    for realm in REALMS:
        platform_id = REALM_IDS[realm]
        if _registry.get(platform_id) is not None:
            continue
        _registry.register(PlatformSpec(
            id=platform_id,
            label="腾讯 Marvis",
            realms=(),
            enabled=True,
            # Marvis 没有 PC 客户端可供扫描（它是应用宝里的 AI 助手），
            # 凭证只能靠「扫码登录」或「粘贴导入」。
            supports_local_scan=False,
            # 授权登录走 QQ / 微信扫码，不是设备码也不是密码。
            auth_mode="qr-auth",
            # 转发入口（dispatch 调用）
            gateway="admin.platform.marvis.gateway",
            # 管理契约（账号池 / 任务 / 探测 / 卡片）
            platform_gateway="admin.platform.marvis.platform_gateway:MarvisGateway",
            storage_platform=PLATFORM,
            realm=realm,
            icon="marvis",
            # 平台自评（控制台「平均级」徽标）。契约要求每个内建平台都声明，
            # 否则 `/api/platforms` 只回空 dict、卡片上缺徽标（测试会挡）。
            rating={
                "tier": "npc",
                "reason": "令牌钱包制、量给得很足（每号每日 1000 万计费 token），"
                          "微信登录态只有约 2 小时（QQ 30 天量级），"
                          "已由 marvis_keepalive 定时用 refresh_token 自动续签；不扫本机。",
            },
            notes="腾讯 Marvis（应用宝 AI 助手）：OpenAI 协议上游，"
                  "Ual-Access 头鉴权；令牌钱包制（每号每日 1000 万计费 token，"
                  "缓存免费、输入输出五折）；支持扫码登录 + 凭证导入，"
                  "登录态由 marvis_keepalive 定时续签",
            extra={
                "task_runners": list(_task_runners().keys()),
                "actions": ["import", "export", "begin-auth", "refresh-quota"],
                # 额度口径：**token 而不是积分**。控制台据此把「积分」列改成
                # 「Token」并启用 K/M 缩写（每号每日 1000 万，数字动辄七八位，
                # 千分位铺开会把等宽列撑爆）。
                "quota_unit": "token",
                "quota_unit_label": "Token",
            },
        ))
        logger.debug("腾讯 Marvis 平台已登记：%s", platform_id)


install()
