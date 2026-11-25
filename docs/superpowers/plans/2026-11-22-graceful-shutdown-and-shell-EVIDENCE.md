# 优雅退出与前端壳 · 任务 8 交付证据

> 口径：**只有本轮真实观察到的才写「通过」**。没执行的一律在对应类别里留复现命令与待观察现象。
> 所有测试都在系统临时目录 + 动态端口 + 临时 SQLite 里跑，不连部署库、不改 .env、不 commit。

- 采集时间：2026-11-22 深夜 – 2026-11-23 01:47（Asia/Hong_Kong）
- **复验 #1：2026-11-23 02:01:17 – 02:09:00（全量复跑：Python 批 02:01:44–02:03:24，控制通道 68 tests 到 02:09；起因见 §5 —— 首次验收时 `admin/console/console-app.js` 正被 web-shell 并行改动，抓到的是过期快照）**
- **最终轮（t10）：2026-11-23 02:17–02:31**：e2e 扩到 **14 条**（新增「伪造代号不能控制进程」3 条，67.577s / OK）、control 68 OK / 396.715s、resume 13 OK / 17.724s、graceful 86 OK、desktop 20 OK、waiting 29 OK、async_sync 16 OK、node 28/28、keepalive 14/14、check_maintenance 10 OK；跨流一致性核对见 §10。
- **复验 #2（t18 落账）：2026-11-23 02:10:22 – 02:11:51**：graceful 86 OK / 9.859s、keepalive 14/14、`import admin.server` exit 0（0 处 Traceback）、e2e 11 OK / 62.255s、node 25/25、desktop 20 OK / 6.123s、deploy waiting 29 OK / 0.130s；控制通道 68 OK / 402.483s（本轮真跑，02:09:40 起）。两次复验之间关键文件 mtime 未变（server.py 23:36、lifecycle.py 00:10、lifecycle_control.py 23:55、managed_worker.py 00:00、console-app.js 01:53、lifecycle.js 23:40、test_e2e_graceful.py 01:51），是同一棵树。
- 环境：Windows 10.0.26200、Python 3.14.7、uvicorn 0.54.0、FastAPI 0.162.2、Starlette 1.10.0、Node v24.19.0
- 仓库 HEAD：a6c1ed7（工作区含 t1–t7 的在途改动，未提交）
- 端到端夹具：tests/test_e2e_graceful.py（持久回归，11 个用例，约 78 秒）

## 0. 结论摘要

| 类别 | 结论 |
|---|---|
| 一、自动化（Python + Node） | 端到端夹具 **14/14** 通过（最终轮 67.577s；新增伪造代号/匿名泄露 3 条）；生命周期 86、控制 68、桌面 20、waiting page 29、keepalive 14/14 全绿；**Node 壳测试 25/25 通过**（02:01 复跑；首次验收时的 2 条失败是过期快照，原文与复现命令保留在 §5 作演练记录） |
| 二、真实 Windows EXE | **未执行**（本机不做打包冒烟）：复现命令与待观察现象见 §3 |
| 三、真实浏览器 | **未执行**（无可用浏览器自动化通道）：见 §4；HTTP 层行为已由 §1 覆盖 |
| 四、带反代部署 | **未执行**（本机无 nginx）：见 §6；nginx 配置的结构性回归 29/29 通过不等于真机验证 |

## 1. 一、已通过的自动化

### 1.1 任务 8 端到端夹具（真实 Uvicorn 子进程 + 本机 IPC + 真流式 SSE）

```powershell
cd MiraiHub
python -m unittest discover -s tests -p test_e2e_graceful.py -v
```

实测输出（EXIT=0，首跑）：

```
Ran 11 tests in 77.582s
OK
```

稳定性复跑（同一命令，修掉一处「空闲实例 1ms 内排空完 → 与进程退出赛跑」的脆测之后）：
`RUN_A_EXIT=0 Ran 11 tests in 74.548s / OK`、`RUN_B_EXIT=0 Ran 11 tests in 74.128s / OK` —— 连续 3 次全绿。

夹具构成（全部在临时目录现生成，不入库）：

- 合成 ASGI 服务，用真的 `admin.lifecycle.LifecycleMiddleware`：`/api/sse` 首帧 → 等 tail 闩 →
  尾帧 → 等 audit 闩 → 写审计行（独立 SQLite 连接）→ 终止帧；被强退则不写假尾帧。
- 子进程用 `python -m admin.managed_worker` 启动（真运行器、真控制端点、真退出码 0/75）。
- 测试侧用真 `admin.lifecycle_control.WorkerOwner` 经本机 IPC（Windows AF_PIPE）发 STATUS/BEGIN/FORCE。
- 闩由测试进程里的 TCP 小服务持有（`WAIT` 阻塞 / `release` 放行），不靠 sleep 猜时序。
- SSE 用 `http.client` + `read1()` 真流式读取，不是内存 TestClient 攒包。

### 1.2 其余定向命令（全部真跑）

| 命令 | 实测输出 | 退出码 |
|---|---|---|
| python -m unittest discover -s tests -p test_graceful_lifecycle.py -v | 首次 86 tests / 12.982s；复验 86 tests / 11.488s — OK | 0 |
| python -m unittest discover -s tests -p test_lifecycle_control.py -v | 首次 68 tests / 398.643s；复验 68 tests / 425.013s — OK | 0 |
| python -m unittest discover -s tests -p test_desktop_lifecycle.py -v | 首次 20 tests / 7.201s；复验 20 tests / 6.623s — OK | 0 |
| python -m unittest discover -s tests -p test_deploy_waiting_page.py -v | 首次 29 tests / 0.248s；复验 29 tests / 0.218s — OK | 0 |
| python tests/test_keepalive_frames.py | 通过 14 / 14（两次一致） | 0 |
| node --check admin/console/lifecycle.js | 无输出 | 0 |
| node --check admin/console/review-improvements.js | 无输出 | 0 |
| node --test tests/lifecycle-shell.test.cjs | 首次 25 tests / 23 pass / 2 fail（见 §5）；**复验 2026-11-23 02:01:17 → 25 tests / 25 pass / 0 fail** | 0 |

## 2. 验收表逐行（本机可执行项）

| 场景 | 操作 | 必须观察到 | 实际观察到（来自 §1.1 的真实运行） |
|---|---|---|---|
| 空闲退出 | ready 后经 IPC 发 BEGIN(restart) | 无人为固定延迟；shutdown → 进程退出 → 端口释放 | BEGIN 后 **~2ms** 快照已是 stopping（断言 <0.75s，旧实现固定 sleep 1.5s 才会红）；退出码 **75**；端口连接被拒。实测拆解：uvicorn 关闭序列 ~1.2s + 解释器收尾 ~0.55s 属平台成本，与本次改动无关 |
| 同一 WebView 发起的流 | 收到首帧后重启 | 原页面收真尾帧；旧代 503 拒新业务；记账恰一次；新代 ready 才恢复 | 首帧→BEGIN(active_requests=1)→新业务 503（retry-after: 3 / cache-control: no-store / code=service_draining）→尾帧送达且 audit 闩仍持有（active_requests 保持 1、进程活）→审计行从**另一个连接**读到且恰好 1 条→退出 75→新一代 ready 且 instance_id 不同 |
| 两个独立 API 客户端 | A 长 SSE，B 在排空后发 | A 完整收尾；B 在 dispatch/扣费前被拒 | A 收到 tail 且审计 1 条；B 503 且计数仍是 1（说明没进 dispatch） |
| 非流式与上传 | 一个慢非流式 + 一个慢上传，点停止 | 两条都保留；计数 2→1→0 后才退出 | 封口快照 active_requests=2；慢调用 200、上传 200 且收到完整 512 KiB；归零后退出 75。**注**：本夹具这两条不写审计行，「落库后才归零」由 SSE/Job 两行覆盖 |
| 尾帧后审计仍慢 | 放行尾帧、锁住审计 | 客户端先读完；旧进程仍在，停止未完成 | 尾帧可读后 audit 闩保持 **6.0s**（跨越旧 1.5/3/5 秒阈值），期间进程存活、active_requests=1、elapsed_ms ≥ 5500；**零次自动终止** |
| 手动强退与取消确认 | 长流不释放，快进 3.2s 后先不确认、再确认 | 等待期旧 PID 一直活；确认后一次明确中断；不伪造成功尾帧 | 3.2s 内进程存活且无处置；confirm=false 被协议层拒绝（invalid_message）；错代 force 回 stale_generation；确认后 can_force=false、重复 force 幂等、退出 75、客户端**没有**收到 tail、审计表为空 |
| 客户端断线 | 排空时客户端断开 | 原取消/失败语义生效，清理一次，计数归零 | 断线后 active_requests→0，退出 75，审计表为空（没写成功记录） |
| 后台任务与观察流 | Job 仍工作 + 打开日志实时页 | Job 保持 active_jobs=1；观察流单独结束；读状态不涨计数 | job 租约期间 active_jobs=1；连续 5 次读快照计数不变；观察流收到 draining 帧后**自己结束**；释放后 job 200 且审计 1 条 |
| 启动竞争 | 同一端口再起一个实例 | 每代一棵进程树；旧实例不受影响 | 第二实例以非 0 退出；原实例仍 ready 200 且 instance_id 不变；正常停止返回 0 |
| Web 慢重启（HTTP 层） | 旧代 200 → 排空 → 新代 | 旧代改口 503 service_draining；新代 ready 且换代号 | 旧代 ready 200 → 503（error.code=service_draining）→退出 75→新代 ready 200 且 instance_id ≠ 旧代。（DOM/输入保留属浏览器类，见 §4） |
| 双实例与外部超时 | 两个测试端口，对 A 停服 | B 继续出流；A 的排空不碰 B | A 排空期间 B 仍 ready 200；A 退出 75 后 B 仍活；B 的流收到尾帧且审计 1 条；B 正常停止 0 |
| 手动强退后旧操作不能结束新 PID | — | 旧操作/代号对新进程无效 | IPC 层已覆盖：错代 force 回 stale_generation；真实 PID 归属属 EXE 类（§3） |
| 两种关窗模式与更新 | 托盘 ×、无托盘 ×、长请求中更新 | 见计划 | **未执行**（EXE/桌面类，见 §3） |
| Web 慢重启/未登录（DOM） | 断开 20s、旧代迟到 200、无 Cookie 窗口 | 页面保留状态与输入；匿名只见登录页 | **未执行**（浏览器类，见 §4）；HTTP 层见上表 |

## 3. 二、真实 Windows EXE（未执行）

本机不做打包冒烟（会产出临时构建目录与 EXE，且需要交互式桌面会话）。复现命令与待观察现象：

```powershell
cd MiraiHub
$tmp = Join-Path $env:TEMP ("wb-exe-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force $tmp | Out-Null
$env:WORKBUDDY_DESKTOP_DATA = Join-Path $tmp "data"
python scripts/build_desktop.py --work-dir (Join-Path $tmp "build") --dist-dir (Join-Path $tmp "dist")
# 1) 冒烟：结果文件必须 ok=true，端口与窗口标题要与实际一致
.dist<产物>.exe --smoke-test (Join-Path $tmp "smoke.json")
Get-Content (Join-Path $tmp "smoke.json")
# 2) 托盘模式与 --no-tray 各测一次长流：发 Agent 流 → 收到首帧 → 托盘「重启」
#    观察：原窗口不刷新、尾帧到达、托盘显示「正在重启 · 剩余 N 个请求」、
#    旧 PID 退出后自动起下一代、工作台恢复；--no-tray 的 × 应保留壳直到排空结束。
```

待观察现象（缺一即不通过）：窗口/托盘阶段文案与实际计数一致；`进程处置次数`在等待期间为 0；
更新流程只在后端**实退**后启动一次安装脚本；关最后一个窗口不会切断该窗口发起的流。

## 4. 三、真实浏览器（未执行）

缺浏览器自动化通道（本会话无可用浏览器驱动）。HTTP 层已覆盖的部分见 §1/§2；DOM 层由
`node --test tests/lifecycle-shell.test.cjs` 用 fake fetch/假时钟覆盖（复验 25/25 通过，见 §5 的事故记录）。

手工复现步骤与待观察现象：

1. 启动正式实例，用**已登录**窗口打开 `/admin`，再开一个**无 Cookie** 匿名窗口。
2. 已登录窗口发一条 Agent 流，收到首帧后点「重启服务」；匿名窗口保持打开。
3. 必须观察到：已登录窗口**不整页刷新**，原位出现 `role=status` 等待层（“正在重启 · 剩余 N 个请求 / 已等待 M 秒”）；
   流继续吐到尾帧；旧代拒绝新面板请求；新代 `instance_id` 变化后**只恢复一次**，输入框内容仍在。
4. 匿名窗口：冷打开应只看到中性登录页（不出现后台片段）；服务恢复后仍是登录页。
5. Network 面板：重启 POST 只发一次；探测请求都是 GET 且 `cache: no-store`；无 8 秒整页 reload。
6. 键盘：等待层出现时 Tab 能到「查看详情」「强制结束…」，强退确认框默认焦点在「继续等待」。

## 5. 曾观察到的失败（Node 壳测试 2 条）与复验记录

> **结论：这 2 条是「验收时文件正在被并行改动」的过期快照，不是实现回归。**
> 首次抓到：2026-11-23 01:38（当时 `console-app.js` mtime 00:21，web-shell 正在改）。
> 复验：2026-11-23 02:01:17 在 web-shell 改完后的文件上重跑 —— `tests 25 / pass 25 / fail 0 / exit 0`；
> `admin/console/*.js` 里「启动探针超时／强制进入控制台」**0 处**；`_isDrainFrame:16368`、
> `pauseLogObservationStreams:16381` 均在，两条观察流的 draining 监听在 `console-app.js:16224` 与 `:16322`。
> 以下原始失败原文与复现命令**保留**，作为「验收时文件在途」的演练记录。

命令与真实输出（EXIT=1）：

```powershell
cd MiraiHub
node --test tests/lifecycle-shell.test.cjs
```

```
ℹ tests 25
ℹ pass 23
ℹ fail 2

✖ console-app.js：8 秒强制 enterApp 看门狗已移除并接入统一状态判定，onclick 内联逻辑不被新增
  AssertionError [ERR_ASSERTION]: 固定 8 秒看门狗必须移除
  true !== false
      at tests/lifecycle-shell.test.cjs:733:10

✖ console-app.js：两条观察流都接 draining 收尾帧；等待层期间不重连，新代 ready 才补开
  AssertionError [ERR_ASSERTION]: 上方请求动态流（sentinel）接收尾帧
      at tests/lifecycle-shell.test.cjs:880:10
```

当时的代码位置（只做核实，未作任何修改；复验时已不存在）：

- `admin/console/console-app.js:18055-18067` 仍保留非本地壳的**固定 8 秒强制 enterApp()** 看门狗
  （第 18064 行 `console.warn("[WB] 启动探针超时，已强制进入控制台")`，第 18067 行 `}, 8000)`）。
  计划任务 5 明确要求删除固定 8 秒看门狗；该文件 mtime 为 00:21（晚于测试文件 23:41），可能是并行改动中的状态。
- `admin/console/console-app.js` 全文没有任何 `draining` 字符串：`addEventListener("draining")`、
  `_isDrainFrame`、`pauseLogObservationStreams`、`resumeLogObservationStreams` 均缺失
  （`draining` 目前只出现在 `admin/console/lifecycle.js`）。即服务端在排空时发的 draining 收尾帧，
  前端两条观察流还没有监听者 → 排空期间仍会重连。

处置与结果：已按流程上报并转 web-shell；对方在 `console-app.js` 完成接线后，本条复验为绿（见本节开头）。
教训：并行成员改同一批文件时，验收结论必须带上**被检文件的 mtime**，否则会把「改到一半」当成「实现回归」。

## 6. 四、带反代部署（未执行）

本机没有 nginx，无法真跑；`test_deploy_waiting_page.py` 只是配置/页面的结构性回归（29/29 通过）。
真机步骤与待观察现象：

```bash
# 隔离站点 + 临时上游（临时端口），先语法检查再起
nginx -t -c /etc/nginx/conf.d/wb-upgrade-test.conf
curl -sS -D- http://127.0.0.1:<PORT>/admin            # 上游 502/503/504 时应是 503 + Retry-After + no-store 的等待页
curl -sS -D- http://127.0.0.1:<PORT>/__waiting/reconnect.js   # 必须 200 + application/javascript
curl -sS -D- -X POST http://127.0.0.1:<PORT>/v1/chat/completions  # 不能套 HTML 错误页，保持 JSON 协议与状态码
curl -sS -D- http://127.0.0.1:<PORT>/api/app/ready     # 排空时快速 503 {"ready": false}，不被排进 180 秒队列
```

待观察现象：`/admin`、`/admin/` 命中等待页且带安全头；`/__waiting/index.html` 不能从外部直取（internal）；
`/api`、`/v1`、`/mcp`、`/admin/assets/*` 的错误仍是原协议；等待页脚本回 200 且 MIME 正确；
手动点「重试」后按白名单 tab/hash 回到原管理页。

## 7. 脱敏时间线（真实运行，来自 §1.1 同一夹具）

实例代号与操作号是夹具生成的演示值（`gen-old-demo` / `gen-new-demo` / `op-489ac2b4`），不含任何真实信息。

| 序号 | 相对 t0 (s) | 事件 | 观察值 |
|---|---|---|---|
| 1 | 0.558 | 旧代 ready | instance=gen-old-demo pid=20576 port=14550 |
| 2 | 0.575 | 客户端收到首帧 | GET /api/sse |
| 3 | 0.577 | **BEGIN 回执** | ok=True action=restart operation_id=op-489ac2b4 active_requests=1 |
| 4 | 0.591 | **新请求被拒** | status=503 retry-after=3 code=service_draining |
| 5 | 0.607 | **尾帧到达客户端** | audit 闩仍持有，active_requests=1 |
| 6 | 1.500 | **审计/记账可见** | 另一个 SQLite 连接读到 rows=['sse-timeline'] |
| 7 | 3.705 | **旧 PID 退出** | exit_code=75 pid=20576（uvicorn 关闭 ~1.2s + 解释器收尾 ~0.55s，产品处置 ~2ms） |
| 8 | 4.309 | **新 instance_id ready** | instance=gen-new-demo（≠ gen-old-demo），{"ready": true} |
| 9 | 7.408 | 新代正常停止 | exit_code=0 |

UI 恢复（第 10 步）分两层取证：**JS 层**由 `tests/lifecycle-shell.test.cjs` 覆盖（复验 25/25 通过：恢复只发生一次、
不整页刷新、输入内容保留、旧代迟到 200 不放行）；**真实浏览器**仍未执行（§4），所以这一行只标「JS 层通过」。

## 8. 观察与遗留（非阻塞）

1. **解释器退出成本 ~3.0s**：`python -c "import admin.config"` 实测 `IMPORT 0.535 / ATEXIT 3.612`——
   导入 admin.config 的进程在退出时会多花约 3.0s。它是既有行为（与本次改动无关），但会直接加在
   「旧 PID 真正退出 → 父进程开始下一代」的观测延迟上（时间线第 7→8 步）。建议 captain 记入后续清理项。
2. **Windows 没有 SIGTERM**：`Popen.terminate()` 是 `TerminateProcess`（退出码 1，等于强杀）。
   因此端到端里的「正常停止 → 0」走同一 IPC `BEGIN(stop)`；真实 Ctrl+C/服务管理器信号路径只能到 EXE 类验证。
3. 虚拟时钟：跨进程无法伪造子进程时钟，本文件用**真实阈值穿越**（1.5/3/5 秒）保证「零次自动终止」；
   UI 的 8 秒/90 秒阈值由任务 5 的 node 假时钟测试覆盖（复验 25/25 通过）。
4. **并行会话同时改本仓库**：另有一个 DSH 会话在同一 working tree 上跑另一份计划，同时在改
   `admin/platform/*`、`admin/routers/models.py`、`admin/model_alias.py`（**都不在本计划范围内**）。
   本文件只对 graceful-shutdown-shell 计划的文件负责；上述文件的变化不纳入本验收的通过/失败判据。
   同理，§5 的过期快照事故也是并行改动的直接后果 —— 复验前一律先看 mtime。

## 9. 复现与清理

```powershell
cd MiraiHub
python -m unittest discover -s tests -p test_e2e_graceful.py -v      # 11 tests / ~75s（连续 3 次 OK）
python -m unittest discover -s tests -p test_graceful_lifecycle.py -v  # 86 tests
python -m unittest discover -s tests -p test_lifecycle_control.py -v   # 68 tests / ~400s
python -m unittest discover -s tests -p test_desktop_lifecycle.py -v   # 20 tests
python -m unittest discover -s tests -p test_deploy_waiting_page.py -v # 29 tests
node --test tests/lifecycle-shell.test.cjs                              # 25 tests / 25 pass（2026-11-23 02:01 复验）
node --check admin/console/lifecycle.js
node --check admin/console/review-improvements.js
python tests/test_keepalive_frames.py                                  # 14/14
```

端到端夹具自己清理：每个用例结束后结束子进程树、删除 `%TEMP%` 下的临时目录（含临时 SQLite 与控制端点目录）；
本轮没有产生需要保留的临时文件，也没有修改任何产品文件。

## 10. 跨流一致性核对（t10 收口）

逐项把 CONTRACT 与实现对齐；「证据」列都是可以自己复跑的命令或文件位置，不是转述。

| 契约条目 | 实现 | 证据（可复跑） | 结论 |
|---|---|---|---|
| §1 快照 9 字段固定、phase 六值、空闲 action/operation 为 null | admin/lifecycle.py _snapshot_locked；desktop SNAPSHOT_FIELDS 与前端 PHASE_LABEL 同名 | e2e test_anonymous_lifecycle_is_401_and_ready_is_minimal（set(snapshot)==9 字段）；test_graceful_lifecycle 快照用例；node 壳测试 | 一致 |
| §1 elapsed_ms 单调、begin 起算 | Lifecycle.begin 用 time.monotonic() | e2e test_no_auto_termination_past_old_thresholds（6s 后 elapsed_ms >= 5500） | 一致 |
| §1 线程完成经 loop.call_soon_threadsafe | Lifecycle._wake | test_graceful_lifecycle test_finish_from_thread_wakes_the_event_loop | 一致 |
| §2 /api/app/ready 匿名、只回布尔、no-store | admin/server.py app_ready | e2e（200 {"ready":true} 且键只有 ready）；匿名 503 分支同用例 | 一致 |
| §2 /api/app/lifecycle 需 require_admin、不泄露 | admin/server.py app_lifecycle | e2e 新增用例：匿名 401 且响应内无 instance_id；带管理员令牌 200 九字段 | 一致（本轮补上的路由级证据） |
| §2 restart 202 + {ok,message,lifecycle}；无 owner 409 + can_restart=false；冲突 409 带快照 | admin/routers/maintenance.py _accepted_response / _conflict_response | 源码核对 + e2e force 409 回执（同一 _conflict_response 产出 lifecycle 快照） | 一致 |
| §2 force 需 confirm+instance_id+operation_id；代号不符 409 | maintenance force_restart -> LocalControl.force | e2e 两条新用例：伪造代号 -> 409 且回执报真实代号；伪造操作号 -> 409 且 can_force 未被消耗、原操作继续完成并记账 | 一致 |
| §2 排空拒绝统一 503 + Retry-After: 3 + service_draining + 三类信封 | admin/lifecycle.py send_draining_response / drain_envelope | test_graceful_lifecycle 信封用例；e2e 中 /api/slow 503（retry-after 3 / no-store / code=service_draining） | 一致 |
| §3 IPC 仅本机、authkey、BEGIN/FORCE 幂等 | admin/lifecycle_control.py（AF_PIPE / 私有目录） | e2e 全程经真 IPC 驱动 STATUS/BEGIN/FORCE；t2 的 68 条控制回归 | 一致 |
| §3 resume 只用于升级预排空撤销 | Lifecycle.resume 只校验「阶段 draining + 操作号对齐」，**不校验 action**；边界由 LocalControl._resume_refusal 回 not_a_hold 拦下 | 源码：lifecycle.py resume；lifecycle_control.py _resume_refusal；maintenance _REASON_TEXT["not_a_hold"] | **偏差已记录**：进程内 API 比契约宽，消费侧边界已收紧；建议后续把 action 校验下沉到 resume（非阻塞） |
| §4 运行器：封口->排空->停服->退出码 0/75 | admin/managed_worker.py；desktop backend() 与 main.py 都调它 | e2e 全部用例断言 exit 75 / 0；t7 的 supervisor 回归 | 一致 |
| §4 无自动强退秒表 | 只有 should_exit 之后的 timeout_graceful_shutdown（desktop 5s，注释说明只作用于归零之后） | e2e 6 秒零自动终止用例；desktop/app.py:257-261 注释 | 一致 |
| §5 恢复条件 phase=="ready" 且 instance_id 换新、只恢复一次、禁固定 8 秒 reload | admin/console/lifecycle.js（restoreOnce / onRestore）；console-app.js _isDrainFrame + 观察流 pause/resume | node 28/28（含 t20 新增 3 条真函数体执行用例） | 一致 |
| §5 env.reload 存在但当前从不调用 | lifecycle.js:857 定义 | grep reload( 只有定义行 | **偏差已记录**：计划里「确有资源换版才导航」是可选项，现状恒为原位恢复（更安全）；需要换版导航时再接入 |
| §6 nginx 只给 GET/HEAD 的 /admin、/admin/ 出等待页；/api、/v1、/mcp 不套 HTML | deploy/nginx-prod.conf、upgrade-nginx.conf、waiting/* | test_deploy_waiting_page 29/29（结构性）；真 nginx 未执行（§6） | 结构一致 / 真机待验 |
| 部署入口统一走 managed runner | Dockerfile CMD、deploy/Dockerfile.prod CMD、docker-compose command、main.py WORKER_MODULE | grep 结果：四处均为 python3 -m admin.managed_worker；无 uvicorn admin.server:app 入口残留 | 一致 |
| liveness / 排空不累计故障 | scripts/healthcheck.sh 用 /api/app/version，503 + service_draining 跳过 | 源码 + 脚本自检 | 一致 |
| 桌面 UI 增加 unreachable 态 | desktop/app.py startup_status | CONTRACT §3 明确允许「控制通道失联返回 unreachable」 | 允许 |
| 桌面 os._exit 残留 | 仅 parent_watchdog（父进程消失）与 late-exit guard（后端已退出后才计时） | desktop/app.py:238-254、891-908 | 符合计划允许的灾难恢复 |

**遗留的旧行为（不属本计划文件，仅记录）**：admin/console/boot-diag.js 仍有 8 秒 setTimeout 与手动 location.reload，它是开发诊断页，不在任务 5 的文件清单内；本计划的等待层与主控制台不受影响。

## 11. 最终验收结论与仍需人工验证的项

| 验收要求 | 结论 |
|---|---|
| 任务 1-7 的自动化回归 | **通过**：graceful 86 / control 68 / resume 13 / desktop 20 / waiting 29 / async_sync 16 / keepalive 14 / check_maintenance 10 |
| 任务 8 验收表（本机可执行行） | **通过**：e2e 14/14，逐行见 §2；本轮新增伪造代号 3 条 |
| 前端壳（JS 层） | **通过**：node 28/28 |
| 跨流契约一致性 | **通过**（1 条偏差已记录：Lifecycle.resume 的 action 校验在消费侧） |
| 真实 Windows EXE 冒烟 / 托盘 / 无托盘 / 更新 | **未执行** —— 命令与待观察现象见 §3 |
| 真实浏览器（登录/匿名双窗口、键盘、Network） | **未执行** —— 步骤见 §4 |
| 带反代部署（nginx -t + 隔离站点） | **未执行** —— 命令见 §6 |

最终 e2e 运行（2026-11-23 02:31）：

```
cd MiraiHub
python -m unittest discover -s tests -p test_e2e_graceful.py -v
Ran 14 tests in 67.577s
OK
```
