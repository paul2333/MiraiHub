# 计划升级：等待页、升级闸门与 managed runner

本文件是任务 7 的发布说明与操作手册，回答四件事：

1. 为什么升级窗口内新请求会被排队、旧请求不会被截断；
2. 「scripts/upgrade_control.py」的「begin → drain/hold → 提交并停旧代 → 新代 ready → end」怎么跑；
3. 超预算怎么撤销、CLI 崩溃或标记过期后旧代怎么自动恢复准入；
4. Docker/Compose/systemd 这些**外部硬截止**为什么不能替代预排空。

## 0. 结构：三段各管什么

| 组件 | 位置 | 职责 |
|---|---|---|
| 升级标记 | /run/wb2api-upgrade/active（epoch 秒，最大 180 秒） | 有效期内 nginx/lua 把新请求转到等待代理；过期自动失效，不会永久封口 |
| 等待代理 | scripts/upgrade_gate.py（127.0.0.1:8791） | 排队 + 3 秒 SSE 心跳 + 队列上限 + 原始状态/请求体转发；**一次转发**语义 |
| 发布 CLI | scripts/upgrade_control.py | 写标记、驱动旧代 upgrade_hold、等归零、提交停止、判新代 readiness、撤销 |
| 单 worker 运行器 | admin/managed_worker.py（python -m admin.managed_worker） | 封口 → 等 wait_empty() 归零 → 关监听 → 跑 shutdown 钩子 → 退出码 0/75 |
| nginx 等待页 | deploy/waiting/ + 精确 location | 冷打开 /admin 时上游 502/503/504 → 503 中性页 + Retry-After + no-store |

/api/app/ready 是唯一的 readiness 判据（200 + {"ready": true}）；/api/app/version 只保留
**存活**语义（systemd/容器 HEALTHCHECK 继续用它，主动排空不算故障）。状态接口报告
can_restart=false：容器/平台部署没有重启所有者，进程替换由平台执行，公网不新增任何写接口。

## 1. 部署前：把等待页与代理配好

### 1.1 严格模板（deploy/nginx-prod.conf）

已经内置等待页：只有 **GET/HEAD 的精确 /admin、/admin/** 才在上游 502/503/504 时
error_page 502 503 504 =503 /__waiting/index.html。/api、/v1、/mcp、/admin/assets/*
一律不套 HTML，错误仍是原协议与状态。

1. 拷贝等待页静态文件（<waiting-dir> 按站点实际目录替换）：

        install -D -m 0644 deploy/waiting/index.html   /etc/nginx/wb2api-waiting/index.html
        install -D -m 0644 deploy/waiting/reconnect.js /etc/nginx/wb2api-waiting/reconnect.js

2. 把 nginx-prod.conf 里的 <waiting-dir> 换掉，再 nginx -t、reload。
3. 校验：HTML 的内部 location 是 internal（直接访问 404）；脚本必须能自己取到。

### 1.2 catch-all 站点（deploy/upgrade-nginx.conf）

宝塔这类已有 location / 的站点用本文件：一个命名 location @wb2api_upgrade_wait +
四个精确 location。要点是**精确匹配优先于前缀匹配**，所以升级窗口内 /admin、
/__waiting/* 不会被 access_by_lua_file 排进 180 秒队列；即使把 lua 提到 server 级，
upgrade-access.lua 也会显式跳过 /__waiting/ 与 /api/app/ready。等待代理对探针直接回
{"ready": false}，不会让探针自己等自己。两个文件**不要同时注入同一个 server 块**
（location 重定义会让 nginx -t 失败）。

### 1.3 手工验证（有 nginx 的主机）

    nginx -t -c /etc/nginx/nginx.conf
    curl -sS -o /dev/null -w '%{http_code} %{content_type}\n' https://HOST/__waiting/reconnect.js
    #   -> 200 application/javascript（必须是 200，否则等待页永远不重连）
    curl -sS -D - -o /dev/null https://HOST/admin
    #   上游停掉时：503 + Retry-After: 3 + Cache-Control: no-store
    curl -sS -D - -o /dev/null https://HOST/api/app/version
    #   上游停掉时保持 502/504，绝不能变 HTML
    curl -sS -D - -o /dev/null https://HOST/api/app/ready
    #   窗口开启时立刻 503 {"ready": false}
    curl -sS -D - -o /dev/null https://HOST/v1/models
    #   JSON 协议与状态不被 HTML 覆盖

等待页只显示“服务暂时不可用，正在重新连接”与重试按钮；reconnect.js 只在
GET /api/app/ready 返回 200 + JSON + ready === true 之后才 location.replace() 回 /admin
（保留安全的 tab/hash，丢弃其它参数）。页面不含管理台片段或上次用户数据。

## 2. 发布流程：begin → drain/hold → 提交停旧代 → 新代 ready → end

先决条件：进程由 python -m admin.managed_worker 启动（单 worker），CLI 能访问同一份
私有控制目录与 authkey（--control-dir / WB_LIFECYCLE_CONTROL_DIR，--authkey-file /
WB_LIFECYCLE_AUTHKEY；密钥不要放命令行）。

    # 1) 开窗口：先写标记（新请求开始排队），再给旧代 BEGIN(action="upgrade_hold")
    python scripts/upgrade_control.py begin --seconds 180 \
        --control-dir /app/run/lifecycle --authkey-file /app/run/lifecycle/authkey
    #    -> 旧代封口 + 等归零；归零后仍可查询（不会自己退出）

    # 2) 排空：只信 worker 锁内返回的 active_requests / active_jobs
    python scripts/upgrade_control.py drain --seconds 90 ...
    #    -> 每 5 秒一行心跳：请求 N、后台任务 M、已等秒数
    #    -> 预算耗尽 = 放弃本次**发布**（见第 3 节），不会强行杀进程

    # 3) 提交停止：同一 operation_id 从 hold 升级为 stop（校验操作号/归零/标记有效期）
    python scripts/upgrade_control.py commit ...
    #    -> 提交后**禁用回退**；平台/容器替换进程（EXIT_PLANNED_RESTART=75 也走这条路）

    # 4) 新代起来后判真实就绪，再结束标记
    python scripts/upgrade_control.py end ...
    #    -> GET /api/app/ready 必须 200 + {"ready": true} 才释放排队请求

begin 记录旧代 instance_id 与 operation_id；drain 期间旧代继续应答 /api/app/version、
/api/app/lifecycle 等查询，但不再接新业务。status 打印等待代理健康、标记/租期、hold 记录
与后端 readiness 的 JSON。begin/drain 之外还有 abort（撤销未提交的 hold）与 status。

为什么不用日志表的 active_count==0：那张表只覆盖推理路径，音频、后台任务、审计收尾
不在里面，用它的 0 判“可以安全切换”会截断正在收尾的工作。

## 3. 超预算撤销与失败恢复

- **预算耗尽**（drain 返回 4）：放弃本次发布 —— 同代 RESUME → 确认旧代 /api/app/ready
  重新 200 ready → 结束标记。排队请求回到旧代继续接单，整个过程中没有任何自动强杀。
- **未提交的 hold 有租期**：租期等于标记到期时刻。发布 CLI 崩溃或标记自行过期时，
  旧 worker 在核实“仍是同一未提交 hold”后自动恢复准入；已提交的 hold 没有这条退路。
  正常交互式退出（桌面/命令行停止）不设置这个租期。
- **提交后失败**：回退禁用，只能走“恢复旧代码 → 验证 /api/app/ready → end”。
  不要恢复旧数据库覆盖新写入。
- **旧操作迟到**：commit 校验 --operation-id 与 hold 记录一致，过期标记或非当前操作
  一律返回 5（冲突），不会重复提交，也不会把别的代的 0 当成本代的 0。
- **端口直连**：绕过代理直连 8790 的客户端看不到等待页，也没有排队保护；
  容器/反代部署请只暴露 nginx，直连端口只给运维使用。

## 4. 外部硬截止：Compose 5 秒、systemd TimeoutStopSec

docker-compose.yml 的 stop_grace_period: 5s 与 systemd 单元的 TimeoutStopSec 是
**平台强加的硬截止**：到点内核就 SIGKILL，应用没有“等用户决定”的机会。所以：

- 预排空（begin + drain）必须在发出**不可撤销的外部 stop** 之前完成；单纯把 5 秒
  改大不能保证任意时长的请求，只会在到点前多等一会；
- 容器部署的“停旧代”由平台执行（can_restart=false），应用只负责在被停止之前把
  在途工作做完；被外部硬截止打断时，客户端应由任务 5 的重连/等待壳解释，
  而不是伪造完成；
- systemd 部署把 TimeoutStopSec 设为“业务最长请求 + 收尾”的上界，并让健康检查
  （scripts/healthcheck.sh，判 /api/app/version）在主动排空时不累计故障，
  避免自愈脚本替用户强杀。

## 5. 旧版能力：首次迁移到本机制时的限制

旧镜像既没有 admin/lifecycle_control（生命周期 IPC），也没有 /api/app/ready。这类部署上：

- begin 仍会开启等待窗口（请求继续排队，最多 180 秒），但会明确打印**旧版能力**，
  并说明自动化封口/排空不可用；
- drain 拒绝证明“全站在途为 0”（没有 IPC 就没有可信计数）；
- end 遇到 /api/app/ready 的 404 会报告旧版能力并保留标记 —— **探针 404 不等于就绪**。
  标记最多 180 秒后自行过期，也可以在确认新实例可用后手动删除标记文件；
- 只允许在**维护窗口**切换：外部关闭所有业务入口、确认旧实例空闲，再停/起进程。
  不要用一个进程的 0、或一个 404 探针，代替“全站在途为 0”。

升级到本机制的顺序：先部署带 managed_worker 与 /api/app/ready 的新镜像（三处入口：
Dockerfile CMD、deploy/Dockerfile.prod、docker-compose.yml 的 command，都指向
python -m admin.managed_worker），把等待页与 nginx 片段装好，再启用“先 begin 再停进程”
的发布流程。

多 worker 部署：本工具明确报告**不支持本次自动切换**（--workers N，N>1 直接拒绝），
一次 drain 只证明一个进程的计数归零，不能把一个进程的 0 当作全站的 0。

## 6. 客户端行为（等待代理侧）

Chat Completions、Responses、Messages 的流式请求在等待期间收到 HTTP 200 + SSE 注释，
每 3 秒一条升级提示；注释不是助手 token、不是完成事件、不计费。后端 ready 后原方法、
URL、头与请求体**只转发一次**，后端事件流在同一条连接上继续。非流式请求（含图片生成、
multipart 编辑）等待真实响应并保留其状态与头。鉴权与额度由真实后端在等待后执行；
SSE 已开始后的拒绝按对应协议的 error 事件返回；POST 转发失败**不重试**。排队上限
128 个并发请求、单请求 64 MiB（>1 MiB 落盘）；取消会释放队列槽与上传体。

## 7. 国内网络构建

Dockerfile 支持两个 build arg，不需要直连 PyPI / Docker Hub：

* BASE_IMAGE — 先把基础镜像经镜像站拉下来再打回 python:3.12-slim，或者直接
  BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim。
* PIP_INDEX_URL — 默认 https://pypi.org/simple；国内可用
  https://pypi.tuna.tsinghua.edu.cn/simple 或阿里云 https://mirrors.aliyun.com/pypi/simple/。
  PIP_EXTRA_ARGS 透传额外参数（例如 --trusted-host）。

    docker pull docker.m.daocloud.io/library/python:3.12-slim
    docker tag docker.m.daocloud.io/library/python:3.12-slim python:3.12-slim
    docker compose --env-file .env.docker build \
      --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple admin
    docker compose --env-file .env.docker up -d admin

管理台“更新与备份”页的 国内网络环境 开关可以生成同样的镜像命令；源码更新仍要
git fetch 到 origin，那一跳自行用代理或镜像。

## 8. 在线恢复备份（sqlite）

POST /api/maintenance/restore 接受之前下载的 SQLite 备份原始请求体。它会校验完整性
与必需表，把当前库保存为同目录的 <db>.pre-restore-<timestamp>，再用 sqlite backup API
恢复；任何失败都回填安全副本。恢复后重启服务，让内存池与磁盘状态对齐。
控制台把它暴露为 恢复备份 按钮。
