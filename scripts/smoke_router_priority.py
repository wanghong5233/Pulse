"""Smoke test: LLMRouter.candidate_models 优先级 + reply_shaper 触发逻辑.

跑:
  cd Pulse && python scripts/smoke_router_priority.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 让 ``python scripts/smoke_router_priority.py`` 在没 install 的情况下也能跑.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _clean_env() -> None:
    for k in list(os.environ.keys()):
        if k.startswith(("MODEL_", "PULSE_MODEL_")):
            os.environ.pop(k, None)


def test_default_no_env() -> None:
    _clean_env()
    from pulse.core.llm.router import LLMRouter

    r = LLMRouter()
    cands = r.candidate_models("planning")
    assert cands[0] == "gpt-4.1", f"expected gpt-4.1 first, got {cands}"
    assert "qwen3-max" in cands, f"qwen3-max missing: {cands}"
    print("  [ok] no-env planning ->", cands[:3])


def test_global_env_does_not_override_route_default() -> None:
    """第二轮修正后: MODEL_PRIMARY 是全局兜底, 不会压住路由级代码默认.

    planning 路由代码默认 gpt-4.1 → 即使 MODEL_PRIMARY=gpt-4o-mini
    (用户想让便宜模型当兜底), 也不应替换 planning 的强模型诉求.
    gpt-4o-mini 应该作为全局兜底出现在 gpt-4.1/qwen3-max 之后.
    """
    _clean_env()
    os.environ["MODEL_PRIMARY"] = "gpt-4o-mini"
    from pulse.core.llm import router as router_mod
    import importlib
    importlib.reload(router_mod)
    r = router_mod.LLMRouter()
    cands = r.candidate_models("planning")
    assert cands[0] == "gpt-4.1", (
        f"route default gpt-4.1 should win over global MODEL_PRIMARY, got {cands}"
    )
    assert "gpt-4o-mini" in cands, (
        "global MODEL_PRIMARY should still appear as global fallback"
    )
    idx_primary = cands.index("gpt-4.1")
    idx_global = cands.index("gpt-4o-mini")
    assert idx_primary < idx_global, (
        f"route default should come before global env, got {cands}"
    )
    print("  [ok] global env is tiebreaker (not override) ->", cands[:4])


def test_global_env_fills_in_default_route() -> None:
    """'default' 路由本身没有强烈诉求, 全局 env 应该生效.

    DEFAULT_ROUTE_MODELS['default'] = (gpt-4.1, qwen3-max), 这里写了
    就不再被全局 env 覆盖 → 和 planning 行为一致.
    这个 case 保持与 planning 一致即可 (不再允许 global 覆盖路由特化默认).
    """
    _clean_env()
    os.environ["MODEL_PRIMARY"] = "deepseek-chat"
    from pulse.core.llm import router as router_mod
    import importlib
    importlib.reload(router_mod)
    r = router_mod.LLMRouter()
    cands = r.candidate_models("default")
    assert cands[0] == "gpt-4.1"
    assert "deepseek-chat" in cands  # 作为全局兜底仍应出现
    print("  [ok] default route (route default wins, global fallback kept) ->", cands[:4])


def test_route_env_beats_everything() -> None:
    _clean_env()
    os.environ["MODEL_PRIMARY"] = "gpt-4o-mini"
    os.environ["MODEL_ROUTE_PLANNING_PRIMARY"] = "o3-mini"
    from pulse.core.llm import router as router_mod
    import importlib
    importlib.reload(router_mod)
    r = router_mod.LLMRouter()
    cands = r.candidate_models("planning")
    assert cands[0] == "o3-mini", f"route env should win, got {cands}"
    print("  [ok] route env beats everything ->", cands[:3])


def test_reply_shaper_leak_detection() -> None:
    from pulse.core.brain import Brain
    from pulse.core.tool import ToolRegistry
    b = Brain(tool_registry=ToolRegistry())
    # clean natural-language reply
    leaked, _ = b._looks_leaking_internals("我帮你看了一下, 目前找到 3 个合适的远程岗位, 建议优先投阿里.")
    assert not leaked, "clean reply should not trigger reshape"

    # dict-style leak
    leaked, sample = b._looks_leaking_internals(
        "我调用了 job.greet.scan 工具: {\"keyword\":\"agent\",\"max_pages\":3,\"confirm_execute\":false}"
    )
    assert leaked, "dict-style leak should trigger"
    print("  [ok] reply_shaper leak detection ->", sample)


def test_preview_helpers() -> None:
    from pulse.core.llm.router import LLMRouter
    short = LLMRouter._make_preview("abc")
    assert short == "abc"
    long = LLMRouter._make_preview("x" * 600)
    assert long.endswith(" chars)") and long.startswith("x" * 500)
    # structured preview
    class Dummy:
        def model_dump(self, mode="json", exclude_none=True):
            return {"a": 1, "b": [1, 2, 3]}
    s = LLMRouter._structured_preview(Dummy())
    assert "\"a\"" in s and "1" in s
    print("  [ok] preview helpers")


if __name__ == "__main__":
    print("[smoke] router priority + reply_shaper + preview helpers")
    test_default_no_env()
    test_global_env_does_not_override_route_default()
    test_global_env_fills_in_default_route()
    test_route_env_beats_everything()
    test_reply_shaper_leak_detection()
    test_preview_helpers()
    print("[smoke] ALL PASS")
