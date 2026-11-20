"""出口池泛化后的行为验证（**不带任何真实出口数据**）。

判据（「空池也能独立跑通」这件事必须被证明，不是靠嘴说）：

  1. 真值 `egress_pool.json` **不被 git 跟踪**（数据一律不进仓库；
     本地留着当部署兜底是允许的，但不能进版）；
  2. 空池 → `pick()` 返回 ""，`_opener()` 仍是平台级 opener，不抛异常；
  3. 同一账号永远同一出口（跨调用、跨 reload 都稳定）；
  4. `MARVIS_EGRESS_POOL`（内联 JSON / 逗号串）与
     `MARVIS_EGRESS_POOL_FILE`（文件）都能生效；
  5. 配了出口 → opener 装的是 `ProxyHandler(真实代理)`；
     没配 → 与改动前一致（`ProxyHandler({})`，硬禁代理）；
  6. `open_chat_stream()` 空池走直连、不绑 contextvar；有池就绑；
     传输层失败回退平台出口，业务错误**不回退**（不二次记账）；
  7. 启动期 fail-fast：池空 → WARNING（不崩），`MARVIS_EGRESS_REQUIRED=1`
     → 抛错 —— 因为「全新 clone 构建的镜像没有 egress_pool.json」这件事
     必须在第一屏日志里看得见，不能靠 rel2 工作副本里那份未跟踪文件兜着。

跑法：`python tests/test_marvis_egress_pool.py`（仓库现有测试都是这个形态）。
"""
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from admin import upstream_http
from admin.platform.marvis.core import client, egress

ok = fail = 0
_ENV_KEYS = ("MARVIS_EGRESS_POOL", "MARVIS_EGRESS_POOL_FILE",
             "MARVIS_EGRESS_REQUIRED", "MIRAIHUB_MARVIS_PROXY")


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("PASS  " + name)
    else:
        fail += 1
        print("FAIL  " + name + ("  " + str(extra) if extra else ""))


def env(**kw):
    """改环境变量（None = 删掉），返回恢复函数。"""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for key, value in kw.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    egress.reload()

    def restore():
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        egress.reload()

    return restore


def proxy_handler(opener):
    for handler in opener.handlers:
        if isinstance(handler, urllib.request.ProxyHandler):
            return handler
    return None


def main():
    # ---- 1. 真值出口池不进仓库（无条件） ----
    #
    # 判据是**「不被 git 跟踪」**，不是「文件不存在」：
    #
    #   · egress.py 的 docstring 写着「数据一律不进仓库」，example 的 _comment
    #     同样写着「真值属部署数据，不要提交」—— 仓库该向这句声明看齐；
    #   · 但部署机上**留着这个文件是对的**：没有环境变量覆盖时靠它兜底；
    #     文件一缺，egress.reload() 静默退回直连，出口 IP 一变就是 4100404，
    #     而报错面完全看不出是「池丢了」。
    #
    # ⇒ 这一条**无条件**必须过（文件在不在都要过）：只要它还被 git 跟踪，
    #   内网出口地址就随仓库发布出去了。2026-11-20 本批一度把它改成「文件在
    #   就合法即可」的条件断言，等于给一个 git 跟踪的真值池盖章 —— 那是拆护栏，
    #   现在装回无条件断言，只把判据从「文件不存在」换成「未被跟踪」。
    #
    # 形状检查保留：文件在 → 必须是合法 {"exits": [str, ...]}；不在 → 允许
    # （部署侧改用 MARVIS_EGRESS_POOL / _FILE，见 .env.example）。
    example = os.path.join(os.path.dirname(egress.DEFAULT_POOL_FILE),
                           "egress_pool.example.json")
    check("egress_pool.example.json 存在", os.path.exists(example))
    # `--error-unmatch`：路径被跟踪时返回 0（并打印路径），未被跟踪返回非 0。
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch",
         os.path.relpath(egress.DEFAULT_POOL_FILE, ROOT)],
        capture_output=True, cwd=ROOT)
    check("真值出口池不被 git 跟踪（数据一律不进仓库）",
          tracked.returncode != 0,
          tracked.stdout.decode("utf-8", "replace").strip())
    if os.path.exists(egress.DEFAULT_POOL_FILE):
        try:
            with open(egress.DEFAULT_POOL_FILE, encoding="utf-8") as fh:
                raw = json.load(fh)
            exits = raw.get("exits") if isinstance(raw, dict) else None
            ok_shape = (isinstance(exits, list)
                        and all(isinstance(x, str) and x for x in exits))
        except Exception as exc:  # noqa: BLE001 - 写坏的文件就是这里要抓的
            ok_shape = False
            raw = repr(exc)
        check("egress_pool.json 是合法出口池（exits 为字符串列表）",
              ok_shape, raw)

    # ---- 2. 空池：安全 ----
    # `MARVIS_EGRESS_POOL="[]"` 就是**真空池**：`_load()` 见首字符是 `[` 就直接走
    # `_parse_inline()`（egress.py:88-94）得到 []，**根本不读文件** —— 仓库里那个
    # 真值池在不在都不影响这个 fixture。
    # 以前这里把模块级常量 `DEFAULT_POOL_FILE` 临时指到 <tempdir>/mh-egress-absent.json
    # 再 reload()：既污染了模块全局（一个测试替身写进生产常量），又要写死一个 temp
    # 文件名。换环境变量手段即可，全局常量一个字都不用动。
    restore = env(MARVIS_EGRESS_POOL="[]", MARVIS_EGRESS_POOL_FILE=None)
    try:
        picked = [egress.pick(i) for i in (0, 1, "acct-x", None)]
        check("空池 pick() 全返回空串", picked == ["", "", "", ""], picked)
        check("空池 exits() 为空列表", egress.exits() == [])
        opener = client._opener()
        check("空池 _opener() 可用（不抛异常）", opener is not None)
        check("空池 _opener() 走平台级 opener（不是按出口缓存）",
              client._opener_for("") is not None)
        direct = client._opener_for("")
        check("空池 _opener_for(\"\") == 平台级 opener",
              direct is opener, (id(direct), id(opener)))
    finally:
        restore()

    # ---- 3. / 4. 池来源 ----
    pool = ["http://egress-a.example:8080", "http://egress-b.example:8080"]
    restore = env(MARVIS_EGRESS_POOL='["%s", "%s"]' % tuple(pool))
    try:
        check("内联 JSON 数组被解析", egress.exits() == pool, egress.exits())
        first = egress.pick(189)
        again = [egress.pick(189) for _ in range(20)]
        check("同一账号恒定同一出口", set(again) == {first}, first)
        egress.reload()
        check("reload 后仍稳定", egress.pick(189) == first)
        spread = {egress.pick(i) for i in range(64)}
        check("不同账号会分散到多个出口", spread == set(pool), spread)
        check("pick 只会返回池内出口",
              all(egress.pick(i) in pool for i in range(200)))
    finally:
        restore()

    restore = env(MARVIS_EGRESS_POOL="http://a.example:1 , http://b.example:2 ,")
    try:
        check("逗号串被解析（含尾逗号）",
              egress.exits() == ["http://a.example:1", "http://b.example:2"],
              egress.exits())
    finally:
        restore()

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pool.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"exits": ["http://c.example:3"]}')
        restore = env(MARVIS_EGRESS_POOL=None, MARVIS_EGRESS_POOL_FILE=path)
        try:
            check("MARVIS_EGRESS_POOL_FILE 生效",
                  egress.exits() == ["http://c.example:3"], egress.exits())
        finally:
            restore()
        # 早期用法：环境变量直接给文件路径
        restore = env(MARVIS_EGRESS_POOL=path, MARVIS_EGRESS_POOL_FILE=None)
        try:
            check("MARVIS_EGRESS_POOL 给文件路径也能读（兼容旧用法）",
                  egress.exits() == ["http://c.example:3"], egress.exits())
        finally:
            restore()
        restore = env(MARVIS_EGRESS_POOL=None,
                      MARVIS_EGRESS_POOL_FILE=os.path.join(tmp, "nope.json"))
        try:
            check("文件不存在 → 空池，不抛异常", egress.exits() == [])
        finally:
            restore()

    # ---- 5. opener 上的 ProxyHandler ----
    opener = upstream_http.build_opener(None, proxy="http://p.example:9000")
    handler = proxy_handler(opener)
    check("build_opener(proxy=) 装 ProxyHandler",
          handler is not None and handler.proxies == {
              "http": "http://p.example:9000",
              "https": "http://p.example:9000"}, handler and handler.proxies)
    # 不传 proxy 时必须与改动前**完全等价**：handler 链一模一样，
    # 且链里没有任何带代理的 ProxyHandler（= 不读系统代理 / HTTP_PROXY）。
    plain_new = upstream_http.build_opener(None, proxy=None)
    plain_old = upstream_http.build_opener(None)
    names = lambda opener: [type(h).__name__ for h in opener.handlers]
    check("不传 proxy 时 handler 链与改动前一致",
          names(plain_new) == names(plain_old), names(plain_new))
    check("不传 proxy 时不装任何代理",
          proxy_handler(plain_new) is None,
          [getattr(h, "proxies", None) for h in plain_new.handlers])

    restore = env(MIRAIHUB_MARVIS_PROXY="http://platform.example:3128")
    try:
        upstream_http.reset_platform_openers()
        check("platform_proxy_url 读 MIRAIHUB_<平台>_PROXY",
              upstream_http.platform_proxy_url("marvis")
              == "http://platform.example:3128")
        handler = proxy_handler(upstream_http.platform_opener("marvis"))
        check("platform_opener 装上该代理",
              handler is not None
              and handler.proxies.get("https") == "http://platform.example:3128")
        opener_a = upstream_http.platform_opener("marvis")
        opener_b = upstream_http.platform_opener("marvis")
        check("platform_opener 有缓存（同一实例）", opener_a is opener_b)
        upstream_http.reset_platform_openers()
        check("reset_platform_openers 后重建新实例",
              upstream_http.platform_opener("marvis") is not opener_a)
        check("其它平台不受影响",
              upstream_http.platform_proxy_url("loomy") == "")
    finally:
        restore()
        upstream_http.reset_platform_openers()

    # ---- 6. open_chat_stream 的出口绑定 / 回退 ----
    real_direct = client._open_chat_stream_direct
    seen = []
    behaviour = {"raise": None}

    def fake_direct(credential, realm, body, *, account_id=0):
        proxy = client._EGRESS_VAR.get()
        seen.append(proxy)
        if behaviour["raise"] and proxy:
            raise behaviour["raise"]
        return "resp"

    client._open_chat_stream_direct = fake_direct
    try:
        # 同第 2 节：`"[]"` 让 `_load()` 走内联解析、直接得到空池，不读文件兜底，
        # 也就不需要动 DEFAULT_POOL_FILE 这个模块全局。
        restore = env(MARVIS_EGRESS_POOL="[]", MARVIS_EGRESS_POOL_FILE=None)
        try:
            seen.clear()
            check("空池：open_chat_stream 返回正常",
                  client.open_chat_stream({}, "r", {}) == "resp")
            check("空池：不绑出口（contextvar 为空）", seen == [""], seen)
        finally:
            restore()

        restore = env(MARVIS_EGRESS_POOL='["http://egress-a.example:8080"]')
        try:
            seen.clear()
            client.open_chat_stream({}, "r", {}, account_id=7)
            check("有池：请求绑到该账号出口",
                  seen == ["http://egress-a.example:8080"], seen)
            check("退出后 contextvar 已还原",
                  client._EGRESS_VAR.get() == "", client._EGRESS_VAR.get())

            seen.clear()
            behaviour["raise"] = client.UpstreamError("boom", kind="network")
            check("传输层失败 → 回退平台出口",
                  client.open_chat_stream({}, "r", {}) == "resp")
            check("回退那次确实没绑出口", seen[-1] == "", seen)

            seen.clear()
            behaviour["raise"] = client.UpstreamError("biz",
                                                      kind="upstream_business")
            raised = None
            try:
                client.open_chat_stream({}, "r", {})
            except client.UpstreamError as exc:
                raised = exc
            check("业务错误不重发（原样抛出）", raised is not None, raised)
            check("业务错误只发了一次", len(seen) == 1, seen)
        finally:
            behaviour["raise"] = None
            restore()

        seen.clear()
        opener_a = client._opener_for("http://egress-a.example:8080")
        opener_b = client._opener_for("http://egress-a.example:8080")
        check("按出口缓存 opener", opener_a is opener_b)
        handler = proxy_handler(opener_a)
        check("按出口的 opener 里是这条代理",
              handler is not None
              and handler.proxies.get("https") == "http://egress-a.example:8080",
              handler and handler.proxies)
        client.reset_egress_openers()
        check("reset_egress_openers 后重建",
              client._opener_for("http://egress-a.example:8080") is not opener_a)
    finally:
        client._open_chat_stream_direct = real_direct
        client.reset_egress_openers()

    # ---- 7. 启动期 fail-fast：池空不许静默 ----
    #
    # 这一节锁的是**全新 clone 构建**这条路：`egress_pool.json` 不进 git
    #（数据一律不进仓库），新 clone 出来的镜像里没有这个文件 —— 池一空
    # `_load()` 就返回 []，`pick()` 全 ""，请求退回直连；出口 IP 一变上游就是
    # 4100404，而错误面完全看不出「池丢了」。线上 5e4fb04 能跑，只因为构建源
    # rel2 的工作副本里恰好留了这个未跟踪文件 —— 这条隐式依赖必须被日志打穿。
    from admin.platform import marvis as marvis_platform

    records = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    def warns():
        return [r for r in records if r.levelno >= logging.WARNING]

    collector = _Collect()
    marvis_logger = logging.getLogger("admin.platform.marvis")
    saved_level = marvis_logger.level
    marvis_logger.addHandler(collector)
    marvis_logger.setLevel(logging.DEBUG)  # INFO 也要收，后面按 levelno 过滤
    try:
        # 7.1 全新 clone 的形态：**无 env、无文件** → 启动不崩，但必须 WARNING。
        # 直接把 DEFAULT_POOL_FILE 指到不存在的临时路径来复现（模块全局，
        # finally 里立刻还原 —— 这里必须动它，否则本机那份真值池会把空池盖住，
        # 测的就不是「全新 clone」了）。
        real_pool_file = egress.DEFAULT_POOL_FILE
        restore = env(MARVIS_EGRESS_POOL=None, MARVIS_EGRESS_POOL_FILE=None)
        with tempfile.TemporaryDirectory() as tmp:
            egress.DEFAULT_POOL_FILE = os.path.join(tmp, "egress_pool.json")
            egress.reload()
            try:
                records.clear()
                crashed, ready = None, None
                try:
                    ready = marvis_platform.check_egress()
                except Exception as exc:  # noqa: BLE001 - 崩了就是这里要抓的
                    crashed = exc
                check("全新 clone（无 env、无文件）→ 启动不崩", crashed is None, crashed)
                check("全新 clone → check_egress() 返回 False", ready is False, ready)
                check("全新 clone → exits() 为空", egress.exits() == [])
                hit = warns()
                check("全新 clone → 打 WARNING（池空必须有声音）",
                      len(hit) == 1, [r.getMessage() for r in hit])
                text = hit[0].getMessage() if hit else ""
                check("WARNING 写明注入方式（MARVIS_EGRESS_POOL）",
                      "MARVIS_EGRESS_POOL" in text, text)
                check("WARNING 写明后果（4100404）", "4100404" in text, text)
            finally:
                egress.DEFAULT_POOL_FILE = real_pool_file
                restore()

        # 7.2 env 显式空池（"[]"）：同样 WARNING、同样不崩。
        restore = env(MARVIS_EGRESS_POOL="[]", MARVIS_EGRESS_POOL_FILE=None,
                      MARVIS_EGRESS_REQUIRED=None)
        try:
            records.clear()
            check('env "[]" → exits() 为空', egress.exits() == [])
            check('env "[]" → check_egress() False 且不崩',
                  marvis_platform.check_egress() is False)
            check('env "[]" → 打 WARNING', len(warns()) == 1,
                  [r.getMessage() for r in records])
        finally:
            restore()

        # 7.3 有池（内联 JSON）：解析正确、不 WARNING、INFO 里看得见条数。
        restore = env(MARVIS_EGRESS_POOL='["http://egress-a.example:8080"]',
                      MARVIS_EGRESS_POOL_FILE=None, MARVIS_EGRESS_REQUIRED=None)
        try:
            records.clear()
            check("内联 JSON → exits() 解析正确",
                  egress.exits() == ["http://egress-a.example:8080"], egress.exits())
            check("内联 JSON → check_egress() True",
                  marvis_platform.check_egress() is True)
            check("有池 → 不打 WARNING", warns() == [],
                  [r.getMessage() for r in warns()])
            check("有池 → INFO 里有出口条数",
                  any(r.levelno == logging.INFO and "1 个出口" in r.getMessage()
                      for r in records), [r.getMessage() for r in records])
        finally:
            restore()

        # 7.4 MARVIS_EGRESS_REQUIRED=1 + 池空 → 硬失败（strict=True 才走）。
        restore = env(MARVIS_EGRESS_POOL="[]", MARVIS_EGRESS_POOL_FILE=None,
                      MARVIS_EGRESS_REQUIRED="1")
        try:
            raised = None
            try:
                marvis_platform.check_egress(strict=True)
            except RuntimeError as exc:
                raised = exc
            check("MARVIS_EGRESS_REQUIRED=1 且池空 → 抛 RuntimeError",
                  raised is not None, raised)
            check("抛错信息带 REQUIRED 说明",
                  raised is not None and "MARVIS_EGRESS_REQUIRED=1" in str(raised),
                  raised)
            # install() 走的是 strict=False：同一状态下只告警，不能把平台装配打断。
            check("同一状态 check_egress() 不抛（install 路径只告警）",
                  marvis_platform.check_egress() is False)
        finally:
            restore()
    finally:
        marvis_logger.removeHandler(collector)
        marvis_logger.setLevel(saved_level)

    # ---- 7.5 接线必须真的存在（删掉调用就等于把闸门拆了） ----
    with open(os.path.join(ROOT, "admin", "platform", "marvis", "__init__.py"),
              encoding="utf-8") as fh:
        marvis_src = fh.read()
    with open(os.path.join(ROOT, "admin", "server.py"), encoding="utf-8") as fh:
        server_src = fh.read()
    check("install() 里调了 check_egress()", "    check_egress()" in marvis_src)
    check("FastAPI startup 里调了 check_egress(strict=True)",
          "check_egress(strict=True)" in server_src)

    print()
    print("%d/%d passed" % (ok, ok + fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
