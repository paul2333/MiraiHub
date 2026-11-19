# Marvis uskey 纯 Python 复刻：把 Wine + 1.4G 侧车从部署链路里删掉

> 一句话：**这个仓库里 marvis 平台的 uskey，以前必须靠 Wine 跑 Windows 版 `node.exe` + `qimei.dll` 才能签出来。现在纯 Python，504 字符 / 376 字节，与官方客户端逐字节相同。**

## 先说结论

| 指标 | 改前 | 改后 |
|---|---|---|
| 部署依赖 | Wine 10 + Windows node.exe + beacon_napi.node + qimei.dll | **零**（Python 标准库） |
| 常驻进程 | 1 个 Wine 侧车（约 190 MB RSS） | **0** |
| 磁盘占用 | `/opt/marvis-wine` 1.4 GB（含 prefix） | **0** |
| 签名耗时 | 约 1-15 ms（跨进程 HTTP） | 约 2 ms（进程内） |
| 输出一致性 | - | **与官方客户端 uskey 逐字节相同** |

单测 **0 外部依赖**，落盘两个文件就行。

## 为什么要做这件事

`Ual-Access-MarvisExt` 里那个 `uskey` 是 marvis 上游的准入凭证。它不是一个随机串，上游会校验：

1. 自定义 base64 字母表（**不是**标准表）
2. header 里有 18 字节设备段，**它同时是 XOR keystream 的原料**
3. 320 字节业务 TLV 里含 **`md5(bus_info)`**，七字段顺序错一个字符就全盘作废
4. TLV 里出现**两次**同一个设备串，且必须与 header 里那串一致

历史上的选择是「装个官方客户端 / 起个 Wine 侧车去调它的 `getUskey`」。能跑，但代价是 1.4 GB 镜像层 + 一个必须在线的 Wine 进程，而且 **Docker recreate 之后侧车挂了、网关就全线 502**。

这次直接把算法逆出来了，**不再需要任何原生组件**。

## 算法本体

```
uskey = custom_b64( header24 || AES-128-CBC(K_AES, IV=K_AES, P224, PKCS7) )

header24 = 030057040111 || dev18(18B)
P336     = TEA_dualfeedback(stream, K_TEA) ^ (C_CONST ^ dev18[2:18])
stream   = rand8(padflag=6) || X1 || TLV(320) || 7x00
```

三层都是**非标准**的，逐一说：

| 层 | 坑 |
|---|---|
| **自定义 base64** | 字母表是 `CDABEFIJHGKLOPMNQRSTWXUVYZabecdfighlmjpqknouyzvrstwx0312546978+/`。用标准表解出来的「明文」是垃圾 —— **这是整套逆向里最贵的一课**，之前所有「key 找错了」的结论都是它造成的假象 |
| **TEA 双反馈** | 不是 CBC、不是 PCBC：`S_i = M_i ^ C_{i-1}`，`C_i = TEA(S_i) ^ S_{i-1}`，`S_-1 = C_-1 = 0`，16 轮，delta `0x9e3779b9`，**大端字** |
| **keystream** | `C_CONST ^ dev18[2:18]` —— 密钥材料就藏在明文 header 里。所以 `dev18` 前 2 字节可以随便填，服务端从 header 自己就能还原 |
| **PKCS7 满块** | 336 % 16 == 0，补满 16 字节 `0x10`，密文 352，24+352 = **376** |

再加一个**踩过就忘不掉**的：TLV 里那 15 字节的进程常量、113 字符的周计数 JSON、`MarvisAgent.exe` / `TX Gaming` 这些身份串，**长度一变后面全部错位**。这份模板的偏移常量是拿 3 组真实样本交叉校准出来的，改任何一个字段长度都得重算。

## 交付物

| 文件 | 行数 | 说明 |
|---|---|---|
| `admin/platform/marvis/core/uskey_local.py` | 约 470 | 完整实现，**只用 `base64 / hashlib / os / struct / time`** |
| `admin/platform/marvis/core/uskey.py` | 约 340 | 对外 `sign()`，本地 mint 优先、侧车降级 |
| `tests/test_marvis_uskey_local.py` | 约 180 | KAT + 结构断言 + 长度断言，**零 mock** |

AES-128 的 S-box 是**用 GF(2^8) 求逆 + 仿射变换现算的**，不是手抄 256 项 —— 省 256 行，而且不会抄错。

## 验证

```
FIPS-197 KAT:  69c4e0d86a7b0430d8cdb78070b4c55a  PASS
4 组真实样本:  decode -> re-encode 逐字节相等（504 字符 x 4）PASS
在线实测:      HTTP 200，真实出字，扣真实 token PASS
```

最后一条不是「跑通了就算」：**把官方客户端的真实 uskey 和本实现产出的 uskey 做逐字节 diff，376/376 全同**。

## 兼容性

- 保留 `MARVIS_USKEY_FORCE_SIDECAR=1` 开关 —— 想用旧侧车做对照随时可切
- `MARVIS_USKEY_MIN_BYTES` 可覆盖长度闸门（默认 376）
- 侧车部署目录 `deploy/marvis-uskey-sidecar/` 保留，不删

## 边界

本 PR **只做签名**。

设备串 `qimei36`（36 个 hex 字符 = 18 字节）由**调用方提供**，通过环境变量 `MARVIS_QIMEI36` 传入。
本模块不生成、不派生、不缓存它 —— 它就是上游协议要求的一个输入，和 `model` / `openid` 同一个层级。

它必须写进三个地方且完全一致：

1. 请求头 `Ual-Access-MarvisExt` 里那个字段
2. uskey 的 TLV 偏移 45（`OFF_QIMEI1`）
3. uskey 的 TLV 偏移 150（`OFF_QIMEI2`）

不一致的直接结果就是请求被拒。本模块在 `build_tlv()` 里对这一致性做了断言。

## 这个 PR 值多少

- **删掉 1.4 GB 镜像层 + 一个 Wine 常驻进程**
- 消灭「侧车挂了 → 网关全 502」这一整类故障
- 让 marvis 平台第一次可以**在纯 Linux 容器里跑，不依赖任何 Windows 二进制**
- 顺便把「自定义 base64 + TEA 双反馈 + 明文头当密钥」这套写法钉成文档，后来人不用再逆一遍

**这套东西不是猜出来的：** 三个 key、字母表、TEA 变体、TLV 偏移，每一处都能拿真实 uskey 逐字节回归。有异议的话，跑 `pytest tests/test_marvis_uskey_local.py` 就行。
