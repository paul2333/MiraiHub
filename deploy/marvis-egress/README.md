# Marvis WARP 出口（wireproxy）

Marvis 上游按「账号 + 设备 + 出口 IP」做风控。同一个号换了出口 IP，会直接回
`4100404 客户端通用错误:请求被风控拦截`，而且错误面只写「请求被风控拦截」——
看不出是出口漂了。所以网关侧（`admin/platform/marvis/core/egress.py`）需要一个
**稳定的出口池**：按 `sha256(account_id)` 把每个账号钉在同一个出口上。

本目录是出口池的**宿主侧实现**：每个出口一条 wireproxy（Cloudflare WARP），
在本机监听一个 HTTP 代理端口，容器经 `host.docker.internal` 访问。

## 拓扑

```text
容器 miraihub-admin
  core/egress.pick(account_id) → http://host.docker.internal:42001   ┐
                                                                     │
宿主 systemd                                                          │
  marvis-egress@w1.service  wireproxy  [HTTP] 0.0.0.0:42001  ←────────┘
  marvis-egress@w4.service  wireproxy  [HTTP] 0.0.0.0:42004
        │
        └── WARP 隧道（wgcf 注册的账号，端点 188.114.98.224 / 162.159.192.1）
```

出口与端口、端点的对应表写在 `refresh_exits.sh` 的 `endpoint_for` / `port_for`
里，新增出口要三处同步：**本脚本、`docker-compose.yml` 的
`MARVIS_EGRESS_POOL`、`install.sh` 的 enable 列表**。

## 安装

1. 放二进制（不进 git）：

   ```bash
   install -d /opt/marvis-egress/bin
   # wireproxy v1.1.3: https://github.com/pufferffish/wireproxy/releases
   # wgcf:             https://github.com/ViRb3/wgcf/releases
   install -m 0755 wireproxy /opt/marvis-egress/bin/
   install -m 0755 wgcf      /opt/marvis-egress/bin/
   ```

2. 建出口（首次，会注册两个 WARP 账号）：

   ```bash
   cd /opt/marvis-egress
   python3 mk_exit.py 41001 42001 w1
   python3 mk_exit.py 41004 42004 w4
   ```

3. 装 systemd 单元并启用：

   ```bash
   sudo ./install.sh          # 从仓库拷单元、enable 各出口 + 刷新定时器
   sudo ./install.sh w1 w4    # 或临时指定出口集合
   ```

4. 网关侧（compose 已内置默认值，`.env` 可覆盖）：

   ```yaml
   extra_hosts:
     - "host.docker.internal:host-gateway"
   environment:
     MARVIS_EGRESS_POOL: "http://host.docker.internal:42001,http://host.docker.internal:42004"
   ```

   `MARVIS_EGRESS_POOL` 接受 JSON 数组或逗号串；`MARVIS_EGRESS_POOL_FILE`
   指 JSON 文件也行。**池空 = 直连 = 出口 IP 漂移 = 4100404**，所以 compose
   把它写成带默认值的必填项，而不是让运维记得手加。

## 换 IP（每日轮换）

`marvis-egress-refresh.timer` 每天 04:37（±10min）跑一次 `refresh_exits.sh`：
重新 `wgcf register`（新私钥必然换 IP）→ 重建 `wireproxy.conf` →
`systemctl restart marvis-egress@<exit>`。**不要**用「换 endpoint」代替：
换端点会把多个出口挤到同一个 colo，IP 反而撞在一起。

手动跑：

```bash
sudo systemctl start marvis-egress-refresh.service
sudo tail -n 20 /opt/marvis-egress/logs/refresh.log
```

## 自检

```bash
# 宿主：每个出口当前 IP（应该互不相同）
curl -s -x http://127.0.0.1:42001 https://api.ipify.org; echo
curl -s -x http://127.0.0.1:42004 https://api.ipify.org; echo

# 容器：池子是否真的加载（应为 "出口池已加载：2 个出口"）
docker logs miraihub-admin-1 2>&1 | grep -E '出口池已加载|出口池为空'

# 容器：某个账号会被钉到哪个出口（和网关同一份逻辑）
docker exec miraihub-admin-1 python -c \
  "from admin.platform.marvis.core import egress; print(egress.exits(), egress.pick(192))"
```

看到 `出口池为空` 就说明 `MARVIS_EGRESS_POOL` 没进容器（`.env` 或 compose
被覆盖掉了），此时所有 marvis 请求都会走宿主直连出口 —— 先修这个，再查别的。

## 事故记录

- **2026-11-23 03:04 全站 marvis 503**：部署把 compose 换回仓库版本，而
  `MARVIS_EGRESS_POOL` 当时只存在于部署机上被手改过的那份文件里，从来没进过
  git（`git log -S MARVIS_EGRESS_POOL -- docker-compose.yml` 为空）。加上同一
  次合并还让 header 丢了 `qimei36`（见 `tests/test_marvis_identity_device.py`），
  两条一起把上游风控打到 5/5，触发平台退避。修复：出口池进 compose 默认值 +
  本目录纳入仓库；身份组由 `uskey_identity()` 统一给出设备串。
