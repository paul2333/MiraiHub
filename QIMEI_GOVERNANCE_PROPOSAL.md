# 设备身份（qimei）治理提案

- 目标仓：xiaofan6ya/MiraiHub，基线 origin/main = 8cf3c15
- 性质：改进提案稿（只写稿，不含提交/推送）

## 摘要

1. 当前部署模型下，一个 MiraiHub 实例里所有账号共用同一个 qimei；上游按 qimei 判设备，等于 N 个号挤在 1 台设备上。
2. 一个号触发风控 → 整台设备被标记 → 该部署下全部账号连坐 4100404。我们自己已经踩过一次。
3. 另外 `docs/uskey-reverse-report.md` 里公开了别人的真机 qimei36，照抄的人会和那台真机共设备。三点都影响所有使用者，不是个别配置问题。

## 问题一：一个部署内所有账号共用一个设备身份

证据链：

- `uskey_signer.py:293` 是模块级单例 `_beacon = _Beacon()`，进程内只加载一次 DLL。
- `uskey_signer.py:195-198` 通过 ctypes 调 `beacon_get_uskey(busiid, bus_info, out, size)`，四个参数里没有 guid、没有 device 入参。
- `uskey_signer.py:299-312` per-account 唯一的差异是 bus_info 里的 originOpenID 和随机 nonce。
- `tools/uskey_sign_service.py:62-63` 同样是进程内单例，请求体只有 `{busiid, bus_info}`，没有 qimei 入参。
- `uskey_remote.py:54` 网关默认打 `127.0.0.1:8792/sign`，所有网关共用一个签名器。
- `docs/uskey-wine-deploy.md:130-131` 单 prefix（HOME=/opt/marvis-sign，WINEPREFIX=/opt/marvis-sign/.wine）。
- `uskey_signer.py:108-112` 的 `_DEFAULTS` 只有 agent_tag/scene/model，没有 qimei；`uskey_signer.py:82` 全文件唯一一处 qimei 出现在注释里。也就是说 Python 层没有账号级设备概念，qimei 由 qimei.dll 在 prefix 环境里算，输入是宿主机 MAC、卷序列号这一类东西。

结论：1 个部署 = 1 个 qimei = 1 台设备，账号数量不改变这个事实。上游按设备归因风险，所以风险以部署为单位传导，而不是以账号为单位。

实证：我们自己的部署上 8 个 guid 共 1 个 qimei，触发整设备标记，连真实官方客户端一起 4100404。

## 问题二：文档里公开了别人的真机 qimei36

- `docs/uskey-reverse-report.md:17,87` 明文写着真机 36 位 hex（前 8 位 db4215eb），并标注「（固定）」。

两个后果：

1. 任何照抄这串值的人，设备身份与那台真机相同，那台真机上的官方客户端会被一起连坐。这是把风险转嫁给无关用户。
2. 「固定」与代码事实相反 —— `uskey_signer.py:108-112` 的 `_DEFAULTS` 里没有 qimei，`uskey_signer.py:82` 只在注释里提到它，代码层面本来就不可能写死。文档给出的是一条走不通的路。

建议：立即删除或打码成 `<REDACTED_QIMEI36>`，并在同页加一行加粗警告：qimei 是设备级标识，随运行环境生成，一个部署一份，不可写死、不可跨部署共享。

## 问题三：prefix 可被复制/分发，设备身份跟着走

实测（同一台宿主机）：

- `cp -a` 复制 Wine prefix → qimei 不变
- 只换 MAC → qimei 变
- 同一 MAC 下建两个全新 prefix → 两个不同 qimei

说明 prefix 缓存了足以复现同一 qimei 的状态；MAC/卷序列号是输入之一，prefix 是载体。所以 `docker commit`、备份还原、一键镜像分发都会把设备身份原样带出去，使用者全程无感。

qimei.dll 导入表能看出输入面：GetAdaptersInfo / GetIpNetTable / GetVolumeInformationA / GetSystemFirmwareTable / SetupDiGetDevicePropertyW / CoCreateGuid / CryptGenRandom，落盘在 `C:\ProgramData\Tencent\qimei\`（机器级共享，不随 prefix 隔离）。这也解释了「同 MAC 两个新 prefix 也不同」——初始化阶段有随机成分，落盘后就稳定下来。

Docker 路线更糟：

- `Dockerfile:17-20` 只 COPY admin/desktop/若干 py，没有 COPY vendor、没有装 wine。
- 结果 `_find_dll()` 找不到 DLL → uskey_fail_closed 默认开启 → 503；若 FAIL_CLOSED=0，则 uskey 变成随机值 → 上游 4100404。
- 用户看到的「所有号都被风控」，很可能签名器根本没加载。这两种故障现在从现象上无法区分。

## 问题四：没有轮换与隔离手段

- 文档里搜 轮换 / 隔离 / 共享 / 多账号 / device，零命中。
- git log 里没有设备隔离或轮换相关提交。
- `uskey_cache.py` 的 `_CACHE_TRUST_SECONDS = 0`，每次都现签 —— 这只解决重放，不解决多账号共设备。

结果是：一旦发现设备被标记，使用者手里没有任何受支持的处置动作，只能自己猜。

## 建议的改进

### 1) 文档纠错（最高优先，零风险）

删掉 `docs/uskey-reverse-report.md:17,87` 的真机 qimei36，同页加粗写清「qimei 是设备级标识，一个部署一份，不可写死、不可跨部署共享」。不涉及代码，改完就能减少新的受害者。

### 2) 启动自检

进程启动时做三项检查，任一失败就显式报错退出，不要静默 fail-closed：

- vendor 下 DLL 是否存在、能否 load（对应 `uskey_signer.py:293` 的加载点）
- 首次启动把 prefix 卷序列号 + 当前 qimei 指纹写入 `<prefix>/.deploy-id`
- 后续启动比对：qimei 变了（环境漂移）、或同一 qimei 出现在另一个 deploy-id（克隆）都报错

### 3) 多设备分片

支持配置多个签名后端（多 prefix / 多侧车进程），按账号哈希分配，使单个 qimei 只承载少量账号。我们实测每设备 1-2 个号是安全的。改动面：`uskey_remote.py:54` 从单 URL 改成后端列表；`tools/uskey_sign_service.py:62-63` 从单例改成按 backend 索引的实例表；分配用 openid 哈希取模，保证同一账号稳定落到同一后端。

### 4) 轮换流程

补一份标准步骤文档（换 MAC + 重建 prefix）。我们验证过：换 MAC 后能得到结构合法的 qimei36（36 位 hex），上游返回 200。流程写明：停签名器 → 记录旧 qimei 指纹 → 换 MAC → 建新 prefix（不要 `cp -a`）→ 启动自检 → 单账号验证 → 再放量。旧 prefix 留一份做回滚。

### 5) 观测

把 qimei 打进日志和指标，至少能回答「一个 qimei 挂了多少个 openid」。现在 `status()` 只报 DLL 状态和运行模式，没有设备维度，出事后无法定位。建议输出两个指标：qimei 去敏指纹（前 8 位 + 哈希后缀）、各指纹下的 openid 计数，配一条阈值告警。

### 6) Docker 路线

二选一，别留中间态：补 COPY vendor + 安装 wine，让镜像内自带签名器；或在 `docs/uskey-wine-deploy.md` 明确写「Docker 部署必须外挂签名服务，镜像内不带 qimei」，并在检测到容器环境时拒绝本地签名。

## 最小可行改动清单

按顺序做，前两条当天可完成：

1. `docs/uskey-reverse-report.md:17,87` —— 删除真机 qimei36，替换为 `<REDACTED_QIMEI36>`，同页加粗警告。
2. `docs/uskey-wine-deploy.md` —— 加一节「设备身份」：一部署一份、不可写死、prefix 不可复制分发、换 MAC 会被识别为新设备。
3. 启动自检 —— DLL 存在性 + `<prefix>/.deploy-id` 克隆检测，失败显式报错（`uskey_signer.py:293` 附近）。
4. `status()` —— 增加 qimei 指纹与 openid 计数（`tools/uskey_sign_service.py:62-63`）。
5. 多后端配置 —— `USKEY_BACKENDS` 列表 + 按 openid 哈希取模（`uskey_remote.py:54`）。
6. `Dockerfile:17-20` —— 补 COPY vendor + wine，或在文档声明必须外挂签名服务。
