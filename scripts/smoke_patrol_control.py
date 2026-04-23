"""Patrol conversational control plane dry-run smoke (ADR-004 §6.1 §6.1.8).

## 定位

端到端演练 "Brain → system.patrol.* IntentSpec → AgentRuntime.*_patrol
API → SchedulerEngine" 这条完整路径, **不**依赖真实 BOSS 浏览器 / 真实
Playwright / 真实 LLM; 用一条 noop patrol 把 list/status/enable/disable/
trigger 五个动作逐个跑一遍, 把每步的实际 runtime 状态打印出来, 让
reviewer 肉眼确认:

  1. IntentSpec handler 真的调到了 runtime
  2. runtime 真的改了 SchedulerEngine 状态
  3. lifecycle 事件真的发出来了
  4. heartbeat carve-out 真的生效了

## 为什么需要 smoke (不是单测就够)

单测断的是 "handler → runtime 一跳内行为", smoke 要断的是"跨层组装后
整体心智一致". 宪法 §测试分层#3 要求合入前对真实调用链走一遍, 这个
smoke 就是那道闸. 它不在 CI 跑, 用户审过打印再合 PR.

## 使用

    python scripts/smoke_patrol_control.py

无参数; 默认演练全部 5 个动作 + 打印事件时间线.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pulse.core.runtime import AgentRuntime, RuntimeConfig  # noqa: E402
from pulse.modules.system.patrol.module import PatrolControlModule  # noqa: E402


_BANNER_WIDTH = 72


def _banner(text: str) -> None:
    line = "=" * _BANNER_WIDTH
    print(line)
    for row in text.strip().splitlines():
        print(row)
    print(line)


def _print_step(step: str, payload: dict) -> None:
    print(f"\n--- {step} ---")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _print_events_since(events: list[tuple[str, dict]], last_idx: int) -> int:
    new_events = events[last_idx:]
    if not new_events:
        print("(no new events)")
    else:
        for etype, payload in new_events:
            print(f"  event: {etype:<40} payload={json.dumps(payload, ensure_ascii=False, default=str)}")
    return len(events)


def main() -> int:
    events: list[tuple[str, dict]] = []

    def _emitter(etype: str, payload: dict) -> None:
        events.append((etype, dict(payload)))

    runtime = AgentRuntime(event_emitter=_emitter, config=RuntimeConfig())
    runtime.register_patrol(
        name="demo.autoreply",
        handler=lambda ctx: {"ok": True, "note": f"ran ctx={ctx.task_id}"},
        peak_interval=60,
        offpeak_interval=120,
        enabled=False,
        active_hours_only=False,
        token_budget=1000,
    )

    module = PatrolControlModule()
    module.bind_runtime(runtime)

    _banner(
        "Patrol control-plane smoke (ADR-004 §6.1)\n"
        "  runtime   = fresh AgentRuntime (no scheduler thread started)\n"
        "  patrol    = 'demo.autoreply' (noop handler, enabled=False)\n"
        "  mode      = dry (no real BOSS, no real LLM)\n"
        "  audience  = review of full Brain → IntentSpec → runtime chain"
    )

    last = 0

    _print_step("step 1: system.patrol.list (before enable)", module._list_handler())
    last = _print_events_since(events, last)

    _print_step("step 2: system.patrol.status(demo.autoreply)", module._status_handler(name="demo.autoreply"))
    last = _print_events_since(events, last)

    _print_step("step 3: system.patrol.enable(demo.autoreply)", module._enable_handler(name="demo.autoreply"))
    last = _print_events_since(events, last)

    _print_step("step 4: system.patrol.trigger(demo.autoreply)", module._trigger_handler(name="demo.autoreply"))
    last = _print_events_since(events, last)

    _print_step("step 5: system.patrol.status(demo.autoreply) — after trigger", module._status_handler(name="demo.autoreply"))
    last = _print_events_since(events, last)

    _print_step("step 6: system.patrol.disable(demo.autoreply)", module._disable_handler(name="demo.autoreply"))
    last = _print_events_since(events, last)

    _print_step("step 7: list_patrols (final)", {"patrols": runtime.list_patrols()})

    heartbeat_probe = module._status_handler(name=runtime._heartbeat_task_name)
    _print_step(
        "invariant probe: status(__runtime_heartbeat__) — must fail-loud (ADR-004 §6.1.7 #1)",
        heartbeat_probe,
    )
    assert heartbeat_probe["ok"] is False, (
        "Invariant violated: heartbeat snapshot leaked through system.patrol.status"
    )

    _banner(
        "smoke complete — review the per-step outputs and event stream above.\n"
        "Expected highlights:\n"
        "  * step 1: total=1, demo.autoreply enabled=False\n"
        "  * step 3: emits runtime.patrol.lifecycle.enabled\n"
        "  * step 4: emits lifecycle.triggered + patrol.started + patrol.completed\n"
        "  * step 4 return last_outcome=completed\n"
        "  * step 6: emits runtime.patrol.lifecycle.disabled\n"
        "  * invariant probe: ok=False on heartbeat — carve-out holds"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
