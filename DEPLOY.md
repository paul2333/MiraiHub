# MiraiHub 部署须知（VPS: /home/admin/MiraiHub）

> 这台机器的 `origin` 指向上游只读仓库（xiaofan6ya/MiraiHub），**不是我们的 fork**。
> 部署只认 `fork`（paul2333/MiraiHub）。用 origin 部署 = 把我们的修复全部冲掉。

## 三条铁律

1. **用 fork**：`git remote -v` 里 fork = https://github.com/paul2333/MiraiHub.git
2. **别用 origin**：`origin` 是上游只读镜像，`origin/main` 是 3567fcb 这种老版本；永不 push origin
3. **reset 只认 fork/main**：`git fetch fork && git reset --hard fork/main`

## 标准部署

```bash
cd /home/admin/MiraiHub
git fetch fork
git reset --hard fork/main          # 不是 origin/main！
sudo docker compose up -d --build
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8790/api/app/version   # 期望 200
```

## 部署后自检（两条，缺一不可）

1. **出口池进容器了没有**（2026-11-23 事故：池子丢了一夜没人发现）

   ```bash
   docker exec miraihub-admin-1 env | grep MARVIS_EGRESS_POOL
   docker logs --since 5m miraihub-admin-1 2>&1 | grep -E '出口池已就绪|出口池为空'
   ```

   期望 `出口池已就绪：N 个出口`；出现 `出口池为空` 说明池子没进容器，
   此时所有 marvis 请求都从宿主直连出网 —— 先修这个，再谈别的。

2. **身份组自洽**（header 的 qimei36 必须等于 uskey TLV 里的设备段）

   ```bash
   python -m pytest tests/test_marvis_identity_device.py -q
   ```

   宿主侧出口安装/轮换见 `deploy/marvis-egress/README.md`。

## 连接复用与分段诊断（2026-11-23 引入，默认全关）

| env | 默认 | 含义 |
|---|---|---|
| `MARVIS_HTTP_KEEPALIVE` | 0 | 1 = chat 流式请求走上游长连接池（按 出口×账号×主机 复用） |
| `MARVIS_HTTP_POOL_TTL` | 45 | 空闲连接存活秒数 |
| `MARVIS_HTTP_POOL_PER_KEY` | 2 | 每个键的空闲上限 |
| `MARVIS_HTTP_POOL_MAX_IDLE` | 16 | 进程内空闲上限 |
| `MARVIS_DIAG` | 0 | 分段诊断（只打 `admin.platform.marvis.diag` 一行 JSON，不改报表/计费） |
| `MARVIS_DIAG_SAMPLE` | 1.0 | 诊断采样率；两组对照必须用同一采样率 |

开关方式是改 `.env` 后重建容器：

```bash
docker compose up -d --no-deps --force-recreate admin
docker compose exec -T admin printenv MARVIS_HTTP_KEEPALIVE   # 确认实际生效值
```

回滚：把 `MARVIS_HTTP_KEEPALIVE` 置 0 再重建容器（不需要重建镜像）。关闭时
代码路径与引入前逐字节一致（单测 `tests/test_marvis_http_pool.py::test_legacy_path_has_no_reuse` 守着）。

排障要点：
- 日志出现 `marvis http | ... reused=1` 才是真的复用；
- `连接池已清空 ...` 出现在出口轮换 / `reset_egress_openers()` 之后；
- `delivery_unknown` 是**终止**错误：网关不换出口、不换号、不重发（见 `gateway._attempt`）。

## 禁止

- `git reset --hard origin/main` —— 会回退到上游老版本，冲掉全部 marvis 修复
- `git push origin` / force push —— 上游只读
- 改 `origin` 的指向
- 把 `MARVIS_EGRESS_POOL` 只写进部署机的 `docker-compose.override.yml`：
  它必须留在 `docker-compose.yml` 的默认值里，否则下一次 `reset --hard` 就丢

## 事故记录

- 2026-11-16 02:00：有人执行 `git reset --hard origin/main`，HEAD 从 `676c6a0` 掉回上游 `3567fcb`，
  `20254a0`（命名 404）、`baad481`（事件循环）、`73b621f`（重试/换号 + 画像）全部丢失。
  已用 `git reset --hard fork/main` 恢复，HEAD = 676c6a0。
- 2026-11-23 03:04：合并上游后部署，marvis 全站 503。两个独立回归叠加：
  (1) `uskey_identity()` 不再返回 `qimei36`，header 整个键不写而 TLV 里仍有派生设备段；
  (2) `MARVIS_EGRESS_POOL` 只存在于手改的 compose 里、从未进 git，部署即丢。
  修复：`cb7c487`（身份组统一设备串 + 池子进 compose）、`c2d9a67`（轮换脚本正则）。
  回归测试：`tests/test_marvis_identity_device.py`（无修复 2 failed）。

## 诊断开关（默认关）

`MIRAIHUB_MARVIS_DUMP_403` 会把 403 请求体（含用户对话原文）gzip 落到容器 /tmp，最多留 3 份。
默认关闭；临时开启需要两步：`.env` 里写 `MIRAIHUB_MARVIS_DUMP_403=1`，
并在 `docker-compose.yml` 的 `admin.environment` 下加一行 `MIRAIHUB_MARVIS_DUMP_403: ${MIRAIHUB_MARVIS_DUMP_403:-0}`。
用完记得删掉 compose 那行（否则 git status 常脏，下次 reset 又会踩坑）。
