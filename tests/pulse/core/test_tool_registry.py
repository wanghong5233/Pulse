from __future__ import annotations

import asyncio

from pulse.core.tool import ToolRegistry, tool


@tool(name="demo.echo", description="Echo input")
def _echo_tool(args: dict[str, object]) -> dict[str, object]:
    return {"echo": args}


def test_tool_registry_register_and_invoke() -> None:
    registry = ToolRegistry()
    registry.register_callable(_echo_tool)
    tools = registry.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "demo.echo"

    result = asyncio.run(registry.invoke("demo.echo", {"x": 1}))
    assert result == {"echo": {"x": 1}}


def test_tool_registry_rejects_duplicate_name() -> None:
    registry = ToolRegistry()
    registry.register_callable(_echo_tool)
    try:
        registry.register_callable(_echo_tool)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


@tool(
    name="demo.ctx",
    description="Demo tool for context.",
    when_to_use="输入 payload 返回回显, 纯读无副作用.",
    when_not_to_use="不用于持久化写入; 不用于外部 API 调用.",
)
def _ctx_tool(args: dict[str, object]) -> dict[str, object]:
    return {"ok": True, "args": args}


def test_tool_spec_carries_when_fields() -> None:
    """ToolUseContract §4.1: ToolSpec 必须透传 when_to_use / when_not_to_use"""
    spec = getattr(_ctx_tool, "__pulse_tool_spec__")
    assert spec.when_to_use.startswith("输入 payload")
    assert "持久化写入" in spec.when_not_to_use

    registry = ToolRegistry()
    registry.register_callable(_ctx_tool)
    tools = registry.list_tools()
    assert tools[0].when_to_use == spec.when_to_use
    assert tools[0].when_not_to_use == spec.when_not_to_use


def test_tool_registry_register_accepts_when_fields() -> None:
    """显式 register(...) 也要接收并存储 when_to_use / when_not_to_use."""
    registry = ToolRegistry()

    async def _h(_args):
        return {"ok": True}

    registry.register(
        name="demo.manual",
        handler=_h,
        description="manual register",
        when_to_use="USE_CASE_TEXT",
        when_not_to_use="AVOID_CASE_TEXT",
    )
    tools = registry.list_tools()
    assert tools[0].when_to_use == "USE_CASE_TEXT"
    assert tools[0].when_not_to_use == "AVOID_CASE_TEXT"


def test_tool_spec_defaults_empty_when_fields() -> None:
    """未声明 when_* 等价空字符串, 不破坏现有 @tool 用法."""
    spec = getattr(_echo_tool, "__pulse_tool_spec__")
    assert spec.when_to_use == ""
    assert spec.when_not_to_use == ""
