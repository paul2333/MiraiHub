"""urllib 平台通用的「按账号」HTTPS 长连接池（r3，从 Marvis `http_pool` 提出来的通用部分）。

## 为什么

Trae / CodeArts / Qoder 的对话请求走 urllib：每发请求都新建 TCP + TLS（经代理时
还有 CONNECT），这笔成本发生在首包之前。Marvis 的实测：冷连接 p50 2730 ms，
复用 p50 296 ms（跨境出口）。直连时差得少，但每次仍有 1~3 个 RTT。

## 边界（与 Marvis `admin/platform/marvis/core/http_pool.py` 相同）

1. **键含账号**：`(proxy, account, host, port)`。不同号不共用 socket（风控特征）。
2. **独占租约**：一条连接同时只给一个请求；池空直接新建，不排队。
3. **只有「HTTP 消息完整读完 + 允许保持 + socket 有效 + 代次一致」才归还**。
   SSE 读到 `[DONE]` 就停的调用方，close 时会在很短的预算内（默认 200 ms /
   64 KB，`ADMIN_URLLIB_KEEPALIVE_DRAIN_MS`）把剩下的终止块读完，读不完就销毁。
4. **交付未知**（`open_pooled`）：`request()` 开始之后的失败一律抛
   `DeliveryUnknown`，调用方**不重试、不换号、不换出口**；唯一例外是复用连接
   在「写请求」阶段被对端关闭、或一个响应字节都没收到时被对端优雅关闭（空闲
   关闭竞态），同路由换新连接重发一次。建连失败（请求没发出）抛 `ConnectError`
   （`OSError` 子类），调用方按原来的网络错误处理。
5. 锁内只做字典操作；建连、读写、关闭都在锁外。

## 开关（默认全关：关着时调用方完全走原 opener，行为与改动前一致）

| env | 默认 | 含义 |
|---|---|---|
| `ADMIN_URLLIB_KEEPALIVE` | 0 | 总开关（trae / codearts / qoder 都看它） |
| `ADMIN_URLLIB_KEEPALIVE_<PLATFORM>` | 空 | 按平台覆盖：`1` 单独打开、`0` 单独关闭（例如 `ADMIN_URLLIB_KEEPALIVE_TRAE=1`） |
| `ADMIN_URLLIB_POOL_TTL` | 30 | 空闲秒数（低于常见的 60s 服务端空闲超时） |
| `ADMIN_URLLIB_POOL_PER_KEY` | 2 | 每键空闲上限 |
| `ADMIN_URLLIB_POOL_MAX_IDLE` | 32 | 每平台空闲总上限 |
| `ADMIN_URLLIB_POOL_MAX_IN_USE` | 64 | 每平台在用上限；到顶交回原 opener（不排队） |
| `ADMIN_URLLIB_POOL_SWEEP_INTERVAL` | 15 | 跨键过期扫描最小间隔（秒） |
| `ADMIN_URLLIB_KEEPALIVE_DRAIN_MS` | 200 | close 时补读终止块的时间预算；0 = 不补读 |
| `ADMIN_URLLIB_STALE_RETRY` | 1 | 空闲关闭竞态重发一次；0 = 发出即终止 |

建连超时沿用 `ADMIN_UPSTREAM_CONNECT_TIMEOUT`（默认 10s）。

## 不走池的情况（`open_pooled` 返回 None，调用方用原 opener）

开关关、非 HTTPS、账号键为空、在用到顶、显式代理不是 `http://host:port`（带认证 /
socks）、环境代理（trust_env）同理。
"""
from __future__ import annotations

import http.client
import io
import logging
import os
import select
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import weakref
from collections import OrderedDict, deque, namedtuple
from urllib.parse import urlsplit

logger = logging.getLogger("admin.http_keepalive")

Key = namedtuple("Key", "proxy account host port")

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env_float(name: str, default: float, low: float) -> float:
    try:
        return max(low, float(os.getenv(name) or default))
    except ValueError:
        return default


def _env_int(name: str, default: int, low: int) -> int:
    try:
        return max(low, int(os.getenv(name) or default))
    except ValueError:
        return default


def enabled(platform: str) -> bool:
    """按平台的开关：平台变量（非空）优先，否则看总开关。每次读环境变量。"""
    name = "ADMIN_URLLIB_KEEPALIVE_" + "".join(
        ch if ch.isalnum() else "_" for ch in str(platform or "")).upper()
    own = (os.getenv(name) or "").strip().lower()
    if own in _TRUE:
        return True
    if own in _FALSE:
        return False
    return (os.getenv("ADMIN_URLLIB_KEEPALIVE") or "").strip().lower() in _TRUE


def stale_retry_enabled() -> bool:
    return (os.getenv("ADMIN_URLLIB_STALE_RETRY") or "1").strip().lower() not in _FALSE


def drain_budget_s() -> float:
    return _env_float("ADMIN_URLLIB_KEEPALIVE_DRAIN_MS", 200, 0) / 1000.0


# ---------------------------------------------------------------------------
# 纯函数 / 连接工具（Marvis http_pool 复用这几项）
# ---------------------------------------------------------------------------
def parse_proxy(proxy: str):
    """只接受 `http://host:port`（无认证、无路径）；其余返回 None。"""
    raw = str(proxy or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme.lower() != "http":
        return None
    if parts.username or parts.password:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    try:
        port = int(parts.port or 0)
    except ValueError:
        return None
    if not parts.hostname or port <= 0:
        return None
    return parts.hostname, port


def resolve_proxy(url: str, proxy: str | None, *, trust_env: bool):
    """本次请求在池里用哪个出口。返回 `""`（直连）、`"http://h:p"`，或 None（不走池）。

    语义对齐调用方原来的 opener：显式 `proxy` 优先；没有时 `trust_env=True` 读
    环境代理（含 no_proxy），`False` 一律直连。
    """
    explicit = str(proxy or "").strip()
    if explicit:
        return explicit if parse_proxy(explicit) else None
    if not trust_env:
        return ""
    host = urlsplit(url).hostname or ""
    try:
        env = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        return None
    chosen = str(env.get("https") or "").strip()
    if not chosen:
        return ""
    try:
        if host and urllib.request.proxy_bypass(host):
            return ""
    except Exception:  # noqa: BLE001
        return None
    return chosen if parse_proxy(chosen) else None


def is_stale(conn) -> bool:
    """零等待探活：socket 已关或「空闲时可读到应用层数据 / EOF」都算陈旧。"""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return True
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    if not readable:
        return False
    prev = sock.gettimeout()
    try:
        sock.setblocking(False)
        try:
            sock.recv(1)
        except (ssl.SSLWantReadError, BlockingIOError, InterruptedError):
            return False
        except (ssl.SSLZeroReturnError, OSError):
            return True
    finally:
        try:
            sock.settimeout(prev)
        except OSError:
            pass
    return True


def close_quietly(conn) -> None:
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def restore_read_timeout(conn, timeout: float) -> None:
    try:
        conn.timeout = timeout
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.settimeout(timeout)
    except Exception:  # noqa: BLE001
        pass


def socket_of(resp):
    """从 HTTPResponse（或本模块 / Marvis 的包装）里找到底层 socket；找不到返回 None。"""
    seen = 0
    while resp is not None and seen < 4:
        inner = getattr(resp, "_resp", None)
        if inner is None:
            break
        resp, seen = inner, seen + 1
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is None:
        sock = getattr(fp, "_sock", None)
    return sock


def shutdown_socket(sock) -> bool:
    """跨线程打断阻塞读：`shutdown(SHUT_RDWR)` 让另一线程里的 recv 立刻返回。

    用 `socket.socket.shutdown`（不是 SSLSocket 的重载）：后者会把 `_sslobj`
    置空，正在读的线程可能拿到 AttributeError。这里只关底层 fd 的读写方向，
    真正的 close 仍由读线程自己的 `with resp` / finally 完成。
    """
    if sock is None:
        return False
    try:
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
        return True
    except (OSError, ValueError, TypeError):
        return False


class AbortHandle:
    """消费端（事件循环）打断生产线程里阻塞的上游读（r3 第 2 项）。

    生产线程拿到上游响应后 `attach(resp)`，读完 / 出错后（**在 close 之前**）
    `detach()`；消费端走掉时 `abort()`：对底层 socket 做 `shutdown`，阻塞在 recv
    的读线程立刻拿到 EOF / 错误退出，平台生成器的 finally 随即释放账号、记账，
    不用等读超时（Qoder 默认 600 秒）。

    * 先 abort 后 attach：attach 时立即 shutdown 并返回 False；
    * detach 之后 abort 不做任何事——连接可能已经归还连接池、正被别的请求用，
      所以调用方必须在 close（归还）之前 detach；
    * 线程安全、幂等，`abort()` 不抛异常。
    """

    __slots__ = ("_lock", "_resp", "aborted")

    def __init__(self):
        self._lock = threading.Lock()
        self._resp = None
        self.aborted = False

    def attach(self, resp) -> bool:
        with self._lock:
            if not self.aborted:
                self._resp = resp
                return True
        shutdown_socket(socket_of(resp))
        return False

    def detach(self) -> None:
        with self._lock:
            self._resp = None

    def abort(self) -> bool:
        with self._lock:
            if self.aborted:
                return False
            self.aborted = True
            resp, self._resp = self._resp, None
        if resp is None:
            return False
        try:
            return shutdown_socket(socket_of(resp))
        except Exception:  # noqa: BLE001 - 兜底清理绝不抛
            return False


def _connect_timeout(read_timeout) -> float:
    value = 10.0
    try:
        from admin import upstream_http

        value = float(upstream_http.connect_timeout() or 0) or 10.0
    except Exception:  # noqa: BLE001
        value = 10.0
    try:
        return min(value, float(read_timeout)) if read_timeout else value
    except (TypeError, ValueError):
        return value


def _default_make_connection(key: Key, timeout: float):
    """建一条（可能经 CONNECT 隧道的）HTTPS 连接；SNI / 证书校验针对目标主机。"""
    from admin.upstream_http import ssl_context

    context = ssl_context()
    proxy = parse_proxy(key.proxy)
    if proxy:
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], timeout=timeout,
                                           context=context)
        conn.set_tunnel(key.host, key.port)
    else:
        conn = http.client.HTTPSConnection(key.host, key.port, timeout=timeout,
                                           context=context)
    conn.connect()
    return conn


# ---------------------------------------------------------------------------
# 池
# ---------------------------------------------------------------------------
class Lease:
    __slots__ = ("key", "conn", "reused", "generation", "connect_ms", "_returned", "pool")

    def __init__(self, pool, key, conn, *, reused, generation, connect_ms=0.0):
        self.pool = pool
        self.key = key
        self.conn = conn
        self.reused = reused
        self.generation = generation
        self.connect_ms = connect_ms
        self._returned = False


class KeepAlivePool:
    """一个平台一个实例。逻辑与 Marvis `http_pool` 相同，状态按实例隔离。"""

    def __init__(self, name: str):
        self.name = str(name or "")
        self._lock = threading.Lock()
        self._idle: "OrderedDict[Key, list]" = OrderedDict()
        self._in_use = 0
        self._generation = 0
        self._last_sweep = 0.0
        self._gc_pending: deque = deque()
        self.stats_counters = {
            "acquire": 0, "hit": 0, "miss": 0, "created": 0,
            "evict_ttl": 0, "evict_capacity": 0, "evict_stale": 0,
            "evict_generation": 0, "evict_unusable": 0, "purge": 0,
            "sweep": 0, "gc_released": 0, "drained": 0,
            "stale_retry": 0, "delivery_unknown": 0, "fallback_capacity": 0,
        }
        # 测试注入点
        self.make_connection = _default_make_connection
        self.now = time.monotonic

    # -- 配置 --------------------------------------------------------------
    def enabled(self) -> bool:
        return enabled(self.name)

    @staticmethod
    def ttl() -> float:
        return _env_float("ADMIN_URLLIB_POOL_TTL", 30, 1)

    @staticmethod
    def per_key() -> int:
        return _env_int("ADMIN_URLLIB_POOL_PER_KEY", 2, 0)

    @staticmethod
    def max_idle() -> int:
        return _env_int("ADMIN_URLLIB_POOL_MAX_IDLE", 32, 0)

    @staticmethod
    def max_in_use() -> int:
        try:
            return int(os.getenv("ADMIN_URLLIB_POOL_MAX_IN_USE") or 64)
        except ValueError:
            return 64

    @staticmethod
    def sweep_interval() -> float:
        return _env_float("ADMIN_URLLIB_POOL_SWEEP_INTERVAL", 15, 1)

    def _bump(self, name: str, delta: int = 1) -> None:
        with self._lock:
            self.stats_counters[name] = self.stats_counters.get(name, 0) + delta

    # -- GC 兜底 -----------------------------------------------------------
    def _drain_gc(self) -> int:
        drained = 0
        while True:
            try:
                lease = self._gc_pending.popleft()
            except IndexError:
                break
            with self._lock:
                pending = not lease._returned
            self.release(lease, reusable=False)
            if pending:
                drained += 1
                self._bump("gc_released")
        if drained:
            logger.warning("%s 连接池：%d 个响应未 close 即被回收，已归还租约", self.name, drained)
        return drained

    def _sweep_locked(self, now: float) -> list:
        if now - self._last_sweep < self.sweep_interval():
            return []
        self._last_sweep = now
        self.stats_counters["sweep"] += 1
        ttl = self.ttl()
        victims: list = []
        for key in list(self._idle.keys()):
            keep = []
            for entry in self._idle[key]:
                conn, since, gen = entry
                if gen != self._generation:
                    self.stats_counters["evict_generation"] += 1
                    victims.append(conn)
                elif now - since > ttl:
                    self.stats_counters["evict_ttl"] += 1
                    victims.append(conn)
                else:
                    keep.append(entry)
            if keep:
                self._idle[key] = keep
            else:
                self._idle.pop(key, None)
        return victims

    def at_capacity(self) -> bool:
        limit = self.max_in_use()
        if limit <= 0:
            return False
        self._drain_gc()
        with self._lock:
            return self._in_use >= limit

    # -- 租约 --------------------------------------------------------------
    def acquire(self, key: Key, *, timeout: float, force_fresh: bool = False) -> Lease:
        self._drain_gc()
        victims: list = []
        candidate = None
        with self._lock:
            self.stats_counters["acquire"] += 1
            victims.extend(self._sweep_locked(self.now()))
            if not force_fresh:
                items = self._idle.get(key)
                while items:
                    conn, since, gen = items.pop()
                    if gen != self._generation:
                        self.stats_counters["evict_generation"] += 1
                        victims.append(conn)
                        continue
                    if self.now() - since > self.ttl():
                        self.stats_counters["evict_ttl"] += 1
                        victims.append(conn)
                        continue
                    candidate = conn
                    break
                if items is not None and not items:
                    self._idle.pop(key, None)
            self.stats_counters["hit" if candidate is not None else "miss"] += 1
            self._in_use += 1
            generation = self._generation
        for conn in victims:
            close_quietly(conn)
        if candidate is not None and not is_stale(candidate):
            return Lease(self, key, candidate, reused=True, generation=generation)
        if candidate is not None:
            close_quietly(candidate)
            self._bump("evict_stale")
        began = self.now()
        try:
            conn = self.make_connection(key, _connect_timeout(timeout))
        except BaseException:
            with self._lock:
                if self._in_use > 0:
                    self._in_use -= 1
            raise
        restore_read_timeout(conn, timeout)
        self._bump("created")
        return Lease(self, key, conn, reused=False, generation=generation,
                     connect_ms=(self.now() - began) * 1000.0)

    def _total_idle_locked(self) -> int:
        return sum(len(items) for items in self._idle.values())

    def _oldest_locked(self):
        best = None
        for key, items in self._idle.items():
            for idx, (_c, since, _g) in enumerate(items):
                if best is None or since < best[2]:
                    best = (key, idx, since)
        return (best[0], best[1]) if best else None

    def release(self, lease: Lease, *, reusable: bool) -> None:
        """归还或销毁租约。幂等。"""
        victims: list = []
        with self._lock:
            if lease._returned:
                return
            lease._returned = True
            if self._in_use > 0:
                self._in_use -= 1
            generation = self._generation
        conn = lease.conn
        if not reusable or lease.generation != generation or is_stale(conn):
            with self._lock:
                if not reusable:
                    self.stats_counters["evict_unusable"] += 1
                elif lease.generation != generation:
                    self.stats_counters["evict_generation"] += 1
                else:
                    self.stats_counters["evict_stale"] += 1
            close_quietly(conn)
            return
        with self._lock:
            items = self._idle.setdefault(lease.key, [])
            items.append([conn, self.now(), lease.generation])
            while len(items) > self.per_key():
                victims.append(items.pop(0)[0])
                self.stats_counters["evict_capacity"] += 1
            if not items:
                self._idle.pop(lease.key, None)
            while self._total_idle_locked() > self.max_idle():
                oldest = self._oldest_locked()
                if oldest is None:
                    break
                key2, idx = oldest
                victims.append(self._idle[key2].pop(idx)[0])
                self.stats_counters["evict_capacity"] += 1
                if not self._idle[key2]:
                    self._idle.pop(key2, None)
        for conn2 in victims:
            close_quietly(conn2)

    def purge(self, reason: str = "") -> int:
        victims: list = []
        with self._lock:
            for items in self._idle.values():
                victims.extend(conn for conn, _s, _g in items)
            self._idle.clear()
            self._generation += 1
            self.stats_counters["purge"] += 1
        for conn in victims:
            close_quietly(conn)
        if victims:
            logger.info("%s 连接池已清空：%d 条（reason=%s）", self.name, len(victims), reason or "-")
        return len(victims)

    def stats(self) -> dict:
        self._drain_gc()
        with self._lock:
            data = dict(self.stats_counters)
            data.update({"platform": self.name, "generation": self._generation,
                         "in_use": self._in_use, "idle": self._total_idle_locked(),
                         "keys": len(self._idle), "gc_pending": len(self._gc_pending)})
        data["enabled"] = self.enabled()
        data["config"] = {"ttl_s": self.ttl(), "per_key": self.per_key(),
                          "max_idle": self.max_idle(), "max_in_use": self.max_in_use(),
                          "connect_timeout_s": _connect_timeout(0),
                          "sweep_interval_s": self.sweep_interval(),
                          "drain_ms": int(drain_budget_s() * 1000)}
        total = data["hit"] + data["miss"]
        data["hit_rate"] = round(data["hit"] / total, 3) if total else 0.0
        return data

    def reset_for_tests(self) -> None:
        with self._lock:
            for items in self._idle.values():
                for conn, _s, _g in items:
                    close_quietly(conn)
            self._idle.clear()
            for name in self.stats_counters:
                self.stats_counters[name] = 0
            self._generation = 0
            self._in_use = 0
            self._last_sweep = 0.0
            self._gc_pending.clear()


_POOLS: dict = {}
_POOLS_LOCK = threading.Lock()


def pool_for(platform: str) -> KeepAlivePool:
    name = str(platform or "").strip().lower()
    with _POOLS_LOCK:
        pool = _POOLS.get(name)
        if pool is None:
            pool = _POOLS[name] = KeepAlivePool(name)
        return pool


def stats() -> dict:
    """所有 urllib 平台池的快照（不含凭据、不含完整代理串以外的敏感信息）。"""
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    return {pool.name: pool.stats() for pool in pools}


def purge_all(reason: str = "shutdown") -> int:
    """清空所有平台池的空闲连接（关机时由 server._stop_upstream_connections 调用）。

    在用连接不动：它们由各自的响应 close 时处理（代次已 +1，不会再归还进池）。
    """
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    total = 0
    for pool in pools:
        try:
            total += pool.purge(reason)
        except Exception:  # noqa: BLE001 - 关闭阶段尽力而为
            logger.debug("清空 %s 连接池失败", pool.name, exc_info=True)
    return total


# ---------------------------------------------------------------------------
# 响应包装
# ---------------------------------------------------------------------------
class PooledResponse:
    """包住 `http.client.HTTPResponse`：补「读完了吗」与「归还租约」，其余透传。

    支持 `for line in resp`（Trae / CodeArts / Qoder 都按行迭代）、`read / read1 /
    readline / headers / status / getcode`，以及 `with`。允许调用方挂属性
    （Trae 会 `setattr(resp, "_trae_host", ...)`）。
    """

    def __init__(self, resp, lease: Lease):
        self._resp = resp
        self._lease = lease
        self._closed = False
        self._finished = False
        pool = lease.pool
        self._finalizer = weakref.finalize(self, pool._gc_pending.append, lease)
        self._finalizer.atexit = False

    @property
    def headers(self):
        return self._resp.headers

    @property
    def status(self):
        return self._resp.status

    @property
    def code(self):
        return self._resp.status

    def getcode(self):
        return self._resp.status

    @property
    def reused(self) -> bool:
        return bool(self._lease.reused)

    def read(self, amt=None):
        data = self._resp.read(amt)
        if amt is None:
            self._finished = True
        elif amt > 0 and (not data or len(data) < amt):
            self._finished = True
        return data

    def read1(self, n=-1):
        data = self._resp.read1(n)
        if not data:
            self._finished = True
        return data

    def readline(self, limit=-1):
        data = self._resp.readline(limit)
        if not data:
            self._finished = True
        return data

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def __getattr__(self, name):
        # `_resp` 还没赋值时（__init__ 之前 / 反序列化）不能再走 getattr(self._resp)，否则无限递归
        if name.startswith("__") or name == "_resp":
            raise AttributeError(name)
        return getattr(self._resp, name)

    def _message_complete(self) -> bool:
        resp = self._resp
        try:
            if getattr(resp, "will_close", True):
                return False
            if getattr(resp, "chunked", False):
                return bool(resp.isclosed())
            return getattr(resp, "length", None) == 0 and bool(resp.isclosed())
        except Exception:  # noqa: BLE001
            return False

    def _drain(self) -> None:
        """调用方提前停读（SSE `[DONE]`）时，在短预算内把剩下的终止块读完。"""
        budget = drain_budget_s()
        if budget <= 0 or self._finished:
            return
        sock = socket_of(self._resp)
        deadline = time.monotonic() + budget
        total = 0
        try:
            while total < 65536:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                if sock is not None:
                    sock.settimeout(left)
                data = self._resp.read1(8192)
                if not data:
                    self._finished = True
                    self._lease.pool._bump("drained")
                    return
                total += len(data)
        except Exception:  # noqa: BLE001 - 读不完就销毁，不影响调用方
            self._finished = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._finalizer.detach()
        if not self._finished:
            self._drain()
        reusable = self._finished and self._message_complete()
        try:
            self._resp.close()
        except Exception:  # noqa: BLE001
            reusable = False
        self._lease.pool.release(self._lease, reusable=reusable)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


# ---------------------------------------------------------------------------
# 发请求（交付语义与 Marvis `_open_chat_stream_pooled` 一致）
# ---------------------------------------------------------------------------
class ConnectError(OSError):
    """checkout / 建连失败：请求没有发出，调用方按原来的网络错误处理（可换号）。"""


class DeliveryUnknown(Exception):
    """请求已开始写出后连接中断：上游可能已经收下。调用方不重试、不换号、不换出口。

    故意**不是** OSError 子类：调用方现有的 `except OSError: 换主机/换号` 不会吞掉它。
    """

    def __init__(self, message: str, *, phase: str = "", detail: str = ""):
        super().__init__(message)
        self.phase = phase
        self.detail = detail


_IDLE_CLOSE_ERRORS = (ConnectionResetError, BrokenPipeError, ConnectionAbortedError,
                      http.client.RemoteDisconnected, ssl.SSLEOFError,
                      ssl.SSLZeroReturnError)
_GRACEFUL_CLOSE_ERRORS = (http.client.RemoteDisconnected, ssl.SSLEOFError,
                          ssl.SSLZeroReturnError)


def is_idle_close_race(exc: BaseException, phase: str) -> bool:
    """复用连接上的失败是不是「对端刚好关掉了空闲连接」（纯函数，规则同 Marvis）。"""
    if not stale_retry_enabled():
        return False
    if isinstance(exc, TimeoutError):
        return False
    if phase == "send":
        return isinstance(exc, _IDLE_CLOSE_ERRORS)
    if phase != "response":
        return False
    if isinstance(exc, _GRACEFUL_CLOSE_ERRORS):
        return True
    return (isinstance(exc, http.client.BadStatusLine)
            and not isinstance(exc, ConnectionError)
            and not str(getattr(exc, "line", "") or "").strip("'\"\r\n "))


_SKIP_HEADERS = ("host", "connection", "content-length", "accept-encoding")


def pool_key(platform: str, url: str, account, proxy: str | None, *, trust_env: bool):
    """本次请求能否走池；能就返回 `(pool, key)`，不能返回 None。"""
    if not enabled(platform):
        return None
    account_key = str(account or "").strip()
    if not account_key or account_key in ("0", "None"):
        return None
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    route = resolve_proxy(url, proxy, trust_env=trust_env)
    if route is None:
        return None
    pool = pool_for(platform)
    if pool.at_capacity():
        pool._bump("fallback_capacity")
        return None
    try:
        port = int(parts.port or 443)
    except ValueError:
        return None
    return pool, Key(route, account_key, parts.hostname, port)


def open_pooled(platform: str, method: str, url: str, data, headers: dict, *,
                timeout: float, account, proxy: str | None = None,
                trust_env: bool = False):
    """走池发请求。返回 `PooledResponse`；不适用时返回 None（调用方走原 opener）。

    * 2xx：返回包装后的响应（调用方负责 close）；
    * >= 300：读完错误体后抛 `urllib.error.HTTPError`（与 urllib 一致，调用方原有
      的 HTTPError 分支照常工作）；3xx **不跟随**；
    * 建连失败：`ConnectError`；写出后中断：`DeliveryUnknown`。
    """
    picked = pool_key(platform, url, account, proxy, trust_env=trust_env)
    if picked is None:
        return None
    pool, key = picked
    parts = urlsplit(url)
    target = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    send_headers = {}
    for name, value in (headers or {}).items():
        if str(name).lower() in _SKIP_HEADERS:
            continue
        send_headers[str(name)] = str(value)
    body = data or b""
    if body and not any(str(n).lower() == "content-type" for n in send_headers):
        send_headers["Content-Type"] = "application/x-www-form-urlencoded"

    try:
        lease = pool.acquire(key, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise ConnectError("%s 出口连接失败：%s" % (platform, exc)) from exc
    if lease.reused and is_stale(lease.conn):
        pool.release(lease, reusable=False)
        try:
            lease = pool.acquire(key, timeout=timeout, force_fresh=True)
        except Exception as exc:  # noqa: BLE001
            raise ConnectError("%s 出口连接失败：%s" % (platform, exc)) from exc

    retried = False
    while True:
        phase = "send"
        try:
            sock = getattr(lease.conn, "sock", None)
            if sock is not None:
                sock.settimeout(timeout)
            lease.conn.request(method, target, body=body if method != "GET" else None,
                               headers=send_headers)
            phase = "response"
            resp = lease.conn.getresponse()
            break
        except Exception as exc:  # noqa: BLE001
            pool.release(lease, reusable=False)
            if not retried and lease.reused and is_idle_close_race(exc, phase):
                retried = True
                pool._bump("stale_retry")
                logger.warning("%s 复用连接被对端关闭（空闲竞态，phase=%s），换新连接重发一次 | %s: %s",
                               platform, phase, type(exc).__name__, str(exc)[:120])
                try:
                    lease = pool.acquire(key, timeout=timeout, force_fresh=True)
                except Exception as exc2:  # noqa: BLE001
                    raise ConnectError("%s 出口连接失败：%s" % (platform, exc2)) from exc2
                continue
            pool._bump("delivery_unknown")
            logger.warning("%s 上游交付未知（不换出口、不换号）| reused=%s | phase=%s | %s: %s",
                           platform, lease.reused, phase, type(exc).__name__, str(exc)[:160])
            raise DeliveryUnknown(
                "%s 上游交付状态未知：请求已发出，连接在收到响应头前中断" % platform,
                phase=phase,
                detail=("%s: %s" % (type(exc).__name__, str(exc)))[:300]) from exc

    status = int(getattr(resp, "status", 0) or 0)
    if status >= 300:
        payload = b""
        try:
            payload = resp.read()
        except Exception:  # noqa: BLE001
            pass
        try:
            resp.close()
        finally:
            pool.release(lease, reusable=False)
        raise urllib.error.HTTPError(url, status, getattr(resp, "reason", "") or "",
                                     resp.headers, io.BytesIO(payload))
    return PooledResponse(resp, lease)



def open_request(platform: str, req, *, timeout: float, account,
                 proxy: str | None = None, trust_env: bool = False):
    """`open_pooled` 的 `urllib.request.Request` 版本：头部取 `req.header_items()`。

    这样发出去的头与原 urllib 路径**逐字相同**（urllib 会把头名 `capitalize()`，
    并在没有 User-Agent 时补 `Python-urllib/x.y`，见 `OpenerDirector.addheaders`）。
    唯一的差别是不再带 `Connection: close`（这正是复用的前提）。
    """
    headers = dict(req.header_items())
    if not any(str(n).lower() == "user-agent" for n in headers):
        headers["User-agent"] = "Python-urllib/%d.%d" % sys.version_info[:2]
    return open_pooled(platform, req.get_method(), req.full_url, req.data, headers,
                       timeout=timeout, account=account, proxy=proxy,
                       trust_env=trust_env)
