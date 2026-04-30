from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

ToolRing = Literal["ring1_builtin", "ring2_module", "ring3_mcp"]
ToolHandler = Callable[[dict[str, Any]], Any]
ExtractFactsFn = Callable[[Any], dict[str, Any]]


def _default_extract_facts(observation: Any) -> dict[str, Any]:
    """ToolUseContract §4.5 fallback — shallow whitelist projection.

    If a tool doesn't declare ``extract_facts``, the verifier still
    gets *something* structured: top-level scalar keys from a dict
    observation. Lists / dicts / None values are dropped to avoid
    dumping the whole observation into the judge prompt.
    """
    if not isinstance(observation, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in observation.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[str(key)] = value
    return out


@dataclass(slots=True)
class ToolSpec:
    """ToolUseContract §4.1 — 工具元数据契约。

    字段分工:
      description    : 这个工具**是什么** (一句话功能说明)
      when_to_use    : **什么场景应该调用**它 (触发条件, 正向示例)
      when_not_to_use: **什么场景不要调**它 (反例, 避免误触发)
      extract_facts  : (ToolUseContract §4.5, Contract C v2)
        可选钩子, 把工具 observation 转成扁平 {str: 标量} dict, 供
        CommitmentVerifier 的 Receipt.extracted_facts 使用. 不声明则
        回退 ``_default_extract_facts`` (顶层标量白名单).
        **约束**: 输出只允许 str/int/float/bool 标量; 禁止 PII / 长文本;
        <= 10 个键; 纯函数, 不抛异常.

    ``when_to_use`` / ``when_not_to_use`` 留空等价"未声明", PromptContract
    的 ``_section_tools`` 会回退到只渲染 ``description``. 新增工具应尽量声明,
    以减小 LLM 在同域多工具间误触发的概率.
    """

    name: str
    description: str
    when_to_use: str = ""
    when_not_to_use: str = ""
    ring: ToolRing = "ring1_builtin"
    schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    extract_facts: ExtractFactsFn | None = None


@dataclass(slots=True)
class _RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler


def tool(
    *,
    name: str | None = None,
    description: str = "",
    when_to_use: str = "",
    when_not_to_use: str = "",
    ring: ToolRing = "ring1_builtin",
    schema: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    extract_facts: ExtractFactsFn | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """Decorator to attach ToolSpec metadata to a callable.

    ``when_to_use`` / ``when_not_to_use`` 见 ``ToolSpec`` docstring
    (ToolUseContract §4.1). ``extract_facts`` 见 §4.5.
    """

    def _decorator(func: ToolHandler) -> ToolHandler:
        func_name = str(name or getattr(func, "__name__", "")).strip()
        if not func_name:
            raise ValueError("tool name must be non-empty")
        spec = ToolSpec(
            name=func_name,
            description=str(description or "").strip() or func_name,
            when_to_use=str(when_to_use or "").strip(),
            when_not_to_use=str(when_not_to_use or "").strip(),
            ring=ring,
            schema=dict(schema or {}),
            metadata=dict(metadata or {}),
            extract_facts=extract_facts,
        )
        setattr(func, "__pulse_tool_spec__", spec)
        return func

    return _decorator


class ToolRegistry:
    """Registry for built-in, module, and MCP tools."""

    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}

    def register(
        self,
        *,
        name: str,
        handler: ToolHandler,
        description: str,
        when_to_use: str = "",
        when_not_to_use: str = "",
        ring: ToolRing = "ring1_builtin",
        schema: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        extract_facts: ExtractFactsFn | None = None,
    ) -> None:
        safe_name = str(name or "").strip()
        if not safe_name:
            raise ValueError("tool name must be non-empty")
        if safe_name in self._tools:
            raise ValueError(f"duplicated tool name: {safe_name}")
        spec = ToolSpec(
            name=safe_name,
            description=str(description or "").strip() or safe_name,
            when_to_use=str(when_to_use or "").strip(),
            when_not_to_use=str(when_not_to_use or "").strip(),
            ring=ring,
            schema=dict(schema or {}),
            metadata=dict(metadata or {}),
            extract_facts=extract_facts,
        )
        self._tools[safe_name] = _RegisteredTool(spec=spec, handler=handler)

    def register_callable(self, func: ToolHandler) -> None:
        spec = getattr(func, "__pulse_tool_spec__", None)
        if not isinstance(spec, ToolSpec):
            raise ValueError("callable is missing @tool metadata")
        self.register(
            name=spec.name,
            handler=func,
            description=spec.description,
            when_to_use=spec.when_to_use,
            when_not_to_use=spec.when_not_to_use,
            ring=spec.ring,
            schema=spec.schema,
            metadata=spec.metadata,
            extract_facts=spec.extract_facts,
        )

    def get(self, name: str) -> ToolSpec | None:
        item = self._tools.get(str(name or "").strip())
        if item is None:
            return None
        return item.spec

    def list_tools(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in sorted(self._tools.keys())]

    async def invoke(self, name: str, args: dict[str, Any] | None = None) -> Any:
        safe_name = str(name or "").strip()
        entry = self._tools.get(safe_name)
        if entry is None:
            raise KeyError(f"tool not found: {safe_name}")
        payload = dict(args or {})

        # Keep the main event loop responsive for channel heartbeats.
        # Many module/MCP tools are sync + blocking (HTTP/browser I/O).
        if inspect.iscoroutinefunction(entry.handler):
            result = entry.handler(payload)
        else:
            result = await asyncio.to_thread(entry.handler, payload)
        if inspect.isawaitable(result):
            return await result
        return result
