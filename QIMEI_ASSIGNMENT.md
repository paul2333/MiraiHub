# qimei 自动分配策略与实现（2026-11-19）

> 目标：新账号进来时自动分配 qimei36。默认生成独立真品；生成不可用或来不及时
> 复用一台已有真品，但**一台设备最多挂 2 个号**，这是硬约束。

## 0. 一句话结论

- **优先顺序**：existing（已有）→ generated（新真品）→ shared（池里 0/1 个 owner 的真品）→ 容量拒绝。
- **派生（derived）不作为自动分配结果**：只在显式 --allow-derived 救灾时返回，不落表、不占名额。生成失败要打 WARNING 再走共用，不能静默变派生。
- **一对多**：qimei_table.json 保持 uid -> qimei 不变；新增 qimei_pool.json 记 qimei -> owners[] + real + 时间戳。加载时 owners > 2 直接 QimeiPoolError，启动失败。
- **回收**：删除账号后从 owners 摘除（设备保留为 0 owner，可再复用）；uid 变更时 assign 会把旧名额摘掉；兜底 --reconcile --prune 清理账号已不存在的名额。

## 1. 任务 A(a)：优先顺序与理由

| 顺位 | 来源 | 何时发生 | 理由 |
|---|---|---|---|
| 0 | existing | uid 已在表里 | 幂等；重新登录/重复导入不能换设备，否则一个号两套设备指纹。 |
| 1 | generated | 新 uid，生成器可用 | 独立真品是唯一没有「设备簇连坐」和「自报假指纹」风险的选项。生成耗时实测 7–13s，可接受。 |
| 2 | shared | 生成失败/超时，或 --no-generate | 用户明确允许临时共用；优先 0 owner（等于独立，还省一次生成），其次 1 owner（占第 2 个名额）。 |
| 3 | 拒绝 | 所有真品都满 2 owner | 宁可不分配，也不写 owners=3；也不静默派生。 |
| — | derived | 仅 allow_derived=True / --allow-derived | 派生值是格式合法但自报「假的」；把它当默认兜底会把容量耗尽伪装成成功，掩盖故障。 |

**为什么不是「生成 > 派生 > 共用」**：2026-11-18 的事故是 8 个号共用一个真机
qimei，被上游按设备簇整体标记。共用有明确上限（2）且可观测；派生值没有簇风险，
但它本身是假信号，且会让「池满」永远不暴露。用户的原话也是「优先独立真品，
退回最多 2 号共用」，所以派生只能作为人工救灾出口。

**为什么 0-owner 真品排在 1-owner 前面**：0 owner 的设备是「回收后闲置」，
复用它等于独立设备，不需要 7–13s 生成；1 owner 才是真正的共用。

## 2. 任务 A(b)：怎么挑「只有 1 个号的真品」

可实现判据（_pick_shareable）：

1. real == True；
2. len(owners) < 2；
3. 排序键 (len(owners), last_used_at, created_at, qimei) 升序：
   - 先 0 owner，再 1 owner；
   - 同档里 last_used_at 最早（最久没被用）优先；
   - 再按 created_at 最早（老设备优先，避免新设备反复被占）；
   - 最后 qimei 字符串做确定性兜底，保证多进程/多次运行结果一致。

**为什么不是「按 4100404 历史最少」**：目前 4100404 只在账号级日志里，池里没有
按 qimei 聚合的风险计数，拿不到可信排序数据。等以后加 risk_4100404 字段，
把它插到排序键第一位即可，接口不变。

**为什么不用随机**：随机不可复现，出问题无法解释「为什么这个号挂到这台设备」。

## 3. 任务 A(c)：一对多怎么表达 + 硬约束

qimei_table.json（保持原形态，兼容 uskey 读路径）：

~~~json
{
  "26398567703977D1C41C97C53703EC3F": "f8b14b30f3d0b37b57f2412830001631ab12",
  "303BEB6C491F3B5AB2070428533DDE5A": "c8c1d41116827b9eaed47f5d30001ea1ab12"
}
~~~

qimei_pool.json（新增，容量权威）：

~~~json
{
  "f8b14b30f3d0b37b57f2412830001631ab12": {
    "owners": ["26398567703977D1C41C97C53703EC3F"],
    "real": true,
    "source": "migrated",
    "created_at": "2026-11-18T03:52:00Z",
    "last_rotated": "2026-11-18T03:52:00Z",
    "last_used_at": "2026-11-19T02:10:00Z"
  }
}
~~~

字段：

- owners：挂在这台设备上的 uid 列表，**硬上限 2**。
- real：true = 真机 qimei（Wine/MAC 算出来的）；false 留给未来的非真品。
- source：generated / migrated / shared，排障用。
- created_at / last_rotated：设备进入池子 / 最近换 MAC 的时间。
- last_used_at：最近一次被分配的瞬间，LRU 判据。

加载校验（load_state）：

1. 任一 qimei 的 owners 去重后 > 2 → QimeiPoolError；
2. 同一个 uid 不能出现在两台设备的 owners 里（漂移时保留字典序靠前，打 WARNING）；
3. table 里的每个 uid 必须在该 qimei 的 owners 里（缺了就补，补进去会超 2 则报错）；
4. table 指向 q1、pool 却把 uid 挂在 q2 → 从 q2 摘除（table 是读路径权威）。

启动接入：admin/platform/marvis/__init__.py 的 install() 调用 validate_or_raise()。
池里出现 owners=3，进程 import 阶段就抛错，容器起不来，不会带病运行。
uskey._qimei_table() 也复用 load_state()，读路径同样校验。

**原子写**：同一目录 .tmp.<pid>.<rand> + os.replace，indent=2、sort_keys=True、
ensure_ascii=False、末尾换行、权限 0600。先写 pool（先占名额）再写 table（读路径生效）：
两步之间崩溃最多留下「名额已占、表未挂」，加载时会把 table 合并回 pool，容量只会更保守，
不会超 2。

## 4. 任务 A(d)：账号删除 / uid 变更怎么回收

- release(uid)：从 qimei_table 删 uid，从所有 owners 摘掉；设备本身保留（owners 可能变 0），
  下次可被 shared 直接复用，不用重新生成。
- 删除接入：install_orm_hooks() 注册 SQLAlchemy after_delete，把 uid 记到 session.info，
  **事务 after_commit 才真正回收**，after_rollback 丢弃。这样覆盖所有 db.delete(account)
  路径（单删、批量删、去重删），且不会出现「事务回滚但名额已释放」。
- uid 变更：assign(new_uid) 发现旧 uid 还挂在某台设备上时，load 阶段会按 table 归一化；
  release(old_uid) 可显式清。Marvis 的 uid 就是 openid，正常不会变；换号等于新账号，
  旧账号删除时走上面的钩子。
- 兜底：--reconcile --prune 扫描 accounts 全表，把已不存在的 uid 从 owners/table 里清掉。
  prune 默认关闭，避免把「正在创建、DB 还没提交」的号误删。

## 5. 任务 B：实现与接入点

新增 admin/platform/marvis/core/qimei_assign.py：

- assign(uid) -> qimei36：主入口；assign_ex() 返回 source/owners 供日志和测试。
- release(uid)、load_state()、validate_or_raise()、reconcile()。
- install_orm_hooks()：删除回收。
- serve_generator()：宿主 HTTP 生成服务（容器没有 Wine，见部署）。
- CLI（模块方式，容器内跑；宿主没装 sqlalchemy，宿主上用文件路径跑同一个 main）：

~~~bash
python3 -m admin.platform.marvis.core.qimei_assign --reconcile --dry-run
python3 -m admin.platform.marvis.core.qimei_assign --reconcile --prune
python3 -m admin.platform.marvis.core.qimei_assign --assign UID
python3 -m admin.platform.marvis.core.qimei_assign --release UID
python3 -m admin.platform.marvis.core.qimei_assign --sync-pool
~~~

接入点（已核对生产代码）：

1. **统一入库口** admin/platform/marvis/services/accounts.py::_upsert。
   扫码登录 auth_submit、/api/marvis/import、import-batch 全部走它。
   在 db.commit()/refresh() 之后调用 _assign_qimei_for_account(account)。
   重复导入（created=False）也会调用，assign 幂等返回 existing。
2. **启动校验** admin/platform/marvis/__init__.py::install()。
3. **删除回收** install_orm_hooks()（在 install 里注册）。
4. **兜底对账** --reconcile：扫 platform='marvis' AND status='active' 的账号，
   表与池都没有的逐个分配；池里有、表里没有的补表（healed）。

生成失败处理：assign_ex 捕获生成异常 → logger.warning("qimei generate failed uid=...; fallback=shared")
→ 走共用；不静默派生。账号入库不回滚（避免因为设备池耗尽丢账号），但会打
ERROR 日志，reconcile 报告里也会列出失败。

日志格式（每次分配一行）：

~~~
qimei assigned uid=2639****EC3F source=generated owners=1 qimei=f8b14b...ab12
qimei assigned uid=303B****DE5A source=shared owners=2 qimei=f8b14b...ab12
~~~

uid 掩码规则：长度 > 8 时前 4 + **** + 后 4；qimei 掩码前 6 + ... + 后 4。

## 6. 任务 C：实测结果

见交付说明中的命令输出（同一台生产机、未重建镜像、未重启容器）：

1. --reconcile --dry-run 对现有 8 个 active Marvis 账号：无需分配。
2. 临时目录空池：
   - 假 uid A + 注入生成器 → source=generated owners=1；
   - 假 uid B + 生成器抛错 → source=shared owners=2；
   - 假 uid C + 生成器抛错 → QimeiCapacityError: qimei 已满 2 号，磁盘 owners 仍为 2。
3. 手工把 owners 改成 3 → load_state 抛 QimeiPoolError，CLI 退出码 3。
4. 生产 qimei_table.json md5 前后一致（见交付记录）。

## 7. 部署要求

**必须重建镜像**：Dockerfile 是 COPY admin ./admin，/app 不是 bind mount。
新增模块、uskey.py、services/accounts.py、marvis/__init__.py 的改动
不重建就不会进容器。

**必须改 compose（否则「默认生成真品」落不了地）**：

容器里没有 Wine / netns / iptables，/opt/marvis-wine/gen_qimei_for_mac.sh 跑不了。
两种上线方式：

1. **推荐：宿主生成服务 + 容器 HTTP 调用**
   - 宿主用 systemd 跑（宿主没有 sqlalchemy，必须用文件路径，不能 -m）：
     python3 /home/admin/MiraiHub/admin/platform/marvis/core/qimei_assign.py --serve-generator --host 172.17.0.1 --port 8792
     （服务文件已放在 deploy/qimei-gen/qimei-gen.service，本次未 enable/start）。
   - compose 的 admin 服务加：
     QIMEI_GEN_URL: http://172.17.0.1:8792/qimei、QIMEI_GEN_TIMEOUT: "30"。
   - 生成不可达时自动退回 shared，不会阻塞入库。
2. **只跑共用（临时）**：不配 QIMEI_GEN_URL。容器里生成器不存在，新号会走
   source=shared；这只满足「不超过 2 号共用」，不满足「默认独立真品」。

**持久化（强烈建议）**：把两个 JSON bind mount 进容器，否则分配写在容器可写层，
docker compose up --build / 重建容器后丢失，且宿主看不到容量台账：

~~~yaml
volumes:
  - admin_data:/data
  - ./admin/platform/marvis/core/qimei_table.json:/app/admin/platform/marvis/core/qimei_table.json
  - ./admin/platform/marvis/core/qimei_pool.json:/app/admin/platform/marvis/core/qimei_pool.json
~~~

第一次挂载前先在宿主生成 pool（不改 table 值；宿主用文件路径跑）：

~~~bash
cd /home/admin/MiraiHub
python3 admin/platform/marvis/core/qimei_assign.py --sync-pool
~~~

**不需要改 DB schema**；accounts 表一条记录都不动。**不需要改 nginx**。

**启动失败语义**：池里 owners>2 时 install() 抛错，容器起不来（compose 会
restart 循环）。这是硬约束的预期行为：先修 JSON 再启动，不要临时放宽上限。

## 8. 没做的部分 / 原因

- **没有部署**：按硬约束只交代码+测试+文档；没有 docker compose build/up，没有
  重启容器，没有安装 systemd 单元。宿主生成服务只做了代码路径，没有常驻。
- **没有改 DB**：8 条账号原样；reconcile 只读 mode=ro。
- **没有 git add/commit/push**：生产树保持未暂存状态。
- **没有把 8 条真值写进 pool 文件后再改 table**：--sync-pool 只新增 pool，
  table 的 8 条值/顺序/缩进保持原样（md5 不变）。
- **没有做 per-qimei 4100404 风险计数**：需要新表/新列，超出本次「分配器」范围；
  _pick_shareable 已留好排序键扩展位。
- **没有自动升级 shared → generated**：本次只做「新号进来时分配」。已有共用号要
  拆成独立真品，可以后续加 --rebalance（遍历 2-owner 设备，生成新真品后迁移其中一个
  owner）；这次没做，避免在没有部署窗口时改存量。
- **没有测试真实 13s 生成接入新号**：生成脚本单独实测过（7.0s），但自动分配链路
  用注入生成器验证，避免在未部署的容器里跑 Wine 影响线上。
