# 优雅退出与前端壳 · 共享接口契约（captain 维护）

本文件只定义**跨成员共享的接口形状**，行为语义以
`docs/superpowers/plans/2026-11-22-graceful-shutdown-and-shell.md`（下称"计划"）为准。
任何成员不得单方面改动本文件；发现契约需要调整时在任务报告里写明，由 captain 统一修订。
实现文件归属见计划各任务的「文件」段与任务卡 inScope，**不要改不属于你的文件**。

## 0. 队长补充裁决（随实现进展追加，成员必须遵守）

- **C1 排空 503 的连接语义**：对未准入请求返回 503 时，若请求可能带 body（含 chunked），
  必须显式下发 `Connection: close`（并不读取请求体），避免客户端在 503 后补发 body 被同连接
  解析成新请求（实测会得到 `400 Invalid HTTP request received`）。HEAD 已由框架自带 close，不需重复。
- **C2 生命周期接口的确定性**：`GET /api/app/ready` 与 `GET /api/app/lifecycle` 在排空期间必须
  仍可访问（豁免表精确方法+路径），不得被自己的排空判定拦成 `service_draining`。
- **C3 仓库脚本**：`scripts/check_maintenance.py` 等用 `TestClient` 的脚本必须用 `with TestClient(app)`
  让 lifespan 真正执行；否则新中间件会把非豁免路径全部 503，脚本断言假失败。
- **C4 测试归属**：`tests/test_graceful_lifecycle.py` 由 T1 与 T2 共同追加，双方动手前先 read；
  `admin/server.py` 的中间件层与排空 503 归 T1，`_startup` 函数体归 T2，一律用 edit 精确替换。
- **C5 upgrade_hold 租期**：`begin_upgrade_hold(operation_id, hold_until=<epoch 秒>)` 的租期到期后，
  worker 若核实自己仍是同一未提交 hold，应自动 RESUME 恢复准入；提交 stop 后禁用该回退。

工具链（已核实）：Python 3.14.7、uvicorn 0.54.0、fastapi 0.162.2、starlette 1.10.0、node v24.19.0。
仓库无 `.env`（`admin.config` 会加载仓库根 `.env`，本地不存在时用开发默认值）。

## 1. `admin/lifecycle.py`（任务 1 交付）

```python
class LifecycleConflict(RuntimeError):
    snapshot: dict          # 冲突时的当前快照

class Lease:                # 不透明租约；只保证 identity 与一次性释放
    kind: str               # "request" | "job"
    lease_id: str

class Lifecycle:
    instance_id: str                      # 每次 reset() 重新生成的不透明随机串
    def reset(self, *, instance_id: str | None = None) -> None
        # 只允许在 lifespan 启动时调用；阶段回到 starting，计数归零，清空 action/operation
        # instance_id 为 None 时采纳 `WB_LIFECYCLE_INSTANCE_ID`（父进程注入值即本代权威代号）
    def resume(self, operation_id: str, instance_id: str | None = None) -> dict
        # 撤销未提交的 hold：仅当代号属于当前代、且当前操作是同一 operation_id、
        # 且尚未提交停止时可用 —— 清空 action/operation、phase 回到 ready、重新打开准入；
        # 不同代、已提交停止、或 action 不是 upgrade_hold（restart/stop/update 等）一律抛 LifecycleConflict。
        # 即：RESUME 只撤销「尚未提交的部署预排空 hold」，不是通用的取消键。
    def mark_ready(self) -> dict          # starting -> ready；其它阶段调用不改变阶段，返回快照
    def mark_stopping(self) -> dict
    def mark_stopped(self) -> dict
    def mark_failed(self, reason: str = "") -> dict
    def try_admit(self, kind: str = "request") -> Lease | None
        # 仅 ready 且未封口时返回租约；draining/stopping/stopped/failed/starting 返回 None
    def finish(self, lease: Lease | None) -> dict     # 幂等：同一租约只减一次
    def begin(self, action: str, operation_id: str, instance_id: str | None = None) -> dict
        # 锁内封口并返回快照：{"phase": "draining", "action": action, "operation_id": ...,
        # "instance_id": <current>, "active_requests": N, "active_jobs": N, "elapsed_ms": 0,
        # "can_restart": bool, "can_force": bool}
        # 同一 operation_id 幂等返回当前快照；不同 action/operation_id 在已封口时抛 LifecycleConflict
    def force(self, operation_id: str, instance_id: str, confirm: bool) -> dict
        # 只允许作用于"当前代 + 当前进行中的操作"，否则抛 LifecycleConflict；成功返回快照并设置 can_force=False
    def snapshot(self) -> dict
    def request_cancel(self) -> bool      # worker 侧强制中断意图；返回是否首次置位
    async def wait_empty(self) -> None    # 等到 active_requests == 0 且 active_jobs == 0

async def wait_empty() -> None            # 模块级便捷函数（取进程单例）
def get_lifecycle() -> Lifecycle          # 进程单例（首次调用创建）
```

快照字段固定、不得增删：`phase, action, instance_id, operation_id, active_requests,
active_jobs, elapsed_ms, can_restart, can_force`。`phase` 取值
`starting|ready|draining|stopping|stopped|failed`。空闲时 `action`/`operation_id` 为 `None`。
`elapsed_ms` 用单调时钟从 `begin()` 起算；未开始时为 0。
快照内**禁止**出现请求原文、账号、密钥、本机路径。

要点：阶段判定与加入在途集在同一短锁内完成；锁外通知等待者；唤醒后重验谓词；
线程完成任务通过 `loop.call_soon_threadsafe` 投递到事件循环；计数永不为负、释放幂等。

## 2. HTTP 接口（任务 1 提供 / 任务 3 扩展 / 任务 5 消费）

| 方法 | 路径 | 鉴权 | 响应 |
|---|---|---|---|
| GET | `/api/app/ready` | 匿名 | 200 `{"ready": true}` 或 503 `{"ready": false}`，`Cache-Control: no-store` |
| GET | `/api/app/lifecycle` | `require_admin` | 200 快照（第 1 节字段） |
| GET | `/api/app/version` | 匿名 | 保持原样（排空期间仍可用） |
| GET | `/api/app/desktop-ready` | `X-Desktop-Boot` nonce | 保持 nonce 校验；校验通过后按真实状态返回 `{"ready": bool, "instance_id": ...}` |
| POST | `/api/maintenance/restart` | `require_admin` + CSRF + `{"confirm": true}` | 202 `{"ok": true, "message": ..., "lifecycle": 快照}`；无重启所有者 409 且 `can_restart=false`；冲突意图 409 带当前快照 |
| POST | `/api/maintenance/restart/force` | 同上 + `{"confirm": true, "instance_id": ..., "operation_id": ...}` | 202 快照；代号不匹配 409 |

排空拒绝统一：HTTP 503、`Retry-After: 3`、`Cache-Control: no-store`，业务码 `service_draining`。
协议信封：
- `/v1/messages`：`{"type": "error", "error": {"type": "service_draining", "message": ...}}`
- OpenAI 兼容（`/v1/chat/completions`、`/v1/completions`、`/v1/responses` …）：
  `{"error": {"message": ..., "type": "service_draining", "code": "service_draining"}}`
- `/mcp`：`{"jsonrpc": "2.0", "id": null, "error": {"code": -32000, "message": "service_draining"}}`
已准入的 SSE 请求继续写原协议尾帧，不因排空被截断。

保留原鉴权、Cookie/CSRF、显式确认、60 秒新操作频控与审计；**先判幂等，再判频控**。
`env_guard` 生产 404、`_fs_guard` 未放行 409 的行为不变。

## 3. 控制通道（任务 3 交付，任务 4/6/7 消费）

`admin/lifecycle_control.py`
```python
CONTROL_DIR_ENV = "WB_LIFECYCLE_CONTROL_DIR"
AUTHKEY_ENV     = "WB_LIFECYCLE_AUTHKEY"
INSTANCE_ENV    = "WB_LIFECYCLE_INSTANCE_ID"

def prepare_control_secrets(control_dir: Path) -> dict[str, str]
    # 创建私有目录（Windows 当前用户受限目录 / Unix 0700）与随机 authkey，返回要注入子进程的环境变量

class WorkerOwner:
    def __init__(self, control_dir: Path, authkey: bytes, *, timeout: float = 2.0)
    def status(self, *, timeout: float | None = None) -> dict      # 快照；不可达时返回 {"phase": "unreachable", ...} 由实现定义但要可判别
    def begin(self, action: str, operation_id: str, instance_id: str | None = None, *, timeout: float = 10.0) -> dict
    def force(self, operation_id: str, instance_id: str, confirm: bool, *, timeout: float = 10.0) -> dict
    def resume(self, operation_id: str, instance_id: str, *, timeout: float = 10.0) -> dict   # RESUME，供升级预排空撤销
    def begin_upgrade_hold(self, operation_id: str, *, timeout: float = 10.0) -> dict          # BEGIN(action="upgrade_hold")
    def wait_exit(self, *, timeout: float | None = None) -> int | None                          # 需要绑定的 Popen 句柄由构造参数/attach 提供
```

协议：`multiprocessing.connection.Listener/Client`；JSON over `send_bytes/recv_bytes`，
单条消息上限 64 KiB，连接与读取都有小超时；每条消息含 `{"kind": "STATUS|BEGIN|FORCE|RESUME", ...}`；
校验 kind、字段、`instance_id`、`operation_id` 与 authkey。BEGIN/FORCE 对同一操作幂等。
仅本机可用：Windows `AF_PIPE`，Unix 私有目录内 `AF_UNIX`。密钥只在进程环境/受保护运行目录，绝不进浏览器、HTTP 参数与日志。

## 4. 单 worker 运行器（任务 3 交付，任务 4/6/7 消费）

`admin/managed_worker.py`
```python
EXIT_OK = 0
EXIT_PLANNED_RESTART = 75

def serve(app, *, host: str, port: int, lifecycle=None, control_dir: Path | None = None,
          instance_id: str | None = None, proxy_headers: bool = False,
          log_level: str = "info", timeout_graceful_shutdown: int | None = None) -> int
# CLI：python -m admin.managed_worker --host H --port P [--app admin.server:app]
#      [--control-dir DIR] [--proxy-headers] [--log-level info] [--timeout-graceful-shutdown N]
```

行为：显式持有 `uvicorn.Server(uvicorn.Config(...))`；封口后 `await lifecycle.wait_empty()`，
归零才在 server 自己的事件循环里令 `server.should_exit = True`；**没有自动强退秒表**。
正常停止返回 `EXIT_OK`；受理的计划重启返回 `EXIT_PLANNED_RESTART`；其余异常沿用错误码。
排空观察者按 `operation_id` 武装（`_watching_op`）：同一次操作复用，`RESUME` 撤销后的**新**操作必须重新武装，否则下一次 stop/restart 永远不会设置 `should_exit`（worker 永久挂住）。
首次 SIGTERM/SIGINT 只投递正常停止，重复回调幂等。

## 5. 前端（任务 5）

`admin/console/lifecycle.js` 暴露 `window.WBLifecycle = { start, refresh, onPhase, isDraining... }`
（具体函数名由任务 5 自定义，但必须只依赖第 2 节 HTTP 接口与本文件字段）。
等待层使用 `role="status"`；`/admin/assets/lifecycle.js` 由服务端资源哈希自动加版本。
禁止固定 8 秒 `location.reload()` 看门狗；恢复条件为
`phase === "ready" && instance_id !== 旧代 instance_id`。

## 6. 部署（任务 7）

nginx 等待页只服务 **GET/HEAD 的精确 `/admin`、`/admin/`** 文档导航，上游 502/503/504 时返回
503 + `Retry-After` + `Cache-Control: no-store`；`/__waiting/reconnect.js` 自身必须 200 且 MIME 正确；
不得给 `/api`、`/v1`、`/mcp`、`/admin/assets/*` 套 HTML 错误页。
升级闸门改判 `GET /api/app/ready` 的真实 readiness；`upgrade_hold` 用第 3 节 BEGIN/RESUME。
