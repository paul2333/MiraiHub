# marvis-uskey-sidecar —— Marvis uskey 签名侧车

MiraiHub 的 `Ual-Access-MarvisExt` 需要一块**真** uskey。上游会校验它的尾块，
本地拼装（`base64(magic + F(bus_info) + 随机尾)`）**100%** 回
`HTTP 403 / 业务码 4100404 请求被风控拦截`。只有 Beacon SDK 的
`getUskey('7800255', bus_info)` 整块产出才被接受。

Beacon SDK（`beacon_napi.node`）是 **Windows x64 N-API PE**，Linux 的 node
`dlopen` 不进来。所以这里用 **Wine + Windows node.exe** 常驻一个签名服务。

## 结论先行：生产不需要 Windows

2026-11-18 实测（VPS，账号 190，`/v1/chat/completions`，`max_tokens=8`）：

| 指标 | Wine 侧车 | 真机 Windows |
|---|---|---|
| uskey 长度 | 480 字符 / 360 字节 | 504 字符 / 376 字节 |
| `getQimei()` | `3aeda2c4343993fc64eec8c830001591ab11`（Wine 虚拟设备） | `84097bfe4bec892cb32e99d730001bd1a611` |
| 上游结果 | **HTTP 200** | HTTP 200 |

服务端认的是「SDK 整块的内部自洽 + bus_info 与 header 一字不差」，
**不是**真机设备指纹。**纯 Linux（Wine 侧车）方案成立。**

## 布局（VPS `/opt/marvis-wine/`）

```
node.exe                  node v20.18.0 win-x64
beacon_napi.node          N-API 模块（签名核心）
beacon_sdk.dll beacon_wrapper.dll qimei.dll
libcurl.dll libcrypto-1_1-x64.dll libssl-1_1-x64.dll
msvcp140.dll msvcp140_atomic_wait.dll vcruntime140.dll
vcruntime140_1.dll concrt140.dll
prefix/                   WINEPREFIX（**必须保留**，qimei36 由它派生）
sidecar.js                ← 本目录文件
sidecar.sh / sidecar_stop.sh
```

## 起停

```bash
# 容器内经 docker0 网关访问（默认拓扑）
sudo -u admin SIDECAR_HOST=172.17.0.1 SIDECAR_PORT=8791 /opt/marvis-wine/sidecar.sh

# MiraiHub 与侧车同 netns（host 网络）时
SIDECAR_HOST=127.0.0.1 /opt/marvis-wine/sidecar.sh

/opt/marvis-wine/sidecar_stop.sh
tail -f /tmp/marvis-sidecar.log
```

冷启约 0.3 s 就绪；单次签 **1–15 ms**（进程内 `getUskey` ≈ 0.3 ms）。

## 协议

```
GET  /health
  -> {"ok":true,"qimei36":"3aeda2...","calls":10,"busId":"7800255","appVer":"1.60.3400.35"}

POST /uskey
  {"agentTag":"main","conversationId":"conv_<32hex>","responseId":"<uuid4>",
   "scene":"agent_chat","model":"main-auto","nonce":"<uuid4>","openId":"<openid>"}
  -> {"uskey":"<480|504 字符>","qimei36":"<36 字符>",
      "busInfo":"marvisAgentTag=main&...","uskeyLen":480,"ms":2}
```

`bus_info` 七字段**固定顺序**（错一个字符 F(bus_info) 就错，整块被拒）：

```
marvisAgentTag&marvisConversationID&marvisResponseID&marvisScene&model&nonce&originOpenID
```

`model` 取**请求体原文**，其余六项必须与 header 一字不差。

## MiraiHub 侧对接

```bash
# 容器里 127.0.0.1 是容器自己，必须指到宿主网关
MARVIS_USKEY_SIDECAR=http://172.17.0.1:8791/uskey
```

对应代码：`admin/platform/marvis/core/uskey.py`（客户端）、
`endpoints.py::llm_headers`（每请求现签 + 组装 header）。

## 三条铁律

1. **不要每请求 fork wine**：冷启 1.1 s，常驻进程内 `getUskey` 只要 0.3 ms。
2. **不要用管道接 stdout**：wine node.exe 的 stdout 是管道时直接
   `Error: open EBADF` 秒崩，一律 `>> /tmp/marvis-sidecar.log 2>&1`。
3. **侧车挂了不许本地造 uskey**：那会把「侧车故障」变成「账号被风控」，
   还会把风控计数记到账号头上。`uskey.sign()` 抛 `SidecarError` 就让它抛。

## 一组凭证只能用一次

`(conversationId, responseId, nonce, uskey)` 是**一组**，只对一次请求有效。
撞 `4100404` 必须整组换新再试（`client.open_chat_stream` 已实现：1 次首发 +
1 次整组轮换）。复用同一组会继续撞同一个判定。

## prefix 不能删

`prefix/` 决定 `getQimei()` 的输出。删了会重新生成一个 prefix，
qimei36 跟着变。换 prefix 不影响功能（上游照样 200），但线上就别动了。
