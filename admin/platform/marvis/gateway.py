"""腾讯 Marvis 的 `/v1/*` 转发实现。

## 上游就是 OpenAI 协议

Marvis 的模型网关 `POST /v1/chat/completions` 本身就是 OpenAI 兼容的（SSE 流式、
`finish_reason`、`tool_calls`、`reasoning_content` 都在原位），所以这里的活主要是：

  1. **模型 id 归一** —— 用户传的是显示名（`deepseek-v4-1-flash-261017`），
     上游只认槽位名（`main-auto`）。直接透传真实模型名会 400。
  2. **鉴权头** —— 不是 Bearer token，而是 `Ual-Access-*` 那一组（见 core/endpoints）。
  3. **流式原样转发** —— 不在这里读干流。
  4. **记账** —— 落 usage_logs，控制台的用量与积分统计靠它。

## 为什么**每一个**上游调用都要在工作线程里

`core/client.py` 走的是 `urllib`（同步）。在 async handler 里直接
`urlopen` / `stream.read()` 会把整个事件循环按秒级卡住 ——
`admin.server` 是单进程单 worker 的 uvicorn，事件循环一停，全站（控制台、
账号同步、健康检查、所有平台的转发）一起停摆。

2026-11-15 线上事故就是这一条：`open_chat_stream` 里的 `urlopen` 在事件
循环线程上等上游状态行，`admin.loop_watch` 实测卡死
3.3s / 7s / 17s / 27s / **63.5s**，43 个在飞请求被串行化后超期，上游回
`4100406 非法请求`（再被旧版 `_classify` 误判成 `signature_invalid`，
DSH 侧就显示成「API key is invalid」）。

所以现在：**建连**（`open_chat_stream` / `chat_completions`）、**逐块读**
（`resp.read`）、以及**同步 DB**（选号 / 租约 / 记账）全部走
`asyncio.to_thread`；事件循环线程上只剩 await。
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import random
import re
import time

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from admin.platform import shared
from admin.platform.marvis.core import client, endpoints
from admin.platform.marvis.services import accounts as marvis_accounts

logger = logging.getLogger("admin.platform.marvis.gateway")

PUBLIC = endpoints.PLATFORM
STORAGE = marvis_accounts.PLATFORM
#: 一次请求最多尝试**几个账号**（不是上游调用次数）。
#:
#: 与「同号重试」是两层独立的上限（2026-11-16 解耦）：
#:   · 同一个号内部：`_call_upstream` 最多 1 + SAME_ACCOUNT_RETRIES = 3 次；
#:   · 账号之间：本常量 = 3 个号，每号最多 3 次 ⇒ 最坏 9 次上游调用。
#: 以前这两层没有任何日志能区分：一个号把 3 次尝试全吃掉后请求就回 403，
#: 看不出是「池里没别的号」还是「换号没发生」。现在换号显式化并留日志
#: （`Marvis 换号重试` / `Marvis 换号终止`）。
MAX_ATTEMPTS = 3

#: 403 请求体转储 —— **临时排障开关，默认关闭**（2026-11-16 加）。
#:
#: 线上 4100406 只在大请求上出现（bytes=3590147 / msgs=270 / sys_chars=142860，
#: 3 次尝试全 403），同一时刻同一个号的小请求 200；而**同形状的合成请求**
#: （270 条 / 3.65MB / sys 143K）全部 200 ⇒ 触发条件是**具体内容**，不是体积、
#: 不是消息数、不是 tools 数。要二分内容，先得拿到真实失败的那一份 body。
#:
#: 开关（每次调用都读环境变量，改 env 后无需改代码）：
#:   · `MIRAIHUB_MARVIS_DUMP_403=1`      打开转储；默认 0 / 未设置 = 完全不碰磁盘；
#:   · `MIRAIHUB_MARVIS_DUMP_403_DIR`   目录，默认 `/tmp`（容器内）；
#:   · `MIRAIHUB_MARVIS_DUMP_403_KEEP`  最多保留几份，默认 3。
#:
#: ⚠ **内容绝不进日志**：日志里只有路径与字节数。dump 文件里是用户对话原文，
#: 排障结束必须删除（`_prune_dump_403` 只管份数，不管生命周期）。
DUMP_403_ENV = "MIRAIHUB_MARVIS_DUMP_403"
DUMP_403_DIR_ENV = "MIRAIHUB_MARVIS_DUMP_403_DIR"
DUMP_403_KEEP_ENV = "MIRAIHUB_MARVIS_DUMP_403_KEEP"
DUMP_403_DEFAULT_DIR = "/tmp"
DUMP_403_DEFAULT_KEEP = 3
DUMP_403_PREFIX = "mh403-"
#: 已落盘内容的指纹 → 路径。
#:
#: 一次请求会打多次上游（同号最多 3 次 + 换号最多 3 个号），**body 完全相同**；
#: 不去重的话一次请求就能把 3 个名额占满，而且三份内容一模一样、毫无增量。
#: 按内容 sha256 去重 ⇒ 一次请求最多落一份，名额留给**不同**的失败请求。
_DUMP_403_SEEN: dict[str, str] = {}
_DUMP_403_SEEN_MAX = 16

PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/models",
    "/v1/balance",
})


class _BadRequest(Exception):
    """本地就能判定的错请求（模型不可用 / 空 messages）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _realm(request) -> str:
    """Marvis 只有国内区。保留这个函数是为了与其它平台同形。"""
    return "cn"


def _client_ip(request) -> str:
    try:
        forwarded = request.headers.get("x-forwarded-for") or ""
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else ""
    except Exception:  # noqa: BLE001
        return ""


def _fail(exc: client.UpstreamError) -> JSONResponse:
    status = exc.status or 502
    if status < 400 or status > 599:
        status = 502
    return JSONResponse(
        {"error": {"type": exc.kind, "code": exc.code or "upstream_error",
                   "message": exc.message}},
        status_code=status)


async def _body_of(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _validate_model(body: dict) -> str:
    """归一模型 id，并在本地拦掉已知持续失败的模型。

    ⚠ 不可用模型返回 **404**（模型级错误），不是 401。
    401 会被上层当成「账号登录失效」去 failover 到每一个账号，
    一个坏模型就能把整池好号全标成失效。
    """
    raw = str(body.get("model") or endpoints.DEFAULT_MODEL)
    slot = endpoints.canonical_model_id(raw)
    if endpoints.is_unavailable_model(slot):
        raise _BadRequest("model_not_found",
                          "模型 %s 在 Marvis 上游不可用（已确认持续失败）" % raw)
    if slot not in endpoints.DISPLAY_BY_SLOT:
        raise _BadRequest("model_not_found",
                          "未知模型 %s；可用：%s" % (
                              raw, "、".join(endpoints.DISPLAY_BY_SLOT)))
    if not (body.get("messages") or body.get("input")):
        raise _BadRequest("invalid_request", "请求缺少 messages")
    return slot


def _bill(key, account, model, usage, *, request, started=None, error="",
          stats: dict | None = None):
    """落一条 usage_logs。**绝不抛**（记账失败不能影响转发）。"""
    try:
        from admin.db import SessionLocal

        elapsed = int((time.perf_counter() - started) * 1000) if started else None
        ttfb = total = 0
        if isinstance(stats, dict):
            ttfb = int(stats.get("ttfb_ms") or 0)
            total = int(stats.get("total_ms") or 0)
        if not (0 < ttfb <= (total or elapsed or 0)):
            ttfb = 0
        latency = max(total, elapsed or 0) or None
        if not ttfb:
            ttfb = latency
        with SessionLocal() as db:
            shared.record_usage(
                db,
                key_id=int(getattr(key, "id", 0) or 0),
                account_id=int(getattr(account, "id", 0) or 0),
                model=model,
                usage=usage if isinstance(usage, dict) else {},
                public_platform=PUBLIC,
                storage=STORAGE,
                error_kind=error, client_ip=_client_ip(request),
                use_case="marvis-chat", seq=1,
                ttfb_ms=ttfb, latency_ms=latency,
                reasoning_effort=shared.reasoning_effort_of_request(request))
    except Exception:  # noqa: BLE001
        logger.exception("Marvis 记账失败")


#: 腾讯内容级风控**指纹表**（7 条全量移植自 DSH 插件
#: `dsh-providers-hub/lib/openai-gateway/tencent-fingerprint.js` 的
#: `CONTENT_FINGERPRINTS`）。
#:
#: ## 为什么要这张表
#:
#: 上游 `yybadaccess.3g.qq.com` 对 **system 消息正文**做**字面量**黑名单匹配
#: （腾讯内容风控，与 `buddy` / `workbuddy` 同一套）。命中即回
#: `HTTP 403 {"code":4100406,"msg":"非法请求"}` —— 客户端只看到「非法请求」，
#: 看不出是内容被拦，也看不出拦的是哪一句。
#:
#: ## 2026-11-16 真机定位（账号 189，直连上游 8 次探针，真实 DSH system 98,594 字符）
#:
#: | 探针 | system 形态 | 结果 |
#: |---|---|---|
#: | F0 基线 | 首行原样 | ❌ 403/4100406 ×2 |
#: | F1 | system 搬成**顶层字段**，messages 只留 user | ✅ 200 ×2 |
#: | F2 | 首行按本表第 4 条改写，其余逐字不动 | ✅ **200 ×2** |
#: | F3 | 首行前加一个 ASCII 空格 | ❌ 403 ×2 |
#: | F4 | 首行前加「换行 + 空格」 | ❌ 403 ×2 |
#:
#: ⇒ 两条**与旧结论相反**的事实（别照抄旧笔记）：
#:   1. **改写首行能放行**（F2 2/2 200），**行首加前缀不能**（F3 / F4 全 403）
#:      ⇒ 这条锚点不是「前接任意字符即放行」，「加个空格绕过」的写法是错的。
#:   2. F1 虽然也 200，但它只对**带顶层 `system` 字段**的请求成立；DSH 实际把
#:      system 放进 `messages[0]`（线上失败画像 `msgs=338`），F1 救不了那条路。
#:      ⇒ 落地选 **F2**（本函数），它是唯一对「system 在 messages[] 里」有效的形态。
#:
#: ## 移植纪律
#:
#: · 每条保留 JS 里的**匹配语义**：`find` 是**字面量**（`re.escape` + `re.I`，
#:   上游大小写不敏感），第 4 条用它登记的 `findRegex`（同时容忍
#:   `DeepSeek Harness` 与 `DeepSeekRuntime` 两种形态）；
#: · **只改内容，不删句子**：替换值与插件逐字一致，语义等义；
#: · **不命中的文本逐字符不变**（先 `search` 预检，命中才 `sub`）——
#:   腾讯侧按前缀缓存，无谓重建字符串会白吃缓存命中；
#: · ⚠️ 插件侧还实测过 **assistant 正文**同样会被拦，这里**不**顺手扩过去：
#:   本次故障的证据链只落在 system（8 次探针 + 线上 403 dump），扩大到
#:   assistant 会篡改会话历史。将来真出现「assistant 复述被拦」的报障，
#:   再按插件那张表补，别凭「统一一下」直接加。
_TENCENT_FINGERPRINT_RULES = (
    # 1. ZCode 内建常量（每个 git 仓库会话都会被注入）
    (re.compile(re.escape("Default branch (you will usually use this for PRs)"), re.I),
     "Main branch (PRs usually go here)"),
    # 2. Claude Code 身份句（刻意不带尾句点，一条覆盖两个常量形态）
    (re.compile(re.escape("You are Claude Code, Anthropic's official CLI tool for Claude"), re.I),
     "You are Claude Code, Anthropic's CLI for Claude"),
    # 3. Claude Code 反馈句
    (re.compile(re.escape("To give feedback, users should report the issue at "), re.I),
     "To give feedback, users should report the issue at the project issue tracker"),
    # 4. **DSH 身份首句 —— 本次 4100406 的触发串就是它**（system 绝对行首）
    (re.compile(r"You\s+are\s+an?\s+AI\s+(?:coding\s+)?agent\s*,?\s*powered\s+by\s+"
                r"(?:the\s+)?DeepSeek\s+Harness", re.I),
     "You are an AI coding assistant, powered by a large language model"),
    # 5. DSH Web 运行面的开头两句
    (re.compile(r"You are interacting with the user through the DeepSeek\s+Harness Web GUI ", re.I),
     "You are interacting with the user through the harness Web GUI "),
    # 6. DSH 的 `@` 文件引用说明句 —— 触发串**跨句边界**（两半各自放行），
    #    所以 find 必须写整段，砍半 = 静默空跑。
    (re.compile(re.escape("Tokens prefixed with @ are paths the user explicitly referenced. "
                          "Relative paths resolve from the workspace root"), re.I),
     "Tokens prefixed with @ are paths the user explicitly referenced. "
     "Paths resolve relative to the workspace root"),
    # 7. 客户端 SDK 的计费头字样
    (re.compile(re.escape("x-anthropic-billing-hdr"), re.I),
     "x-client-billing-header"),
)


def sanitize_tencent_fingerprints(text: str) -> str:
    """把一段文本里已知的腾讯内容风控指纹做**窄改写**（表见上）。

    · 不含任何指纹的文本**逐字符不变**（预检不命中就直接返回同一份内容）；
    · 只改内容，不删句子、不改结构。
    """
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern, replacement in _TENCENT_FINGERPRINT_RULES:
        if pattern.search(out) is None:
            continue
        out = pattern.sub(replacement, out)
    return out


def _sanitize_messages(messages: list) -> list:
    """对**每一条 system 消息**的 `content` 做指纹改写，其余角色原样不动。

    为什么是「每一条」而不是 `messages[0]`：插件那张表实测过**非首位 system
    同样被拦**，只改第一条等于留一条随时会踩的路径。改的时候**新建 dict**，
    不就地改调用方的 body（`_profile_of` 等已经读过它）。
    """
    out = []
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        content = message.get("content")
        if str(message.get("role") or "") != "system" or not isinstance(content, str):
            out.append(message)
            continue
        rewritten = sanitize_tencent_fingerprints(content)
        if rewritten == content:
            out.append(message)
            continue
        copied = dict(message)
        copied["content"] = rewritten
        out.append(copied)
    return out


def _prepare_body(body: dict, slot: str) -> dict:
    """把下游请求整理成上游能接受的样子。

    · `model` 换成槽位名；
    · 流式时补 `stream_options.include_usage` —— 少了它上游不返回 usage 帧，
      这张卡片和用量统计就永远是 0；
    · **system 正文过一遍腾讯内容风控指纹改写**（`4100406` 的修法，见上面
      `_TENCENT_FINGERPRINT_RULES` 的表与实测格）。`/v1/messages` 与
      `/v1/responses` 都经 `_run_normalized` → `chat_completions` 落到这里，
      所以只改这一处就覆盖三个入口。
    """
    out = dict(body)
    out["model"] = slot
    messages = out.get("messages")
    if isinstance(messages, list):
        out["messages"] = _sanitize_messages(messages)
    if out.get("stream"):
        opts = dict(out.get("stream_options") or {})
        opts["include_usage"] = True
        out["stream_options"] = opts
    return out


#: 允许「**同号**立刻重试一次」的瞬时失败 kind。
#:
#: 只有这两类是「网络抖动 / 上游瞬断」：换号没用（新号一样走同一条坏链路），
#: 同一个号隔几百毫秒再来一次往往就过去了。
#:
#: `auth`、`risk_control`、`signature_invalid` **一律不在白名单** ——
#: 它们不会因为「再打一次」而变好；上一版把瞬时 403 当成可重试信号
#: （`exclude.discard()` 后重新选号）实际是「换号重试」，既救不了请求也
#: 白占一个租约。
#:
#: `upstream_business` 整体也不在白名单 —— 唯一的例外是
#: `RETRY_SAME_ACCOUNT_BIZ_CODES` 里的业务码，见下。
RETRY_SAME_ACCOUNT_KINDS = frozenset({"network", "timeout"})
#: 上游偶发 403 4100406「非法请求」：实测同号同分钟 200/403 交替（约 5–10%），
#: 且事件循环阻塞修复后单次仅 ~5s ⇒ 同号重试**两次**（最多 3 次尝试）把失败率
#: 压到 ~0.1%（按每次 ~5–10% 独立概率估算：0.1³ = 0.1%、0.05³ ≈ 0.0125%）。
#:
#: 2026-11-16 线上反例：16:06:44.928 那条 403 elapsed 8869ms ≈ 两次尝试，
#: 即「首发 + 1 次重试」都撞上随机拒绝。单次概率没变（同一时段 60 次连打 0 个
#: 403），只是重试次数不够 —— 所以提到 2 次重试（3 次尝试）。
#:
#: 证据（2026-11-15 14:40–15:05，`request_audits` 21 行）：2 条 403
#: （14:49:51 用时 5.3s、14:52:46 用时 5.5s）+ 19 条 200，同一 key
#: （api_key_id=3）、同一模型（deepseek-v4.1-flash-marvis）。同号、同分钟
#: 内 200 与 403 交替 ⇒ 这是**上游偶发**拒绝，不是请求体本身错，也不是
#: 账号/凭证问题（换号救不了，所以仍然复用同一个账号）。
#:
#: ⚠ 只放这一个码：4100403（登录态过期）是 `auth`、4100404（风控）是
#: `risk_control`，都不在这里，也不会因为这条规则被重试。
RETRY_SAME_ACCOUNT_BIZ_CODES = frozenset({"4100406"})
#: 同号重试次数（不含首次）。2 ⇒ 最多 3 次尝试。
SAME_ACCOUNT_RETRIES = 2
#: 同号重试的**基数**退避（秒）：实际等待 = 基数 × (index + 1) + 抖动，
#: 即第 1 次重试 ~0.3s、第 2 次 ~0.6s（各再加 0–0.3s 随机抖动）。
#: 递增是为了避开上游的短时抖动窗口；抖动是为了避免同频重发。
SAME_ACCOUNT_RETRY_DELAY = 0.3
#: 退避抖动上限（秒），与 `SAME_ACCOUNT_RETRY_DELAY` 一起构成递增 + 抖动退避。
SAME_ACCOUNT_RETRY_JITTER = 0.3


def _warn_upstream_failure(account, exc: client.UpstreamError, *,
                           model: str, stream: bool) -> None:
    """上游失败打一条 warn —— 可观测性的最低要求。

    2026-11-15 之前 marvis 失败**一条日志都没有**：线上只能靠 `usage_logs`
    的毫秒时间戳反推「哪一秒开始串行化」，完全看不出失败原因（kind / 业务码 /
    到底是哪个号 / 上游原话）。这里把 kind、HTTP 状态、业务码、detail 前
    200 字符和 account id 一次打全。

    ⚠ **不含任何凭证**：detail 是上游返回体，access_token 只在请求头里。
    """
    detail = str(exc.detail or exc.message or "").replace("\n", " ").replace("\r", " ")
    logger.warning(
        "Marvis 上游失败 kind=%s status=%s code=%s acc=%s model=%s stream=%s "
        "detail=%s",
        exc.kind, exc.status or "-", exc.code or "-",
        int(getattr(account, "id", 0) or 0), model or "-",
        "1" if stream else "0", detail[:200])


def _profile_of(body: dict) -> dict:
    """请求形状画像 —— **只算数字，绝不复制内容**。

    2026-11-16 线上：4100406 只在大请求（主代理那条 `You are a helpful AI
    assistant…`）上偶发，同一时刻同一个号的小请求 200。要把「请求多大 /
    几条消息 / 几个工具 / 系统提示多长」和成功失败对上，才可能定位触发条件。

    这里算的字节数与 `client` 真正发出去的 JSON 完全一致
    （`json.dumps(body, ensure_ascii=False).encode()`）。
    """
    messages = body.get("messages")
    messages = messages if isinstance(messages, list) else []
    tools = body.get("tools")
    tools = tools if isinstance(tools, list) else []
    sys_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") not in ("system", "developer"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            sys_chars += len(content)
        else:
            sys_chars += len(str(content or ""))
    try:
        size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
    except Exception:  # noqa: BLE001 - 画像失败绝不能影响转发
        size = 0
    return {"bytes": size, "msgs": len(messages), "tools": len(tools),
            "sys_chars": sys_chars, "max_tokens": body.get("max_tokens")}


def _dump_403_enabled() -> bool:
    """临时转储开关是否打开（每次调用都读环境变量，默认关闭）。"""
    return str(os.environ.get(DUMP_403_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def _dump_403_keep() -> int:
    """最多保留几份；非法值退回默认 3。"""
    try:
        keep = int(str(os.environ.get(DUMP_403_KEEP_ENV) or "").strip() or
                   DUMP_403_DEFAULT_KEEP)
    except (TypeError, ValueError):
        return DUMP_403_DEFAULT_KEEP
    return keep if keep > 0 else DUMP_403_DEFAULT_KEEP


def _prune_dump_403(directory: str) -> None:
    """只留最新 N 份，删最旧的。

    文件名里带纳秒时间戳（`mh403-<ns>-<sha16>.json.gz`），定宽 ⇒ 字典序就是
    时间序，不用 stat 每个文件。
    """
    keep = _dump_403_keep()
    try:
        names = sorted(name for name in os.listdir(directory)
                       if name.startswith(DUMP_403_PREFIX)
                       and name.endswith(".json.gz"))
    except OSError:
        return
    for name in names[:max(0, len(names) - keep)]:
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:  # noqa: PERF203 - 删不掉不影响本次转储
            pass


def _dump_403_body(body: dict, *, status: int | None,
                   account_id: int = 0) -> str:
    """把**本次实际发出的 body 原始字节**写成 gzip，返回路径（未落盘返回空串）。

    字节与 `client.open_chat_stream` 发出去的完全一致：
    `json.dumps(body, ensure_ascii=False).encode()`（client.py 同一行），
    所以**原样重放这份 dump 就等于重放上游当时收到的那份请求**。

    只对 `status == 403` 落盘，且同内容只落一次（见 `_DUMP_403_SEEN`）。
    ⚠ 绝不把内容写进日志 —— 日志里只有路径与字节数。
    """
    if not _dump_403_enabled() or status != 403:
        return ""
    try:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    except Exception:  # noqa: BLE001 - 转储失败绝不能影响转发
        logger.warning("Marvis 403 转储：body 序列化失败，已跳过")
        return ""
    digest = hashlib.sha256(data).hexdigest()[:16]
    seen = _DUMP_403_SEEN.get(digest)
    if seen:
        return seen
    directory = str(os.environ.get(DUMP_403_DIR_ENV) or DUMP_403_DEFAULT_DIR)
    path = os.path.join(directory, "%s%d-%s.json.gz" % (
        DUMP_403_PREFIX, time.time_ns(), digest))
    try:
        os.makedirs(directory, exist_ok=True)
        # 先写 .part 再 rename：读到半截文件的重放脚本会拿到坏 JSON。
        partial = path + ".part"
        with gzip.open(partial, "wb") as handle:
            handle.write(data)
        os.replace(partial, path)
    except Exception as exc:  # noqa: BLE001 - 同上，绝不能影响转发
        logger.warning("Marvis 403 转储失败 path=%s: %s",
                       path, type(exc).__name__)
        return ""
    _prune_dump_403(directory)
    if len(_DUMP_403_SEEN) >= _DUMP_403_SEEN_MAX:
        _DUMP_403_SEEN.clear()
    _DUMP_403_SEEN[digest] = path
    # 只有路径与字节数 —— 内容一个字符都不进日志。
    logger.warning(
        "Marvis 403 请求体已转储 path=%s bytes=%d acc=%s（内容不进日志；"
        "排障结束必须删除）", path, len(data), account_id)
    return path


async def _call_upstream(data: dict, realm: str, body: dict, account_id: int, *,
                         stream: bool, profile: dict | None = None):
    """调上游 —— **整个调用跑在工作线程里**，只对「瞬时」失败同号重试（最多 2 次）。

    ## 什么算「瞬时」

    ① 白名单 kind（`network` / `timeout`）；
    ② `upstream_business` 里 **业务码命中 `RETRY_SAME_ACCOUNT_BIZ_CODES`**
       （当前只有 `4100406 非法请求`）—— 实测同号同分钟 200/403 交替，属上游
       偶发，重打一次就过；参数类错误（400/422）不在此列，换号/重打都无用。

    ## 为什么必须 `to_thread`

    `client.open_chat_stream` 内部是 `urllib.request.urlopen`：它会一直阻塞
    到上游把**响应状态行 + 响应头**吐回来。`admin.server` 是单进程单 worker
    的 uvicorn，在事件循环线程里调它等于把整个网关钉死 —— 2026-11-15 实测
    单次阻塞 3.3s/7s/17s/27s/**63.5s**，43 个在飞请求被串行化后超期，
    上游回 `4100406 非法请求`。非流式的 `chat_completions` 会 `read()`
    整个响应体，同样是阻塞调用，同样要挪出去。

    ## 为什么重试**不重新选号**

    `select_account` 是「换号」语义。瞬时网络失败时换号毫无意义，还会白占
    一个租约；这里重试**复用同一个已租账号**（不 `exclude.discard`、不重新
    `select`），与「换号」严格分开。

    换号由调用方 `_attempt` 负责：本函数把**同号重试耗尽**后照样抛，
    `_attempt` 捕获后把该号加入 `exclude` 再选下一个号，并打一条
    `Marvis 换号重试` 日志。2026-11-16 之前换号是**静默**的：线上只能看到
    一个号连打 3 次后回 403，看不出到底有没有第二个号可换。

    ## 请求画像（2026-11-16 加）

    每次上游调用打一条 INFO：`bytes / msgs / tools / sys_chars / max_tokens`；
    失败分支再打一条 `失败画像`，`_attempt` 成功时打 `成功画像`。
    4100406 只在大请求上偶发、同一时刻小请求 200 ⇒ 必须把「请求形状」和
    「成功 / 失败」放在一起比。**只记数字，绝不落 body 内容**（含用户对话）。
    """
    if profile is None:
        profile = _profile_of(body)
    logger.info(
        "Marvis 请求画像 acc=%s model=%s stream=%s bytes=%d msgs=%d tools=%d "
        "sys_chars=%d max_tokens=%s",
        account_id, body.get("model") or "-", int(bool(stream)),
        profile["bytes"], profile["msgs"], profile["tools"],
        profile["sys_chars"], profile["max_tokens"])
    for index in range(1 + SAME_ACCOUNT_RETRIES):
        try:
            if stream:
                return await asyncio.to_thread(
                    client.open_chat_stream, data, realm, body,
                    account_id=account_id)
            return await asyncio.to_thread(
                client.chat_completions, data, realm, body,
                account_id=account_id)
        except client.UpstreamError as exc:
            # 失败画像：与「成功画像」同列，按 bytes/msgs/tools 直接对比。
            logger.info(
                "Marvis 失败画像 kind=%s status=%s code=%s acc=%s bytes=%d "
                "msgs=%d tools=%d sys_chars=%d attempt=%d/%d",
                exc.kind, exc.status or "-", exc.code or "-", account_id,
                profile["bytes"], profile["msgs"], profile["tools"],
                profile["sys_chars"], index + 1, 1 + SAME_ACCOUNT_RETRIES)
            # 403 时把「本次实际发出的 body」转储一份（仅当
            # MIRAIHUB_MARVIS_DUMP_403=1；默认关闭，连目录都不建）。
            # 放 to_thread：3.5MB 的 gzip 要十几毫秒，不能钉在事件循环上。
            if exc.status == 403:
                await asyncio.to_thread(
                    _dump_403_body, body, status=exc.status,
                    account_id=account_id)
            # 白名单 kind（网络抖动 / 瞬断）**或** 上游偶发业务码（4100406）。
            # 后者只在 kind 已经是 `upstream_business` 时才看 —— `auth` /
            # `risk_control` 即便 detail 里带了同一个码也不重试。
            transient = exc.kind in RETRY_SAME_ACCOUNT_KINDS or (
                exc.kind == "upstream_business"
                and any(code in str(exc.detail or "")
                        for code in RETRY_SAME_ACCOUNT_BIZ_CODES))
            # 上限仍然**只由这一个标记**决定（`index + 1 > SAME_ACCOUNT_RETRIES`），
            # 没有第二个计数器：最多 1 + SAME_ACCOUNT_RETRIES = 3 次尝试。
            if index + 1 > SAME_ACCOUNT_RETRIES or not transient:
                raise
            # 递增 + 抖动退避：第 index 次尝试失败后等
            # 基数 × (index + 1) + U(0, 抖动上限) 秒。
            # 按每次 ~5–10% 独立概率估算，3 次尝试把失败率压到 ~0.1%
            # （0.1³ = 0.1%，0.05³ ≈ 0.0125%）。
            delay = (SAME_ACCOUNT_RETRY_DELAY * (index + 1)
                     + random.uniform(0, SAME_ACCOUNT_RETRY_JITTER))
            logger.warning(
                "Marvis 瞬时失败（kind=%s status=%s code=%s），同号重试 "
                "acc=%s 第 %s/%s 次重试，退避 %.2fs",
                exc.kind, exc.status or "-", exc.code or "-", account_id,
                index + 1, SAME_ACCOUNT_RETRIES, delay)
            await asyncio.sleep(delay)


async def _attempt(db, request, key, body: dict, realm: str, *, stream: bool):
    """选号 + 租约 + 调上游。换号语义与其它平台一致。

    ## 为什么是 async，且每一步都要 `to_thread`

    这里的三类调用全是**同步阻塞**的：

      · `shared.select_account` / `lease` / `note_failure`：SQLAlchemy 同步会话；
      · `marvis_accounts.ensure_token`：可能 `db.commit()`；
      · `client.open_chat_stream` / `chat_completions`：`urllib` 同步 HTTP。

    留在事件循环线程上，任何一次慢调用都是**全站**慢（见 `_call_upstream`
    的 63.5 秒事故）。所以这里一步都不留在循环上。

    ## 两层重试（2026-11-16 解耦）

    同一个号内部的重试在 `_call_upstream`（最多 1 + SAME_ACCOUNT_RETRIES = 3 次）；
    本函数只负责**换号**：同号重试耗尽 ⇒ 该号进 `exclude` ⇒ 选下一个号，
    最多 `MAX_ATTEMPTS` 个号。`auth`（4100403）不重试，直接冷却该号并换号；
    400/422 换号也没用，直接抛。

    换号成功打 `Marvis 换号重试 acc=A -> B（原因 kind=... code=...）`；
    一个号都没换成（池里只有它 / 全被 exclude）时打 `Marvis 换号终止`。
    2026-11-16 之前这两条日志都不存在，线上 403 无法区分「没号可换」和
    「没换号」—— 这次把换号做成**可数的事实**。
    """
    model = str(body.get("model") or "")
    exclude: set[int] = set()
    last_error: client.UpstreamError | None = None
    # 请求画像只算一次，本次请求的所有上游尝试共用（只记数字，不落内容）。
    profile = _profile_of(body)
    # 两层重试，各管各的上限：
    #   · 同一个号：`_call_upstream` 内最多 1 + SAME_ACCOUNT_RETRIES = 3 次；
    #   · 账号之间：本循环最多 MAX_ATTEMPTS = 3 个号（每号 3 次 ⇒ 最多 9 次）。
    # 换号 = 把失败号加入 `exclude` 后继续循环 —— `select_account` 会跳过它。
    switch_from = 0
    switch_reason: tuple[str, str] = ("", "")
    tried = 0
    for _ in range(MAX_ATTEMPTS):
        # `min_balance=0` = 只要余额 > 0 的号。
        #
        # Marvis 是令牌钱包制：额度用完的号凭证**是好的**，但每次调用都会被上游
        # 打回。不在这里挡掉的话，只能靠「撞一次 → 冷却 → 换号」，而冷却到期后
        # 它又排回队首（这些号从没成功过，`last_used_at` 恒为 NULL，选号按它升序）
        # —— 用户感受到的就是「一直失败，用不了」。NULL 余额（从没同步过）仍放行，
        # 否则新导入的号会被全部误杀。
        account = await asyncio.to_thread(
            shared.select_account, db, storage=STORAGE, realm=realm, key=key,
            model=model, exclude_ids=exclude, min_balance=0)
        if account is None:
            break
        if switch_from:
            # 真换到号了才打这条 —— 日志里能直接数出「换号重试」触发了几次。
            logger.info(
                "Marvis 换号重试 acc=%s -> %s（原因 kind=%s code=%s）",
                switch_from, int(account.id or 0),
                switch_reason[0] or "-", switch_reason[1] or "-")
            switch_from = 0
        if not await asyncio.to_thread(shared.lease, account):
            exclude.add(account.id)
            continue
        started = time.perf_counter()
        try:
            data = await asyncio.to_thread(
                marvis_accounts.ensure_token, db, account)
            tried += 1
            result = await _call_upstream(
                data, realm, body, int(account.id or 0), stream=stream,
                profile=profile)
            if not stream:
                shared.note_success(account)
                shared.release(account)
            logger.info(
                "Marvis 成功画像 acc=%s model=%s bytes=%d msgs=%d tools=%d "
                "sys_chars=%d elapsed_ms=%d",
                int(account.id or 0), model or "-", profile["bytes"],
                profile["msgs"], profile["tools"], profile["sys_chars"],
                int((time.perf_counter() - started) * 1000))
            return result, account, started
        except client.UpstreamError as exc:
            shared.release(account)
            # 可观测性：失败必须留痕（kind / status / 业务码 / detail / 号）。
            # ⚠ 放在「改写成用户文案」之前 —— 这里要的是**上游原话**。
            _warn_upstream_failure(account, exc, model=model, stream=stream)
            # 换号日志要写**上游原话**：auth 下面会被改写成 credential_expired，
            # 而排障时想知道的是上游那个 4100403。
            fail_kind, fail_code = exc.kind, exc.code or ""
            # 401 是「登录态过期」而不是「配置错了」—— 文案要能直接指导操作。
            # Marvis 的 token 实测只活约 2 小时（见 `TOKEN_TTL_SECONDS`），
            # 说「请检查凭证」只会让用户去翻配置，而答案是重新扫码。
            detail = exc.detail or exc.message
            if exc.kind == "auth":
                hint = marvis_accounts.token_expiry_hint(
                    marvis_accounts.credentials_of(account))
                age = hint.get("age_seconds")
                extra = ("（该凭证已签发 %d 分钟，超过约 2 小时的有效期）"
                         % (age // 60) if age is not None else "")
                exc = client.UpstreamError(
                    "Marvis 登录态已过期%s：请在控制台「授权登录」重新扫码（QQ / 微信）"
                    % extra, status=401, code="credential_expired", kind="auth",
                    detail=detail)
            await asyncio.to_thread(
                shared.note_failure, db, account, kind=exc.kind,
                detail=exc.detail or exc.message, status=exc.status,
                model=model, public_platform=PUBLIC,
                cool_seconds=(600 if exc.kind in ("auth", "risk_control") else 0))
            last_error = exc
            exclude.add(account.id)
            # 参数类错误换号也没用，直接抛（不再浪费一次上游往返）。
            if exc.status in (400, 422) and exc.kind not in (
                    "auth", "rate_limit", "risk_control"):
                raise exc
            # 走到这里 = 同号重试已经在 `_call_upstream` 里**耗尽**
            # （白名单 kind + 4100406 业务码，最多 2 次）⇒ 该换号了：
            # 失败号已进 `exclude`，下一轮选号自然换到下一个号。
            # `auth`（4100403）在 `_call_upstream` 里不重试，这里是**直接**
            # 冷却该号（note_failure 600s）并换号。
            switch_from = int(account.id or 0)
            switch_reason = (fail_kind, fail_code)
        except Exception as exc:  # noqa: BLE001
            shared.release(account)
            logger.warning("Marvis 调用失败 acc=%s: %s: %s",
                           int(getattr(account, "id", 0) or 0),
                           type(exc).__name__, str(exc)[:200])
            last_error = client.UpstreamError(
                "Marvis 调用异常（%s）" % type(exc).__name__, kind="upstream")
            exclude.add(account.id)
            switch_from = int(account.id or 0)
            switch_reason = ("exception", type(exc).__name__)
    if last_error is not None:
        if switch_from:
            # 换号没得换了（池子只有一个号 / 全被 exclude）—— 这条是排障关键：
            # 它证明「不是没试换号，是没人可换」。
            logger.warning(
                "Marvis 换号终止：已无更多可用账号（已试 %d 个号，最后 kind=%s "
                "code=%s）", tried, switch_reason[0] or "-",
                switch_reason[1] or "-")
        raise last_error
    # 走到这里说明**一个号都没选到**（选号返回 None 或全被租约挤掉）。
    # 「导入凭证」这句话只在池子真的空的时候成立；有 active 号却选不到，
    # 原因只有两种，得分开讲清楚（2026-11-15 用户实测被这句话误导：账号明明
    # 是 active、凭证也在，只是 access_token 过了约 2 小时有效期）。
    try:
        rows = await asyncio.to_thread(marvis_accounts.accounts_for, db, realm)
        alive = [r for r in rows if (getattr(r, "status", "") or "") == "active"]
        expired = [r for r in alive
                   if "过期" in str(getattr(r, "last_err_msg", "") or "")]
    except Exception:  # noqa: BLE001
        alive, expired = [], []
    # 「一个号都没选到」同样是网关失败，而且是最难从响应里看出原因的一种：
    # 响应里只有 503 + 一句人话，没有 kind / 没有上游。这里补一条 warn。
    logger.warning("Marvis 无可用账号 realm=%s model=%s active=%d expired=%d",
                   realm, model or "-", len(alive), len(expired))
    if alive and len(expired) == len(alive):
        raise shared.PlatformRejected(
            503, "Marvis 账号登录态已全部过期，请在控制台「授权登录」重新扫码"
                 "（access_token 由上游按天轮换，过期后只能重登）",
            kind="server_error", code="credential_expired",
            headers={"Retry-After": "5"})
    if alive:
        raise shared.PlatformRejected(
            503, "Marvis 账号都在冷却或排队中，请稍后重试",
            kind="server_error", code="accounts_cooling",
            headers={"Retry-After": "3"})
    raise shared.PlatformRejected(
        503, "Marvis 还没有账号，请在控制台「授权登录」扫码（QQ 或微信）",
        kind="server_error", code="no_available_account",
        headers={"Retry-After": "3"})


async def _iter_upstream(resp):
    """逐块读上游 SSE —— **每一块都在工作线程里读**，事件循环上不留阻塞调用。

    每取一块才发起下一次 `read`，消费者慢下来就自然形成背压；线程里的
    异常会在 `await` 处原样抛回，不需要自己搬运。

    ⚠ 建连（`open_chat_stream`）也是阻塞的，已经在上游的 `to_thread` 里；
    这里只负责「已建好的连接」的读取。
    """
    while True:
        chunk = await asyncio.to_thread(resp.read, 8192)
        if not chunk:
            break
        yield chunk


def _usage_of(result: dict) -> dict:
    usage = result.get("usage") if isinstance(result, dict) else None
    return usage if isinstance(usage, dict) else {}


async def chat_completions(request: Request):
    """OpenAI 兼容入口 —— 本平台的唯一主路径。"""
    body = await _body_of(request)
    realm = _realm(request)
    try:
        slot = _validate_model(body)
    except _BadRequest as exc:
        return JSONResponse({"error": {"type": exc.code, "message": exc.message,
                                       "code": exc.code}},
                            status_code=404 if exc.code == "model_not_found" else 400)

    upstream_body = _prepare_body(body, slot)
    wants_stream = bool(body.get("stream"))

    from admin.db import SessionLocal

    db = SessionLocal()
    try:
        # 鉴权、选号、上游调用全是同步实现 —— 一律在工作线程里跑（见 _attempt）。
        key = await asyncio.to_thread(
            shared.authenticate, db, request, platform=PUBLIC)
        resp, account, started = await _attempt(
            db, request, key, upstream_body, realm, stream=wants_stream)
    except shared.PlatformRejected as exc:
        db.close()
        # `openai()` 本身返回的就是 **JSONResponse**，不要再包一层 ——
        # 包了会 TypeError: Object of type JSONResponse is not JSON serializable。
        return exc.openai()
    except client.UpstreamError as exc:
        db.close()
        return _fail(exc)
    except Exception:
        db.close()
        raise
    db.close()

    if not wants_stream:
        # 非流式：整段响应体已经在 _call_upstream 的线程里读完（不再阻塞循环）；
        # 记账是同步 DB，也放到线程里。
        usage = _usage_of(resp)
        await asyncio.to_thread(
            _bill, key, account, slot, usage, request=request, started=started)
        return JSONResponse(resp)

    async def generator():
        stats = {"ttfb_ms": 0, "total_ms": 0}
        usage: dict = {}
        first_at = None
        began = time.perf_counter()
        buffer = b""
        try:
            async for chunk in _iter_upstream(resp):
                if first_at is None:
                    first_at = time.perf_counter()
                    stats["ttfb_ms"] = int((first_at - started) * 1000)
                yield chunk
                # 顺手把最后一个 usage 帧记下来 —— 不需要改动转发的字节
                buffer += chunk
                if b'"usage"' in buffer:
                    for line in buffer.split(b"\n"):
                        line = line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        payload = line[5:].strip()
                        if payload in (b"", b"[DONE]"):
                            continue
                        try:
                            obj = json.loads(payload)
                        except Exception:  # noqa: BLE001
                            continue
                        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
                            usage = obj["usage"]
                    # 只保留尾部，避免 buffer 无限增长
                    buffer = buffer[-8192:]
        finally:
            stats["total_ms"] = int((time.perf_counter() - began) * 1000)
            client.close_stream(resp)
            # 流正常读完才算这个号「用成功过」—— 失败的情况下 note_success
            # 会把刚才 note_failure 记的失败计数清零，等于把坏号洗白。
            shared.note_success(account)
            shared.release(account)
            _bill(key, account, slot, usage, request=request, started=started,
                  stats=stats)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _anthropic_to_openai(body: dict) -> dict:
    messages: list[dict] = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list):
        text = "".join(str(part.get("text") or "") for part in system
                       if isinstance(part, dict))
        if text.strip():
            messages.append({"role": "system", "content": text})

    for item in (body.get("messages") or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            content = "".join(parts)
        messages.append({"role": role, "content": content or ""})

    out = {
        "model": body.get("model") or endpoints.DEFAULT_MODEL,
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    if body.get("max_tokens"):
        out["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if isinstance(body.get("tools"), list):
        tools = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not name:
                continue
            tools.append({"type": "function",
                          "function": {"name": name,
                                       "description": tool.get("description") or "",
                                       "parameters": tool.get("input_schema") or {}}})
        if tools:
            out["tools"] = tools
    return out


def _responses_to_openai(body: dict) -> dict:
    """Responses 请求 → OpenAI chat 请求（**复用 protocol_bridge**）。

    2026-11-15 修：这里原来是自己手写的一个精简版，只搬 messages /
    max_output_tokens，**把 `tools` 整个丢了** —— 于是通过 `/v1/responses`
    发工具定义时，上游根本收不到，模型只能回「我没有 get_time 工具」
    （真机实测 function_call=0，而同一个请求走 `/v1/messages` 正常返回
    tool_use）。`protocol_bridge.responses_to_openai` 是各平台共用的成熟实现
    （工具、tool_choice、function_call 回传都处理过），直接用它。
    """
    from admin.platform import protocol_bridge as bridge

    out = dict(bridge.responses_to_openai(body) or {})
    # 模型 id 必须是槽位名（上游只认槽位），bridge 会原样保留上游名。
    out["model"] = endpoints.DEFAULT_MODEL
    if not out.get("messages"):
        # bridge 对纯字符串 input 已经处理；这里兜底空消息体
        out["messages"] = [{"role": "user",
                            "content": str(body.get("input") or "")}]
    return out


class _Inner:
    """给 bridge 中继用的内层：`body_iterator` 产出 OpenAI SSE 字节。"""

    def __init__(self, response):
        self.body_iterator = response.body_iterator


async def _run_normalized(request: Request, normalized: dict, *, protocol: str):
    """把已翻译好的 OpenAI 请求走一遍主路径，**再把响应翻回请求方协议**。

    ## 2026-11-15 修：这里原来少了「翻回去」这一步

    改造前是 `return await chat_completions(inner)` —— 直接把 OpenAI 的
    JSON / SSE 原样返回。Anthropic 客户端读 `content[]`、Responses 客户端读
    `output[]`，拿到的却是 `choices[]`，于是**流式零事件、非流式空文本**
    （真机实测：`/v1/messages` 与 `/v1/responses` 全空，只有
    `/v1/chat/completions` 正常）。协议翻译只做了一半。

    现在：上游返回后按 `protocol` 走 `protocol_bridge` 对应的中继/整形，
    与豆包、Loomy 等平台同一条路径。
    """
    from starlette.requests import Request as _Request

    from admin.platform import protocol_bridge as bridge

    payload = json.dumps(normalized, ensure_ascii=False).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    inner = _Request(request.scope, receive)
    response = await chat_completions(inner)

    if protocol == "anthropic":
        model = str(normalized.get("model") or "")
        if getattr(response, "status_code", 200) >= 400:
            return response                     # 错误原样返回（已是 JSON）
        if isinstance(response, StreamingResponse):
            return bridge.anthropic_stream_relay(_Inner(response), model,
                                                 prefix="marvis")
        # 非流式：JSONResponse.body 是已序列化的 bytes
        result = json.loads(response.body.decode("utf-8"))
        return JSONResponse(bridge.openai_to_anthropic(result, model))

    if protocol == "responses":
        model = str(normalized.get("model") or "")
        if getattr(response, "status_code", 200) >= 400:
            return response
        if isinstance(response, StreamingResponse):
            return bridge.responses_stream_relay(_Inner(response), model,
                                                 prefix="marvis")
        result = json.loads(response.body.decode("utf-8"))
        return JSONResponse(bridge.openai_to_responses(result, model))

    return response


async def messages(request: Request):
    """Anthropic 入口 → 转成 OpenAI 请求跑主路径 → 响应翻回 Anthropic。"""
    body = await _body_of(request)
    normalized = _anthropic_to_openai(body)
    return await _run_normalized(request, normalized, protocol="anthropic")


async def responses(request: Request):
    """Responses 入口 → 折叠成 chat/completions → 响应翻回 Responses。"""
    body = await _body_of(request)
    normalized = _responses_to_openai(body)
    return await _run_normalized(request, normalized, protocol="responses")
    """本地估算。口径：CJK 1 token/字，其它 4 字符 1 token（保守近似）。"""
    body = await _body_of(request)
    parts: list[str] = []
    if isinstance(body.get("system"), str):
        parts.append(body["system"])
    for message in (body.get("messages") or []):
        if isinstance(message, dict):
            content = message.get("content")
            parts.append(content if isinstance(content, str) else str(content or ""))
    text = "\n".join(parts)
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return JSONResponse({"input_tokens": cjk + max(1, (len(text) - cjk) // 4)})


async def list_models(_request: Request):
    """模型目录。

    Marvis 的模型网关**没有** `/v1/models`（实测无此端点），目录是客户端内置的
    槽位表，所以这里返回静态目录 —— 不猜上游。

    `id` 给**可读模型名**（`deepseek-v4.1-flash`）而不是槽位名
    （`main-auto`）—— 后者对外无法理解（用户实测反馈「怎么模型能叫这个
    名称呢」）。槽位名留在 `api_id` 里，调用时由 `canonical_model_id()`
    换回去，客户端不需要知道它是槽位。
    """
    data = [{"id": item["id"], "object": "model", "created": 0,
             "owned_by": PUBLIC, "name": item["name"],
             "api_id": item["api_id"], "context": item["context"],
             "display_name": item["name"]}
            for item in endpoints.models_catalog()]
    return JSONResponse({"object": "list", "data": data})


async def balance(request: Request):
    """余额快照 —— 汇总各账号的令牌钱包。"""
    from admin.db import SessionLocal

    realm = _realm(request)
    with SessionLocal() as db:
        summary = marvis_accounts.summarize_db(db, realm)
    return JSONResponse({"object": "balance", "platform": PUBLIC,
                         "realm": realm,
                         "total_tokens": summary["balance_total"],
                         "remain_tokens": summary["balance_remain"],
                         "accounts": summary["accounts"],
                         "active": summary["active"]})


async def dispatch(request: Request, path: str):
    """按路径分发（供 `PlatformSpec.gateway` 的通用入口调用）。"""
    if path in ("/v1/chat/completions", "/chat/completions"):
        return await chat_completions(request)
    if path in ("/v1/messages", "/messages"):
        return await messages(request)
    if path in ("/v1/responses", "/responses"):
        return await responses(request)
    if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
        return await count_tokens(request)
    if path in ("/v1/models", "/models"):
        return await list_models(request)
    if path in ("/v1/balance", "/balance"):
        return await balance(request)
    return JSONResponse({"error": {"message": "Marvis 不支持的路径：%s" % path,
                                   "type": "not_found"}}, status_code=404)