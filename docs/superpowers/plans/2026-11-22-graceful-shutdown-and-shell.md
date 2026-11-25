# MiraiHub 优雅退出与桌面、Web 前端壳实施计划

**用户已确定的行为**　覆盖 Windows 桌面窗口、托盘和浏览器管理台。用户正常停止、退出、重启或安装更新时，已经接收的工作可以继续跑到结束。等待没有自动强退时限。只有用户明确选择并确认“强制结束”才允许中断。

**实现目标**　用一次真实的长请求验收。客户端收到第一帧后点重启，旧实例停止接新业务，原客户端仍能收到尾帧，服务完成记账和资源释放后退出。此时才启动下一代实例。桌面和浏览器都能解释当前状态，且在新实例就绪后各自恢复工作台。

**技术与基线**　Python、FastAPI/Starlette 原生 ASGI、Uvicorn、Windows PyWebView/pystray、原生浏览器 JS，沿用现有打包和部署方式。按编写时的 `main` 提交 `ea20a57` 核对。以下以函数名定位，实施前在目标分支重新定位。本文就是需求、接口与验收规格，可单独交给实施模型。

## 交给实施模型的第一条指令

> 按任务 1 至 8 顺序实施。一次只完成一个能验收的切片。先写能重现该问题的最小回归，看到对应失败，再改产品代码并跑同一检查。每次交接给出变更文件、命令、观察到的状态或响应、通过与待验项。沿用仓库现有语言和依赖。测试在临时数据目录和动态端口运行，使用合成流和临时数据库。保留明确标为“持久回归”的测试。清理本次创建的临时文件，保留用户的工作文件、备份和证据。

优先打通任务 1–5 的纵向链路：一个请求、一个子进程、两个壳。任务 6 处理第二个启动器与守护脚本，任务 7 补有反向代理时的冷打开和部署，任务 8 做跨入口验收。每一步都可单独评审，整体验收以文末表格为准。

## 已经从源码确认的入口

| 位置 | 已有行为 | 容易复现的结果 |
|---|---|---|
| `desktop/app.py` 的 `_start_worker`、`start`、`retry_start` | 获取启动锁后，worker 发现现有子进程活着就 `terminate()`。探测 `/api/app/desktop-ready` 时已有每次启动独立的 nonce。 | 已运行时重复点启动/重试，可能杀掉正在服务的实例。`open_console` 另用 TCP 探活，两条路径对就绪的判断不同。 |
| 同文件的 `teardown_backend`、`stop_services`、`_restart_worker`、`quit`、`run` | 停止先 terminate，超时尝试杀树。后端配置 `timeout_graceful_shutdown=5`。托盘退出末尾另有 3 秒 `os._exit(0)`。无托盘窗口通过 `closed` 停服务，函数末尾还有 `finally`。 | Windows terminate 会跳过应用排空；关最后一个窗口也会提前切断它自己持有的连接。 |
| `desktop/app.py:apply_update`、`desktop/apply-update.ps1` | 校验和写权限预检后，先启动等旧进程退出的安装脚本，再逐个 destroy 窗口。 | 更新绕过停止流程，托盘的关窗回调还可能把窗口隐藏，导致更新脚本一直等待。 |
| `admin/routers/maintenance.py:restart_service`、`admin/console/review-improvements.js:doRestartService` | 服务端确认后等待 1.5 秒调用 `os._exit(0)`。前端收到成功后固定 8 秒整页刷新。 | 正在生成或落库的工作会被截断，慢启动则把还能显示状态的旧页面刷成错误页。 |
| `admin/request_metrics.py:TimingMiddleware`、`admin/routers/logs.py:_activity_probe` | 中间件只观测部分 `/v1/`，跳过音频转录。活动页按推理路径过滤。中间件先移出 `_active`，后面才执行持久化审计。 | 活动页显示 0 时音频、管理端写入、审计收尾都可能仍在运行。 |
| `main.py:_ensure_port_free`、`_kill_children`、`scripts/daemon.ps1`、`stop-service.bat` | 启动可自动接管旧服务。监督器结束孩子的总预算约 12 秒。PowerShell 停止会强杀监听者并枚举 `main.py`，BAT 还按 8790 强杀兜底。 | 修了桌面按钮，普通启动或停止脚本仍可中断同一批请求。 |
| `admin/server.py:admin_index`、`admin/console/desktop.js:initializeNative`、`console-app.js` 末尾的启动探针 | 服务端用 Cookie 或本地运行时决定下发完整台或中性登录页。本地壳通过桥换会话。浏览器另有 8 秒后强行 `enterApp()` 的看门狗。 | 加一层遮罩却保留定时器，会出现旧计时器越过等待页或把失效会话重新显示为已登录。 |
| `scripts/upgrade_control.py`、`scripts/upgrade_gate.py`、`docker-compose.yml` | 已有带过期时间的升级标记和等候代理，CLI 的 drain 读取活动日志，end 和代理用版本接口判断就绪。Compose 的停止宽限为 5 秒。 | 在 docker stop 之后才排空已经太晚。版本接口返回 200 也没有证明新实例可以接业务。 |

## 一、跨层契约

### 谁决定什么

1. **业务 worker** 决定是否还能接新工作，报告自己准确的在途数量。先原子地关闭准入，再等待。它持有真实的 Uvicorn Server，并负责正常走完 lifespan shutdown。
2. **进程所有者**指实际启动 worker 的桌面父进程或 `main.py` 监督器。它串行处理启动、停止、重启与更新，等待操作系统确认子进程退出。进程句柄和本次启动随机标识共同标记一代。UI、托盘、脚本都向同一所有者提交意图。直接由容器管理的部署见任务 7。
3. **两个前端**显示同一组阶段。桌面可以通过桥读取所有者状态，后端掉线时仍能显示。浏览器在已加载的页面里保留 DOM，通过同源探针重连。后端完全离线时首次打开 Web 页，由独立运行的反向代理提供小型等待页。

### 状态和操作

| 阶段 | 谁能确认 | 界面与动作 |
|---|---|---|
| `starting` | 所有者已开始启动，worker 尚未确认就绪 | “正在启动本地服务”；显示实际阶段和诊断入口。重复启动合并。慢启动可继续等待。 |
| `ready` | 目标 worker 完成必要初始化，准入已打开 | 进入工作台，允许提交业务。重复启动返回现有实例。 |
| `draining` | worker 已关闭准入 | “正在重启 · 剩余 2 个请求、1 个后台任务 · 已等待 37 秒”。正常等待持续到零。可查看详情或手动强退。 |
| `stopping` | 已排空，正在关闭监听、刷新持久化数据或等待进程退出 | “请求已完成，正在关闭服务”。保留壳，直到进程退出或进入下一代启动。 |
| `stopped` | 所有者已确认旧进程退出 | “服务已停止”，保留桌面壳和启动按钮；若原意图是退出，才关闭宿主。重启则立即进入下一代 starting。 |
| `failed` | 启动报错或进程意外退出，有可解释原因 | 展示诊断、重试。控制通道失联且进程仍活时显示“状态暂不可用”，保留最后观测时间，数量显示“未知”。 |

使用 `instance_id` 标识一次后端启动，`operation_id` 标识一次停止/重启/更新。两者均为随机不透明值，不充当凭据。后台和前端的旧回调都要核对这两个字段。相同意图返回已有操作；冲突意图返回 409 和当前动作。退出与更新的冲突也遵守此规则。冷启动时点退出允许撤销尚未完成的启动，最终等本代孩子退出。已停止时退出直接收壳。

建议的轮询间隔、慢提示和视觉文案是实现默认值。1 秒刷新进度，30 秒显示“仍在等待”，90 秒开放启动诊断。时间用单调时钟计算。**任何这些计时器触发时，进程处置次数必须为 0。**

### 接口数据

```json
{
  "phase": "draining",
  "action": "restart",
  "instance_id": "old-instance",
  "operation_id": "requested-operation",
  "active_requests": 2,
  "active_jobs": 1,
  "elapsed_ms": 37000,
  "can_restart": true,
  "can_force": true
}
```

- `GET /api/app/ready` 新增匿名、轻量、同源探针，只返回 `{"ready":true}`（200）或 `{"ready":false}`（503），加 `Cache-Control: no-store`。监听未开始时允许连接失败。版本号继续由原接口提供，普通存活检查在排空时仍能工作。
- `GET /api/app/lifecycle` 需 `require_admin`，返回上述快照。空闲时 action、operation_id 为 null。快照只含阶段、计数及能力，禁止加入原始请求、账号、密钥或本机路径。桌面 `Bridge.startup_status()` 增量返回同一结构，同时保留 `message`、`failed` 两个旧字段。
- `POST /api/maintenance/restart` 沿用 `{"confirm":true}`，成功为 202，保留原 `ok`、`message` 并附快照。增加 `POST /api/maintenance/restart/force`，须提交 `{"confirm":true,"instance_id":"…","operation_id":"…"}`。仅允许加速进行中的同一重启操作。桌面退出/停止/更新的强制动作走本机壳。受管桌面从普通浏览器进入时，Web 重启也交给桌面父进程。
- 保留原路由鉴权、Cookie/CSRF、显式确认、60 秒新操作频控与审计。重复点击已受理操作应在鉴权后先判幂等，再核对新操作频控。沿用 `env_guard` 与 `_fs_guard` 的生产 404、未放行 409 行为。无重启所有者的运行方式返回可读 409，并报告 `can_restart=false`。状态丢失、401、403、404 都不等于排空完成。

## 任务 1　先复现请求被截断，再做原子准入和计数

**文件**　新增 `admin/lifecycle.py`，更新 `admin/server.py`。持久回归放 `tests/test_graceful_lifecycle.py`。按现有白名单习惯只放行本功能的具体持久测试文件。

- [ ] 写一个最小 ASGI 子应用，用两个 `asyncio.Event` 分别卡住最后一帧和最后一次清理。先发响应头与第一帧，开始 drain，断言第二个新业务请求立即 503，释放第一道闩后仍能等到第二道闩。最后放行清理才允许计数归零。先跑 `python -m unittest discover -s tests -p test_graceful_lifecycle.py -v` 记录失败。
- [ ] 实现每个应用运行实例自己的 `Lifecycle`，暴露 `try_admit(kind)`、`finish(lease)`、`begin(action, operation_id)`、`snapshot()` 和可由事件循环异步等待的 `wait_empty()`。阶段检查与加入在途集在同一短锁内完成，begin 在同一把锁中封口。使用唯一租约使释放幂等，锁外通知等待者，唤醒后重验谓词。线程完成任务通过安全的线程唤醒机制通知事件循环。每次 lifespan 启动重置，避免同进程重建 app 时继承上次 draining。
- [ ] 写原生 ASGI 中间件，不读取请求体、不缓存输出。目标层序从外到内为现有封禁和 IP 防护 → 生命周期 → `TimingMiddleware` → 原准入队列与业务 dispatch。Starlette 后注册者在外，按 `app.user_middleware` 及一条真的优先返回路径验证。对既有生产封禁路径放行到旧策略或在排空前复用同一封禁判定，确保仍返回 404。生命周期生成的 503 补正确 JSON 类型、`Retry-After: 3`、`Cache-Control: no-store`；跨域已启用的网关验证原 CORS 响应头仍可见。

```python
# 业务分支的计数边界。send 原样传递，应用清理和审计完成后才释放。
lease = lifecycle.try_admit("request")
if lease is None:
    return await draining_response(scope, receive, send)
try:
    await self.app(scope, receive, send)
finally:
    lifecycle.finish(lease)
```

- [ ] 默认纳入所有会触发业务或修改数据的 HTTP，包含完整 `/v1` 子树、`POST /mcp`、管理台 Agent、上传、导入、恢复和导出；排队中的请求也持有租约。旧请求仍按原限流、超时、取消和计费语义执行。观察用的独立探针、静态页面/资源、会话核验、生命周期操作、`/api/logs/requests/stream` 与 `/api/logs/usage/stream` 按明确的方法和路径豁免。给豁免表逐条写理由，不按 `/api/*` 或 `text/event-stream` 一概排除。查看实际登录/换票路由，覆盖壳使用的只读探针和必要的凭据交换。非 HTTP scope 透传。
- [ ] 为 `/v1/messages`、OpenAI 兼容路径及 MCP 分别保留对应错误信封。只拒绝尚未准入的请求，业务码统一 `service_draining` 或依协议映射到可重试错误；已有 SSE 持续写原协议尾帧。对于 MCP 的未准入调用可在 HTTP 层明确 503，避免为取 RPC id 预读请求体。登录、权限、模型不可用、限流等普通错误仍由原流程生成。
- [ ] 为客户端断开、下游异常、发送末帧失败、尚未获得限流槽、两个 begin 并发、最后一个 finish 与新请求同时到达写回归。每个场景检查没有负数、重复释放或错误的零。请求日志仍可显示自己的统计口径。

第一个最小回归可从下面的断言起步，放在 `unittest.IsolatedAsyncioTestCase` 中。约定 `mark_ready()` 只允许从 starting 转入 ready，`begin()` 返回快照，`wait_empty()` 等到两种计数都归零。随后加上真实 ASGI/子进程夹具，才能覆盖流和进程退出顺序。

```python
async def test_begin_seals_admission_before_waiting(self):
    state = Lifecycle()
    state.mark_ready()
    first = state.try_admit("request")
    self.assertIsNotNone(first)
    snapshot = state.begin("restart", "op-1")
    self.assertEqual(snapshot["active_requests"], 1)
    self.assertIsNone(state.try_admit("request"))
    waiter = asyncio.create_task(state.wait_empty())
    await asyncio.sleep(0)
    self.assertFalse(waiter.done())
    state.finish(first)
    await asyncio.wait_for(waiter, timeout=1)
    state.finish(first)
    self.assertEqual(state.snapshot()["active_requests"], 0)
```

壳需要穿过排空的现成入口至少包括 `GET /api/app/version`、`GET /api/app/desktop-ready`、`GET /api/accounts/probe`、`GET /api/login/session`；最后一个是本地凭据交换。观察流在排空后应快速关闭，重新取票和连接不应形成循环。单独验证匿名请求排空期间仍看不到后台内容、外部用户猜一个实例号也不能控制进程。

## 任务 2　把请求结束后的工作、就绪与关闭顺序接入同一状态

**文件**　`admin/jobrunner.py:JobRunner.start`、`admin/scheduler.py:_loop`、`admin/request_metrics.py:TimingMiddleware`、`admin/routers/logs.py`、`admin/routers/releases.py`、`admin/server.py:_startup`。补任务 1 的持久回归。

- [ ] 审计“HTTP 已返回，但线程仍在工作”的入口。至少核对 `JobRunner.start` 的守护线程、`admin/routers/agent.py` 的子 task、`admin/routers/proxy.py` 的同步 I/O 收尾、视频任务 ID 入库和审计 `save`。对已 await 到结束的路径，由外层租约覆盖。后台已持久化、可以在下次启动接续的远程视频，仅等本机提交及任务 ID 落库，不轮询远端视频直至生成完毕。
- [ ] 对真正脱离请求且需要完成的工作，在启动线程/调度 task **之前**取得 job 租约。线程启动失败、正常完成、异常都在同一所有权下释放；复用已经 running 的 Job 时不新增计数。已准入请求后来才派生的必要工作允许继承父租约，必须先在锁内登记子租约再释放父租约；显式把父租约传到线程边界。独立定时器每一轮在封口后跳过新派发，已经开始的有限写入完成后释放。禁止把常驻调度循环本身计作一个永远活跃的 Job。
- [ ] 在两个后台日志观察流的生成循环里检测 drain 并主动结束，前端收到结束后暂停重连。这两类页面订阅仍会被 Uvicorn 等待，仅从计数排除还不足以让进程关掉。另测同一时刻的业务 SSE 能继续发送，不要统一取消所有 task。
- [ ] `_startup` 已经支持数据库故障时降级启动，`init_db()` 有布尔返回值。新 readiness 在必要初始化完成且数据库/必要表可用时再开；保留诊断与存活接口。对降级启动，在后台用有超时、单飞、限频的实际 DB 就绪探测重新检查并补一次所需初始化，再发布 ready。Node 准备、检查更新、可选平台预热不挡就绪。排空期间迟来的 DB 探针无权把准入改回 ready。保留 `desktop-ready` 的 nonce 校验，校验后依真实状态返回 ready，并附本代 instance_id。
- [ ] 正常顺序是封口 → 已有请求、子任务与审计全部结束 → 停止 HTTP 服务 → 运行现有 shutdown 钩子（IP 活动 flush、上游连接池与 loop watch）→ worker 进程退出。阻塞同步清理移出事件循环。单测令审计写入挂起和 DB 先失败后恢复，再断言探针、计数和退出顺序。用临时 SQLite 从**另一个连接**断言审计/记账记录确实可见且仅一条。

## 任务 3　让进程所有者能发起、观察并完成同一次排空

**文件**　新增 `admin/managed_worker.py`、`admin/lifecycle_control.py`。修改 `desktop/app.py:backend`、`admin/routers/releases.py`、`admin/routers/maintenance.py:restart_service`。任务 6 将同一运行器接入 `main.py`。持久回归 `tests/test_lifecycle_control.py`。

- [ ] 用真实子进程和临时目录先测一条空闲退出链路。worker 记录进入和离开 ASGI shutdown 的两个事件，父进程读取真实退出码。专门令一个 shutdown 钩子等测试闩，断言界面仍为 stopping，父进程没有启动下一代。
- [ ] 控制通道采用每代一个**仅本机可用**的标准库 IPC 端点。可用 `multiprocessing.connection.Listener/Client`，Windows 用 `AF_PIPE`，Unix 用私有运行目录内的 `AF_UNIX`。通信使用有大小上限的 `send_bytes/recv_bytes` JSON，校验消息种类、字段、代号及随机 authkey。父进程为每次启动准备地址和密钥，传给自己的孩子，保留 Popen 句柄。worker 在接受业务前建立端点；从控制线程使用 `loop.call_soon_threadsafe` 投递到真实事件循环，I/O 和等待均不占用 GUI/ASGI 主线程。密钥仅存在进程环境或受当前服务账户保护的运行目录，禁止进入浏览器、HTTP 请求参数和日志。私有目录按部署/端口区分，Unix 权限 0700/0600，Windows 使用当前用户受限目录和 ACL。
- [ ] 定义小协议 `STATUS` 读取快照，`BEGIN(action, operation_id, instance_id)` 关闭准入，`FORCE(operation_id, instance_id, confirm)` 手动升级，`RESUME(operation_id, instance_id)` 只供尚未提交停机的部署预排空使用。BEGIN 对同一操作幂等。worker 的 HTTP 重启处理器直接调用同一 begin 逻辑；父进程根据 worker 的受理回执或退出码识别 Web 发起的重启。本地 job 和 HTTP 的计数只由 worker 写入。控制连接和 HTTP 状态查询都不计数。
- [ ] `admin/managed_worker.py` 显式持有 `uvicorn.Server(uvicorn.Config(...))`。封口后任务等待 `wait_empty()`，归零才在 server 所属事件循环设置 `server.should_exit = True`。保留 host、port、loop、h11、proxy_headers、WebSocket、日志等各入口原设置。若显式接管 OS 信号，要对实际安装的 Uvicorn 验证信号钩子，首次 SIGTERM/SIGINT 只投递正常停止；重复回调仍幂等。外部管理器发 SIGKILL 或系统断电不可能由应用拦下。不要让 Uvicorn 或父进程的秒数定时器早于用户的手动决定。
- [ ] 定义明确的退出结果，例如正常停止 `0`、受理的计划重启 `75`，其余异常沿用错误码；父进程持有的 stop/quit 意图优先于自动重生。先完成原 restart HTTP 响应发送和关闭钩子，再返回计划退出码。可通过响应发送完成信号与 Uvicorn 正常等待当前 ASGI task 实现；请求方若断线，在 finally 解除应答等待，客户端从状态探针查询结果。删除旧路由固定延迟和 `os._exit(0)`。没有拉起方时，必须在封口前返回 409。
- [ ] 默认操作可以等待无穷久，每次 IPC 连接和读取自身有小超时。超时只意味着暂时观测不到，保留进程句柄、操作和快照。手动强退须二次确认，并绑定**当前代和当前操作**，先记录操作与剩余数量，再向本代 worker 请求取消；无应答时仅所有者能够结束自己持有的进程树。Windows 包装进程可能先退出，要持有真实服务进程的身份/句柄或专属 Job Object，验证端口真正释放。常规路径不可先 terminate 再靠失败触发清树。只有退出已落实才清空 `process`、`port` 和本代记录。
- [ ] 验收同一重启双击仅一个 operation_id，前一代迟来的 force 无效，不具备能力时旧进程仍接单。另测“状态接口短暂失联后恢复”和“请求 202 已发送但客户端没收到”，均不得二次重启。

**跨进程冲突的裁决**　对活 worker，BEGIN 的权威结果始终是该 worker 在锁内返回的快照。所有者先把自己的意图标为待受理，收到 BEGIN 成功才承诺后续重启或安装。若 worker 中的 Web 操作已经胜出，所有者采用回执中的动作并撤销自己未获准的安装/停止。Web 强退确认后尽快发回受理结果，随后处理取消并返回计划重启码；父进程永远只重生一次。对尚未启动的 worker，所有者持有本地唯一的待处理意图。补一个“托盘安装与 Web 重启同时点”的回归，断言只有获准的一方能启动新进程或安装器。

## 任务 4　桌面启动、窗口、托盘和更新共用一个控制器

**文件**　`desktop/app.py` 的 `DesktopAPI`、`Bridge`、`SPLASH`、`run`，`desktop/tray.py`，核对 `desktop_entry.py`、`desktop/apply-update.ps1`。持久回归 `tests/test_desktop_lifecycle.py`，用 fake Popen、window、tray 和事件闩。

- [ ] 将 `start/retry_start/restart/stop_services/quit/apply_update` 送到同一个小型串行控制器。锁只保护状态、代号、句柄替换；网络、子进程等待、JS 调用均在锁外。分开“用户想退出”和“现在可以销毁窗口”，不要在 drain 开始时就置现有 `_quitting` 或 `closing` 导致 GUI 放行关闭。每次 Popen 前登记代号，句柄发布后重读待处理意图。禁止启动锁先释放、再回调 `start()` 造成竞争间隙。
- [ ] `_start_worker` 删除对已在运行实例的直接终止。正在启动的同代重复调用只返回状态，ready 返回已有工作台。冷启动过程中先点停或退出，记下目标，最早可联系孩子时执行 BEGIN；就绪回调即便稍后到达，也不能导航、保存旧端口或再起进程。等不到就绪时继续显示阶段与诊断；重试探测一个仍活的孩子，确认孩子退出后才允许新建。父进程异常消失时的 `parent_watchdog` 单列为灾难恢复，正常路径要在孩子关闭完之前保持父进程存活。
- [ ] `open_window`、`open_console` 和 splash 复用同一就绪判断。检查本代 nonce、instance_id 和当前阶段，再保存端口、导航或打开外部浏览器。端口已被另一实例接管，即使 TCP 通或版本号相同也不能传登录信息。保留现有桌面 URL 检查和 session 交换；控制密钥不用作登录凭据。第二次打开/恢复窗口也绑定与首窗口一致的 closing/closed 处理。
- [ ] 桌面有两种载体。冷启动、已停止、启动失败时用自包含的 `SPLASH`，不依赖后端静态文件；已经进入工作台且仍有请求时，在原页面上通过桥显示非破坏性的进度层。此时调用 `load_html`、`load_url`、`destroy` 都可能中断由当前 WebView 发起的 Agent/SSE，请等旧工作完成。后台意外断开时进入可诊断状态，允许按实际 PID 状态重试。正常停服务后再切回本地壳；重启先保留壳，再等待新代 nonce。继续使用现有诊断复制/导出能力。
- [ ] 托盘模式点 × 仍为隐藏；托盘“退出”才开始排空，必要时显示或唤回进度窗口。无托盘模式在 `window.events.closing` 拦截第一次 ×，排队 quit 并返回 False；确认 backend 退出后设置允许销毁并只 destroy 一次。处理 `--smoke-test`、托盘导入失败回退和 `run().finally` 重入。删除或严格后移 3 秒 GUI 强退兜底，避免其在任何业务/持久化仍运行时触发。托盘增加 draining、stopping、failed 状态，显示“查看退出进度”；原启动按钮按阶段禁用。手动强退确认需显示最近已知的数量或“状态未知”。
- [ ] 更新保留下载校验和可写性预检。成功后登记 `action=update`，排空、等后端实退，再启动现有 PowerShell updater，传正确的 bootloader PID，最后关宿主。下载或预检失败时继续服务；排空后 updater 启动失败时保留窗口、旧 EXE 和已验证的包，显示“重试安装 / 启动旧版”。同一操作至多启动一次安装脚本，复用已有 `.previous` 恢复逻辑。
- [ ] 可运行的界面文案示例：`服务正在停止 · 剩余 1 个请求`，次行 `已等待 42 秒。完成后会自动关闭。`，按钮 `查看诊断`、`强制结束…`。打开强退确认框时默认焦点为“继续等待”。只更新计数文本，键盘焦点和可见选择不被每秒轮询抢走。
- [ ] 运行 `python -m unittest discover -s tests -p test_desktop_lifecycle.py -v`。断言连续 start 只 Popen 一次，慢启动期间 quit 没有孤儿，托盘 × 对 backend 调用数为零，关闭/更新事件顺序为“关准入 → 原请求尾帧 → 后端实退 → 安装器（如需要）→ 销毁窗口”。在 Windows 用真实 WebView 再验任务 8。

## 任务 5　浏览器等待壳、受保护的 Web 重启与重连

**文件**　`admin/console/index.html`、新增 `admin/console/lifecycle.js`、`admin/console/console.css`、`admin/console/console-app.js`、`admin/console/review-improvements.js`、`admin/console/desktop.js`。只读参考 `boot-desktop.js`、`boot-guard.js`、`secure-fetch.js`、`admin/static/login.html`。持久回归可用原生 Node `tests/lifecycle-shell.test.cjs`。

- [ ] 在完整控制台首屏放一个有 `role="status"` 的状态区，脚本从 `/admin/assets/lifecycle.js` 加载，沿用服务端资源哈希。给既有 `localBoot`、`initializeNative`、普通浏览器启动探针和旧的 8 秒 enterApp 看门狗接入同一状态判定。保留 `secure-fetch.js` 最先加载、无内联脚本的约定以及 `#wb-page-end`。原生桥只用于本地能力，普通浏览器没有桥也能进入等待、恢复或登录。
- [ ] 重启确认仍用已有按钮/API。发起前记录旧 instance_id，禁双击。202 后在当前 DOM 显示等待层，停止新面板刷新/观察订阅，已经进行的模型流和写请求继续。收到明确的 `service_draining` 时，其他已打开的标签页也应进入等待；空闲标签通过轻量、限频的探针或可见时检查发现状态。普通业务 503、429 和单接口错误留在原面板，不能一律视作重启。HTTP 返回丢失只查 GET 状态，不自动重发 POST、导入、扣费或重启。
- [ ] 检查 `console-app.js:_apiRaw` 的本地 401 自动换票并重发分支。给生命周期变更请求显式禁用此类自动重发，先重新查状态再由用户决定。其它调用方维持现有鉴权行为。会话过期遇到“旧代不明”时，服务可达后先走原登录/换票流程再取新快照。
- [ ] 用完成后调度的 `setTimeout`、AbortController 和单飞标记代替固定 8 秒 reload；建议离线退避 1、2、4、最多 8 秒并加少量抖动，重新可见或手动点“重试连接”立即探测。丢弃旧轮次响应；pagehide 清定时器和探针，但不取消其它业务 fetch。空闲不需要每秒打鉴权接口。探测用 `cache:"no-store"`，不经过批量面板加载。

```js
// 处理已受理的 restart。status 是通过鉴权的 lifecycle 响应。
const recovered = status.phase === "ready"
  && status.instance_id !== restartFromInstance;
if (recovered) restoreConsoleOnce(status.instance_id);
```

- [ ] 相同 instance_id、仅版本号相同或旧实例短暂 200 都不能结束“重启中”。新代 ready 后重新核验现有会话，恢复一次当前 tab、平台筛选和可安全保留的表单。优先原位恢复并重连只读订阅；确有前端资源换版需导航时，保留非敏感页面选择，有未提交输入则提示再刷新。会话真的 401 时进入既有认证路径；403 按接口原有 CSRF/权限语义处理；暂时断网保留壳与草稿。跨端口时 cookie、同源和 bridge 权限按原规则重新核验。外部浏览器无法发现改到哪一端口时，提示从托盘重新打开，禁止猜端口或带凭据跨源探测。
- [ ] `can_restart=false` 的生产/无人监督模式只显示状态。管理台内的“强制结束本次重启…”按钮依同一快照显示，提交前再确认操作号和剩余数。普通浏览器没有后端且 IPC 不可达时只能重连；本机托盘/命令行是失联时的强退入口。Android 继续使用原生 `WBAndroid` 启动链；共享 JS 遇到不可用能力要降级，严禁 HTTP 重启去结束宿主 Android 进程。
- [ ] 用 Node 测试里的 fake fetch、时钟、spy navigation 重放 `旧代 ready → draining → 连续断网 → 旧代迟到 200 → 新代 ready`。断言重启 POST 一次、恢复一次、无中途 reload，输入内容仍在。另测 403、真实 session 401、普通模型 503、离线再上线、休眠标签恢复、同版本新代和缺少桥。

## 任务 6　收口命令行、Windows 服务脚本和监督器

**文件**　`main.py` 的 `_build_admin_cmd`、`_ensure_port_free`、`_kill_children`、`_launch`、信号处理及重生循环，`scripts/daemon.ps1`、`start-service.bat`、`stop-service.bat`、`scripts/watchdog.sh`、`scripts/healthcheck.sh`。测试归入 `tests/test_lifecycle_control.py`。

- [ ] 让 `main.py` 通过任务 3 的运行器启动单 worker，保留参数解析、重定向和后台日志。登记所有者 PID、启动身份、工作目录、端口、受保护 IPC 地址和本代信息。登记文件用临时同目录文件加原子替换，在死亡/切代时按身份清理。检查依赖旧 `uvicorn admin.server:app` 命令行片段的识别代码和脚本，支持新旧形态，避免进程名变化被当外部服务。
- [ ] 两次普通启动瞄准同一部署、端口及活的所有者时，第二次显示“既有实例正在运行/启动/排空”和连接信息后返回。合法的旧孤儿如果支持控制协议，可请求其正常排空，确认释放端口再接管。旧版本无协议或归属不清时，普通启动保留原实例并说明如何手动处置。显式 `--restart` 对本项目走正常排空；保留旧 CLI 选项解析和既有的外部端口冲突诊断，手动中断需明确确认目标，禁止根据“监听此端口”推导进程归属。
- [ ] 区分“重启 worker”和“停止整个所有者”。后者先记录停机意图、禁止监督器拉起，再请求 worker drain。为 `main.py` 增加本地 `--stop` 查询并控制运行中的监督器；复用同一 IPC 实现，监督器自己的控制地址从启动到退出保持稳定（包括孩子尚未创建、准备重生的空档）。信号处理器只设置意图，主循环执行关闭；清理只由一个地方承担。计划重启码跳过崩溃退避；意外退出继续原有退避。CLI 等待时输出剩余数量和操作号，手动强退命令绑定当前操作并需显式确认。
- [ ] PowerShell `-Stop` 先停止**这一部署**的守护拉起，再请求监督器停止并等待完成；守护器也要校验身份和工作目录。删除 BAT 对 8790 的自动 taskkill 回退及 PowerShell 枚举其它 `python main.py` 的处理，向上返回真实退出码。`-Once` 与循环探活按本项目的所有者状态区分“慢启动／主动排空／真正消失”。在本测试账号开两个临时目录和端口，停 A 后 B 的长连接、监听与 PID 均不变。
- [ ] Linux 双路径分开验。宝塔 watchdog 的一次 `GET /admin` 失败会执行 kill 并短等后强杀；识别持有者的 starting/draining/stopping 意图并跳过破坏性恢复。systemd healthcheck 继续使用独立 liveness，主动排空不能累计故障。瞬时超时或旧版无可验证状态不能自动解释为“安全强杀”。保留确实退出后的拉起及连续故障诊断。外部运行环境的最终截止时间放在任务 7 的发布说明里。

## 任务 7　Web 冷打开和已有升级代理

**文件**　`deploy/nginx-prod.conf`、`deploy/upgrade-nginx.conf`、`deploy/upgrade-access.lua`、新增 `deploy/waiting/index.html` 和 `deploy/waiting/reconnect.js`，`scripts/upgrade_control.py`、`scripts/upgrade_gate.py`、`deploy/UPGRADES.md`。核对 `Dockerfile`、`deploy/Dockerfile.prod` 和 `docker-compose.yml` 的实际入口。

- [ ] 在保持原管理台限流、代理头和权限的前提下，为 **GET/HEAD 的精确 `/admin`、`/admin/` 文档导航**设置上游 502/503/504 的中性等待页。页和小脚本由 nginx 的本地静态目录供应，页面返回 503 并带 `Retry-After`、`Cache-Control: no-store`；JS 自身必须回 200、正确 MIME。可用精确 location 加 `proxy_intercept_errors on`、`error_page 502 503 504 =503 /__waiting/index.html`，HTML 的内部 location 用 `internal`，只放行脚本的精确静态路径。保留安全头、CSP、原有访问限制；不要对 `/api`、`/v1`、`/mcp` 或控制台 assets 套 HTML 错误页。更新代理配置中现有的 catch-all 示例也要验证这两个精确入口。
- [ ] 等待页只显示“服务暂时不可用，正在重新连接”及重试按钮，不含管理台片段或上次用户数据。脚本探测同源 ready，验证 JSON 和布尔值后才重新导航回原管理页，保留安全的 tab/hash 选择。代理升级标记有效时，轻量探针快速返回未就绪，避免把探针本身排进 180 秒队列。首次访问时从未加载过页面且浏览器直连未监听的端口，浏览器只能显示连接错误；桌面靠内置 splash，有代理的 Web 靠上述静态页，已打开的直连 Web 靠任务 5。
- [ ] 保留原升级代理的流式等待心跳、队列上限、原始状态/请求体转发与一次转发语义。把 `UpgradeGate.backend_ready()` 与 CLI 的 end 改为判断真实 readiness。保留宿主健康检查的存活语义。`drain` 不再靠日志表 `active_count==0` 判定安全切换。先开已有升级标记，再给**旧代**发 `BEGIN(action="upgrade_hold")`。这个特殊动作封口并等待归零，达到零时保持旧 worker 可查询，只有发布方明确提交停止才退出。记录旧 instance_id 和 operation_id。提交应是同一操作从 hold 升级为 stop，校验操作号、归零和标记有效期后一次性执行；正常 stop/restart 不需要此二阶段。
- [ ] 升级标记有最长 180 秒的旧约束。部署预算耗尽时放弃本次**发布**，调用同代 `RESUME`、确认旧代 ready，再结束标记，让排队请求回到旧代。给未提交的 hold 设置和标记一致的租期；发布 CLI 崩溃或 marker 自行过期时，旧 worker 在核实仍是同一未提交 hold 后自动恢复准入。提交后禁用这条回退，失败走“恢复旧代码 → 验证 ready → 解除等候”。正常交互退出没有这个租期。验证 marker 过期、端口直连、发布中断、恢复调用重试、旧操作迟到，不出现假空闲、永久封口或重复转发。
- [ ] 普通 Docker/生产 Docker 的默认 CMD 和 Compose `command` 目前直接启动 Uvicorn。如要让上述部署控制生效，三个入口统一指向单 worker 的 managed runner，保留各自绑定地址、代理头策略和生产最小镜像。standalone 模式由 runner 创建私有控制目录，CLI 在容器内或经仅服务账户可读的共享运行目录访问 IPC；生产公网不增加写接口。状态报告 `can_restart=false`，由平台执行进程替换。多 worker 部署若没有“对每个 worker 封口并收齐回执”的实现，部署工具必须明确报告不支持本次自动切换，不能把一个进程的 0 当作全站的 0。
- [ ] 在 `deploy/UPGRADES.md` 写清 `begin → drain/hold → 提交并停旧代 → 新代 ready → end` 和超预算撤销。Compose 的 5 秒、systemd 的 TimeoutStopSec 都是外部硬截止。计划发布必须在发出不可撤销的外部 stop 之前完成预排空；单纯把 5 秒改大无法保证任意时长请求。旧镜像首次迁移缺少 IPC/ready 时，工具明确报告旧版能力，只允许在外部关闭所有业务入口、确认旧实例空闲的维护窗口切换，不能把探针 404 当作就绪。验收用临时容器/代理，检查 nginx 配置与实际 HTTP 响应。

## 任务 8　端到端验收、故障演练与交付证据

**合成夹具**　建一个临时 ASGI 测试服务，真实 Uvicorn 子进程负责网络收发。流响应依次吐首帧、等待闩、吐尾帧，再在 finally 等独立的“审计完成”闩。通过测试持有的本机 IPC 释放闩。使用事件/期限等待条件；用虚拟时钟越过旧 1.5/3/5/8/12/90 秒阈值，不要求每个测试真实睡满。浏览器到本机的 SSE 一定用真实流式读取，内存 TestClient 可能把整个响应攒完才返回。

| 场景 | 操作 | 必须观察到 |
|---|---|---|
| 空闲退出 | 服务 ready，点退出一次 | 无人为固定延迟；依次经过 shutdown、进程退出、销毁窗口，端口释放。 |
| 同一 WebView 发起的流 | 发 Agent 流，收到首帧后托盘重启 | 原页面留在原处，收到真实尾帧。旧代 503 拒绝新业务，原工作记账恰好一次，新代 ready 后才恢复。 |
| 两个独立 API 客户端 | A 先做长 SSE，B 在排空后再发 | A 完整收到协议终止事件；B 在 dispatch/扣费前收到可重试拒绝。检查上游调用数和数据库记录。 |
| 非流式与上传 | 暂停一个非流式调用及一个已准入的慢上传，点击停止 | 两条请求都保留。一个完成后计数从 2 到 1，两者均已落库后才归零。 |
| 尾帧后审计仍慢 | 放行尾帧、锁住审计写入 | 客户端可以先读完。旧进程仍在，停止未完成；松开审计后成功一次。 |
| 手动强退与取消确认 | 长流不释放，快进超时，先关闭确认框再主动确认 | 等待期间旧 PID 一直活，自动终止次数为零。确认后才有一次明确中断，留下原因和剩余数量，不伪造成功尾帧；旧操作不能结束新 PID。 |
| 客户端断线 | 排空时客户端自行断开或上游报错 | 原取消/失败语义生效，清理执行一次，计数归零，服务不永久等待已断开的请求。 |
| 后台任务和观察流 | 一个 Job 返回 202 后仍工作，同时打开日志实时页 | Job 保持 active_jobs=1，完成后释放；日志订阅单独结束，读状态不使计数增长。 |
| 启动竞争 | 连点启动/重试；在 Popen 发布前点退出；端口上放另一个假 200 服务 | 每代仅一棵进程树。旧回调不写端口、不导航，假 200 收不到登录信息。 |
| 两种关窗模式与更新 | 托盘 ×、无托盘 ×、长请求中更新，各做一次 | 托盘 × 收起后仍可调用；无托盘 × 留壳等排空；更新器只在后端实退后启动一次。 |
| Web 慢重启/未登录 | 断开 20 秒，先返回旧代 200，再返回新代；另开无 Cookie 窗口 | 已开页面保留状态与输入，鉴权后仅恢复一次；冷打开只见中性页，服务恢复后匿名仍只见登录页。 |
| 双实例与外部超时 | 两个测试端口；对 A 跑停服、发布超预算和撤销 | B 继续出流。A 超预算时旧代继续完成请求、可恢复接单；整个流程没有自动强杀。 |

**执行命令**　下列新增测试文件均为实施交付项。先在临时数据目录通过定向测试，再运行仓库已有的相关回归。Python 依赖采用项目环境，桌面打包按 `desktop/README.md` 与锁定依赖。Node 用内置测试运行器。

```powershell
python -m unittest discover -s tests -p test_graceful_lifecycle.py -v
python -m unittest discover -s tests -p test_lifecycle_control.py -v
python -m unittest discover -s tests -p test_desktop_lifecycle.py -v
node --test tests/lifecycle-shell.test.cjs
node --check admin/console/lifecycle.js
node --check admin/console/review-improvements.js
python tests/test_keepalive_frames.py
```

- [ ] 真实 Windows 冒烟，在临时目录设置 `WORKBUDDY_DESKTOP_DATA`，使用 `scripts/build_desktop.py --work-dir <临时构建目录> --dist-dir <临时产物目录>` 构建。对实际生成的 EXE 执行 `--smoke-test <临时结果.json>`，检查结果 `ok`、端口和窗口标题。再用正常托盘及 `--no-tray` 各测一次长流，其窗口行为与 smoke 不同。
- [ ] Web 真实浏览器测已登录/匿名两窗口，检查首次等待页、静态脚本、手动重试、401/403、GET 探针、键盘可操作、长等待文本可见、网络面板无重复 POST。反代环境执行 `nginx -t` 后在隔离站点验证 `/admin` 可显示等待页，`/v1` 与 `/api` 的错误仍是正确协议与状态。
- [ ] 给出一条脱敏时间线：接收首帧 → BEGIN 回执 → 新请求被拒 → 最后一帧 → 账本/审计可见 → shutdown 完成 → 旧 PID 退出 → 新 instance_id ready → UI 恢复。分别标出旧/新实例和操作号。强退演练记录确认时刻与未完成数量。
- [ ] 回滚按整条控制协议处理：先停发新操作，等已受理的操作完成或撤销对应的部署 hold，再回滚配套的父进程、worker、Web 静态资源与代理配置。数据库无需引入业务 schema 迁移。打包只收产品文件；临时库、进程、socket、日志和构建目录由创建它们的测试清理。
- [ ] 最终报告分别列出已通过的自动化、真实 Windows EXE、真实浏览器、带反代部署的检查。只有实际观察到的路径列为通过；缺少平台的检查留下具体复现命令和待观察现象。
