"""IntentSpec → as_tools() 透传 when_* 字段单测 (ADR-001 契约 A).

覆盖 `ModuleRegistry._build_intent_tools` 把 IntentSpec 里的
`when_to_use / when_not_to_use` 带到 tool descriptor, 这是 Ring 2 module-as-tool
接入 PromptContract 三段式渲染的唯一路径。
"""

from __future__ import annotations

from pulse.core.module import BaseModule, IntentSpec, ModuleRegistry


class _ProbeModule(BaseModule):
    name = "probe"
    description = "probe module used only in unit tests"
    route_prefix = "/api/modules/probe"

    def register_routes(self, router) -> None:  # type: ignore[override]
        # 测试 module 不暴露 HTTP 路由; 空实现满足抽象基类即可.
        return None

    intents = [
        IntentSpec(
            name="probe.do",
            description="Do a side-effecting action.",
            parameters_schema={"type": "object", "properties": {}},
            handler=lambda **_: {"ok": True},
            when_to_use="写入外部系统; 参数 target 必填.",
            when_not_to_use="只读用 probe.read.",
            mutates=True,
            risk_level=2,
        ),
        IntentSpec(
            name="probe.bare",
            description="Legacy intent without when_* declarations.",
            parameters_schema={"type": "object", "properties": {}},
            handler=lambda **_: {"ok": True},
        ),
    ]


def test_build_intent_tools_propagates_when_fields() -> None:
    registry = ModuleRegistry()
    registry.register(_ProbeModule())
    tools = {t["name"]: t for t in registry.as_tools()}

    assert "probe.do" in tools
    do = tools["probe.do"]
    assert do["when_to_use"] == "写入外部系统; 参数 target 必填."
    assert do["when_not_to_use"] == "只读用 probe.read."
    assert do["ring"] == "ring2_module"
    assert do["metadata"]["mutates"] is True

    bare = tools["probe.bare"]
    assert bare["when_to_use"] == ""
    assert bare["when_not_to_use"] == ""
