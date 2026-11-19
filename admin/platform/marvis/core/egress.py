"""Marvis 出口池：按账号选出口，避免所有账号共用同一个 IP。

2026-11-18：WARP 双出口（wireproxy 内建 HTTP 代理）。
  w1 -> http://172.20.0.1:42001   出口 104.28.196.199
  w4 -> http://172.20.0.1:42004   出口 104.28.228.198

为什么按账号：上游会按「同一出口 + 多账号」聚簇。
"""
import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

_POOL_PATH = os.environ.get("MARVIS_EGRESS_POOL") or os.path.join(
    os.path.dirname(__file__), "egress_pool.json")
_CACHE = None


def _pool():
    global _CACHE
    if _CACHE is None:
        try:
            with open(_POOL_PATH, encoding="utf-8") as fh:
                _CACHE = [str(x) for x in json.load(fh).get("exits", []) if x]
            logger.info("marvis 出口池已加载：%d 个出口", len(_CACHE))
        except Exception as exc:  # noqa: BLE001
            logger.warning("marvis 出口池加载失败（回退平台级代理）：%s", exc)
            _CACHE = []
    return _CACHE


def pick(account_key) -> str:
    """按账号标识稳定选一个出口（同一账号永远同一出口）。"""
    pool = _pool()
    if not pool:
        return ""
    key = str(account_key or "")
    idx = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(pool)
    return pool[idx]
