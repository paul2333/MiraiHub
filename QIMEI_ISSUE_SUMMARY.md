# Issue 摘要：一个部署 = 一台设备（qimei 治理）

> ⚠️ **已过时（文件已删）**：本文引用的 `uskey_signer.py` / `uskey_remote.py` /
> `uskey_cache.py` / `docs/uskey-wine-deploy.md` / `docs/uskey-reverse-report.md` /
> `tools/uskey_sign_service.py` / `deploy/marvis-uskey-sidecar/` / `vendor/beacon/*.dll`
> 均已在 commit `0cd124d`（`chore(marvis): drop the wine-based uskey sidecar`）中删除。
> 文中所有 `文件:行号` 都是**历史行号**，指向 `3e797bc` 及更早的版本，不是当前仓库内容；
> 每处引用行尾的 `<!-- 0cd124d 删除 -->` 就是这个标记。
> 保留本文只为留存问题现象与治理思路；实现现状见 `admin/platform/marvis/core/uskey.py`
> （纯 Python mint，唯一签名路径，无侧车、无 DLL、无 HTTP 回落）。

**建议标题**：所有账号共用同一 qimei，一个号触发风控 → 整部署连坐 4100404

**现象**：某部署下全部账号同时 4100404，换号无效、换部署有效；上游按 qimei 判设备。

**根因**
1. `uskey_signer.py:293` 模块级单例 + `docs/uskey-wine-deploy.md:130-131` 单 prefix ⇒ 1 部署 1 个 qimei；`uskey_signer.py:195-198` 的 ctypes 调用没有 guid/device 入参，账号差异只在 bus_info 的 originOpenID（`uskey_signer.py:299-312`）。  <!-- 0cd124d 删除；行号指向 3e797bc 历史版本 -->
2. `docs/uskey-reverse-report.md:17,87` 公开真机 qimei36 并标「（固定）」：照抄者与那台真机共设备，官方客户端被连坐；而 `uskey_signer.py:108-112` 的 `_DEFAULTS` 无 qimei，代码层面本就不能写死。  <!-- 0cd124d 删除；行号指向 3e797bc 历史版本 -->
3. prefix 可复制：实测 `cp -a` 后 qimei 不变；`Dockerfile:17-20` 未 COPY vendor、未装 wine ⇒ `_find_dll()` 失败 ⇒ 静默 503，或 FAIL_CLOSED=0 时随机 uskey ⇒ 4100404。
4. 无轮换/隔离：文档搜 轮换|隔离|共享|多账号|device 零命中，git log 无相关提交，`uskey_cache.py` 的 `_CACHE_TRUST_SECONDS=0` 仅防重放。  <!-- 0cd124d 删除；行号指向 3e797bc 历史版本 -->

**建议（按优先级）**
1. 删掉文档里的真机 qimei36，加粗「设备级、一部署一份、不可写死」（零风险）。
2. `status()` 输出 qimei 指纹 + 每指纹的 openid 计数。
3. 启动自检：DLL 可用性 + prefix 克隆检测，显式报错替代静默 fail-closed。
4. 多后端分片（每设备 1-2 号实测安全）+ 文档化「换 MAC + 重建 prefix」轮换流程。
