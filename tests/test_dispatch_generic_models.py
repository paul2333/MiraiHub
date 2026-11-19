#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用路由名不该被「归属平台」抢走（2026-11-16，issue #38）。

## 事故

用户开「聚合模型」后用 `auto` 调 `/v1/chat/completions`，恒 HTTP 404；
同时 `/v1/models` 里看不到 `deepseek-v4.1-flash`（那一半在 1.1.3 由
`20254a0` 修掉，见 tests/test_marvis_model_naming.py）。

## 根因

`auto` / `default` / `chat` 这些名字是「让网关自己挑模型」，每个平台都可以
各自实现，`model_configs` 里谁登记了它纯属偶然 —— 但改道逻辑把它当普通
模型做归属反查，于是 `auto` 被判给**唯一**登记了 auto 行的平台（线上是
Qoder，29 个号），请求落到那边，上游不认这个词 → 404，用户还看不出落点。

## 修法

`_platform_for_model()` 认 `_GENERIC_MODEL_NAMES` 就返回 None：不按模型
选平台，交给当前平台自己解释（默认平台由主应用 `proxy.py` 的自动选模型
接住）。

2026-11-18 用户要求「按平台来，不要串平台」后：`_reroute_target()` 整段
删除（连同 dispatch 层的 404/502/503 跨平台重试），平台一旦解析出来就
钉死；本文件里「普通模型照旧跨平台改道」的断言随契约一起改掉。

运行：
  python tests/test_dispatch_generic_models.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from admin.db import Base  # noqa: E402
from admin.models import ModelConfig  # noqa: E402
from admin.platform import dispatch  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [!!] {name}  {detail}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    # 线上形态：auto 只有 Qoder 登记（29 个号），deepseek 系在 workbuddy 有号，
    # hunyuan-vision 只在 marvis 有。
    db.add_all([
        ModelConfig(platform="qoder", model_id="auto", enabled=1),
        ModelConfig(platform="workbuddy", model_id="deepseek-v4.1-flash", enabled=1),
        ModelConfig(platform="marvis", model_id="hunyuan-vision-2.0", enabled=1),
    ])
    db.commit()
    return db


def test_generic_names_not_rerouted(db) -> None:
    print("\n== 通用路由名不选平台（跨平台改道入口已删除）==")
    # 2026-11-18「按平台来，不要串平台」：改道入口整段删除。
    # 这里直接钉住「模块里不再有这个函数」，防止以后被顺手加回来。
    check("_reroute_target 已删除", not hasattr(dispatch, "_reroute_target"))
    for name in ("auto", "AUTO", " auto ", "default", "chat", "best", "fast"):
        picked = dispatch._platform_for_model(name, None)
        check(f"{name!r} 不选平台", picked is None, picked)


def test_normal_models_single_platform_only(db) -> None:
    print("\n== 普通模型只在目录里定位平台，不再跨平台改道 ==")
    # 契约变了：平台一旦解析出来就钉死，dispatch 层不再有「换个平台再试」。
    # 故意盯住模块属性：函数/常量被加回来 = 跨平台轮换复活。
    check("_reroute_target 已删除", not hasattr(dispatch, "_reroute_target"))
    check("_RETRYABLE_STATUS（跨平台重试状态集）已删除",
          not hasattr(dispatch, "_RETRYABLE_STATUS"))
    plats = dispatch._model_platforms(db, "hunyuan-vision-2.0")
    check("hunyuan-vision-2.0 只在 marvis 的清单里",
          plats == [("marvis", "")], plats)
    # 默认平台自己有的模型：目录归属仍只有 workbuddy，不会向外改道。
    same = dispatch._model_platforms(db, "deepseek-v4.1-flash")
    check("deepseek-v4.1-flash 只属于 workbuddy",
          same == [("workbuddy", "")], same)


def test_resolve_keeps_generic_on_default(db) -> None:
    print("\n== 开「无头分摊」时 auto 也固定落默认平台 ==")
    import json

    from admin.platform import registry

    class _Req:
        def __init__(self, model: str):
            self.headers = {}
            self.query_params = {}
            self.scope = {"_wb_body": json.dumps({"model": model}).encode()}

    saved = (dispatch._key_of, dispatch._disabled_platforms,
             dispatch._spread_unbound, dispatch._strategy_pick,
             dispatch._platform_for_model)
    dispatch._key_of = lambda _r: None
    dispatch._disabled_platforms = lambda: set()
    dispatch._spread_unbound = lambda: True
    dispatch._strategy_pick = lambda: ("loomy", "")
    dispatch._platform_for_model = lambda _m, _k: None
    try:
        check("auto -> 默认平台",
              dispatch.resolve(_Req("auto")) == (registry.DEFAULT_PLATFORM, ""),
              dispatch.resolve(_Req("auto")))
        check("普通模型仍按策略分摊",
              dispatch.resolve(_Req("deepseek-v4.1-flash")) == ("loomy", ""),
              dispatch.resolve(_Req("deepseek-v4.1-flash")))
    finally:
        (dispatch._key_of, dispatch._disabled_platforms,
         dispatch._spread_unbound, dispatch._strategy_pick,
         dispatch._platform_for_model) = saved


def main() -> int:
    db = make_db()
    try:
        test_generic_names_not_rerouted(db)
        test_normal_models_single_platform_only(db)
        test_resolve_keeps_generic_on_default(db)
    finally:
        db.close()
    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
