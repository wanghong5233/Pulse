from __future__ import annotations

import asyncio

from pulse.core.skill_generator import SkillGenerator
from pulse.core.tool import ToolRegistry


class _FakeCodegenLLMRouter:
    """Minimal LLM router that deterministically renders a safe tool stub."""

    def invoke_text(self, prompt_or_messages, *, route: str = "default") -> str:  # noqa: ANN001
        _ = prompt_or_messages
        if route == "classification":
            return "btc_monitor"
        return (
            "from __future__ import annotations\n"
            "from typing import Any\n"
            "from pulse.core.tool import tool\n\n"
            "@tool(name='btc_monitor', description='Monitor BTC price', "
            "schema={'type': 'object', 'properties': {'symbol': {'type': 'string'}}}, "
            "ring='ring1_builtin')\n"
            "def run(args: dict[str, Any]) -> dict[str, Any]:\n"
            "    symbol = str(args.get('symbol') or 'BTC').upper()\n"
            "    return {'tool': 'btc_monitor', 'symbol': symbol, 'price_usd': 65000.0}\n"
        )


def test_skill_generator_create_and_activate(tmp_path) -> None:
    registry = ToolRegistry()
    generator = SkillGenerator(
        tool_registry=registry,
        output_dir=str(tmp_path / "generated_skills"),
        llm_router=_FakeCodegenLLMRouter(),
    )
    record = generator.create_skill(prompt="I need to monitor BTC price changes")
    assert record["status"] in {"draft", "blocked"}
    assert record["tool_name"].startswith("btc_monitor")
    assert record["activation_required"] is True

    if record["status"] == "blocked":
        raise AssertionError("expected generated btc skill to pass sandbox")

    preview = generator.activate_skill(skill_id=record["skill_id"], confirm=False)
    assert preview["ok"] is False
    assert preview["needs_confirmation"] is True

    activated = generator.activate_skill(skill_id=record["skill_id"], confirm=True)
    assert activated["ok"] is True
    assert record["tool_name"] in activated["activated_tools"]

    output = asyncio.run(registry.invoke(record["tool_name"], {"symbol": "BTC"}))
    assert str(output["tool"]).startswith("btc_monitor")
    assert "price_usd" in output


def test_skill_generator_blocks_unsafe_override(tmp_path) -> None:
    registry = ToolRegistry()
    generator = SkillGenerator(
        tool_registry=registry,
        output_dir=str(tmp_path / "generated_skills"),
    )
    unsafe_code = (
        "import os\n"
        "from pulse.core.tool import tool\n"
        "@tool(name='unsafe_demo', description='unsafe')\n"
        "def run(args):\n"
        "    return os.system('whoami')\n"
    )
    record = generator.create_skill(
        prompt="unsafe test",
        tool_name="unsafe_demo",
        code_override=unsafe_code,
    )
    assert record["status"] == "blocked"
