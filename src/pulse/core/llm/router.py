"""Route-aware LLM facade for Pulse.

设计目标: 把 ``langchain_openai.ChatOpenAI`` 的异构 base_url / api_key / 模型
命名差异统一到一个极简的"按业务 route 取客户端"接口, 给 Brain 与所有业务域
复用。

业务层直接依赖 ``LLMRouter`` 三/四个方法, 不应该再 import ``langchain_openai``:

    * ``invoke_text(prompt, route)``       — 自由文本生成(最便宜, 失败抛异常)
    * ``invoke_json(prompt, route)``       — 结构化 JSON(剥 fence + ``json.loads``,
                                             解析失败返回 ``None``, 调用方降级)
    * ``invoke_structured(prompt, schema, route)`` — pydantic/JSON-schema 校验(
                                             成功率最高, 但模型必须支持
                                             ``with_structured_output``)
    * ``invoke_chat(messages, tools, route)`` — ReAct / Tool-use 专用

``route`` 约束见 ``DEFAULT_ROUTE_MODELS``; 常用取值:
``classification`` / ``planning`` / ``generation`` / ``cheap``。

*不要*在业务侧直接构造 ``ChatOpenAI`` 或读 ``OPENAI_API_KEY``, 所有凭证解析
都在 ``LLMRouter.resolve_api_config`` 里统一处理。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable
from urllib.parse import urlparse

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from ..event_types import EventTypes, make_payload
from ..logging_config import get_trace_id

logger = logging.getLogger(__name__)

EventEmitter = Callable[[str, dict[str, Any]], None]


def _endpoint_for_log(base_url: str) -> str:
    """Log API endpoint without path/query (no secrets)."""
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
        if p.netloc:
            return f"{p.scheme or 'https'}://{p.netloc}"
    except Exception:
        pass
    return raw[:48] + ("…" if len(raw) > 48 else "")


RouteDefaults = dict[str, tuple[str, str]]
ClientFactory = Callable[[str, str, str], Any]

# ─────────────────────────────────────────────────────────────────────
# Route -> (primary, fallback) 代码层保底默认.
#
# 设计原则(2026-04 重新校准):
#   - OpenAI 系列优先 (用户显式声明): tool-use / 指令遵循 / structured output
#     三项 Qwen 系中只有 qwen3-max 能追平, qwen-plus 不够强.
#   - fallback 统一到 qwen3-max: 国内网络 / OpenAI 不可用时, 至少是同档次模型,
#     而不是 qwen-plus 这种中档.
#   - cheap/classification: 用 gpt-4o-mini (便宜稳定) 或 qwen-turbo (便宜更便宜)
#
# 覆盖优先级 (见 ``candidate_models``):
#   1. MODEL_ROUTE_<ROUTE>_PRIMARY / _FALLBACK (按 route 精细覆盖)
#   2. MODEL_PRIMARY / MODEL_FALLBACK (全局覆盖)
#   3. 本字典的 (primary, fallback) — 最后的代码保底
# ─────────────────────────────────────────────────────────────────────
DEFAULT_ROUTE_MODELS: RouteDefaults = {
    "default":        ("gpt-4.1",      "qwen3-max"),
    "planning":       ("gpt-4.1",      "qwen3-max"),
    "generation":     ("gpt-4o-mini",  "qwen-plus"),
    "classification": ("gpt-4o-mini",  "qwen-turbo"),
    "cheap":          ("gpt-4o-mini",  "qwen-turbo"),
}


def _route_env_prefix(route: str) -> str:
    key = "".join(ch if ch.isalnum() else "_" for ch in str(route or "default").upper())
    return f"MODEL_ROUTE_{key}"


def _dedupe_models(models: list[str]) -> list[str]:
    return list(dict.fromkeys([m.strip() for m in models if isinstance(m, str) and m.strip()]))


def _read_env(*keys: str) -> str | None:
    for key in keys:
        value = os.getenv(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class LLMRouter:
    """Route-aware model router with fallback and structured output support."""

    def __init__(
        self,
        *,
        route_defaults: RouteDefaults | None = None,
        client_factory: ClientFactory | None = None,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        defaults = dict(DEFAULT_ROUTE_MODELS)
        if route_defaults:
            defaults.update(route_defaults)
        if "default" not in defaults:
            raise ValueError("route_defaults must include 'default'")
        self._route_defaults = defaults
        self._client_factory = client_factory
        self._event_emitter = event_emitter

    def bind_event_emitter(self, emitter: EventEmitter | None) -> None:
        """运行期注入事件总线, 见 ``server.py`` 装配."""
        self._event_emitter = emitter

    def _emit(self, event_type: str, **fields: Any) -> None:
        emitter = self._event_emitter
        if emitter is None:
            return
        try:
            payload = make_payload(
                trace_id=get_trace_id(),
                actor="llm_router",
                **fields,
            )
            emitter(event_type, payload)
        except Exception:  # pragma: no cover - 观测侧绝不阻塞主流程
            logger.debug("llm_router event emit failed", exc_info=True)

    def route_default_pair(self, route: str) -> tuple[str, str]:
        return self._route_defaults.get(route, self._route_defaults["default"])

    def candidate_models(self, route: str = "default") -> list[str]:
        """返回按**路由意图优先**排序的候选模型列表.

        修复历史:
          - 2026-04 第一轮: 把 ``route_defaults`` (代码硬编码) 放在 ``MODEL_PRIMARY``
            (用户 env) 前面 → 用户改不动. 当时反转为 "显式 env > 代码保底".
          - 2026-04 第二轮(当前): 发现 "全局 env > 路由特化默认" 会把
            ``MODEL_PRIMARY=gpt-4o-mini`` (用户意图:cheap 兜底)误用到 planning 路由
            (路由意图:gpt-4.1 强模型). 真正的 override 语义分层应该是:
              * 路由特化(route-specific): 既可能是用户显式 env, 也可能是代码作者
                按路由质量诉求精心挑选的默认;二者共属"路由级"
              * 全局(global): 作为"所有没有路由特化的场景"的兜底, **不应压住
                路由特化默认**
            所以本次反转后的优先级把"路由级代码默认"放在"全局 env"之前,
            用户可以通过 ``MODEL_ROUTE_<ROUTE>_PRIMARY`` 显式覆盖路由默认.

        优先级 (高 → 低):
          1. ``MODEL_ROUTE_<ROUTE>_PRIMARY`` (env, 路由级显式覆盖)
          2. ``MODEL_ROUTE_<ROUTE>_FALLBACK`` (env)
          3. ``DEFAULT_ROUTE_MODELS[route]`` primary (代码的路由级意图)
          4. ``DEFAULT_ROUTE_MODELS[route]`` fallback (代码的路由级意图)
          5. ``MODEL_PRIMARY`` (env, 全局兜底)
          6. ``MODEL_FALLBACK`` (env)
          7. ``DEFAULT_ROUTE_MODELS["default"]`` 的 primary / fallback (最终兜底)
        """
        normalized = str(route or "default").strip() or "default"
        prefix = _route_env_prefix(normalized)

        route_primary = _read_env(f"{prefix}_PRIMARY", f"PULSE_{prefix}_PRIMARY")
        route_fallback = _read_env(f"{prefix}_FALLBACK", f"PULSE_{prefix}_FALLBACK")
        global_primary = _read_env("MODEL_PRIMARY", "PULSE_MODEL_PRIMARY")
        global_fallback = _read_env("MODEL_FALLBACK", "PULSE_MODEL_FALLBACK")
        default_primary, default_fallback = self.route_default_pair(normalized)
        base_default_primary, base_default_fallback = self._route_defaults["default"]

        return _dedupe_models(
            [
                route_primary,
                route_fallback,
                default_primary,
                default_fallback,
                global_primary,
                global_fallback,
                base_default_primary,
                base_default_fallback,
            ]
        )

    def resolve_api_config(self, model: str = "") -> tuple[str, str]:
        """Resolve (base_url, api_key) with auto-detection by model name prefix."""
        model_lower = model.strip().lower()

        if model_lower.startswith(("gpt-", "gpt4", "o1-", "o3-", "o4-", "chatgpt")):
            key = _read_env("OPENAI_API_KEY")
            if key:
                return _read_env("OPENAI_BASE_URL") or "https://api.openai.com/v1", key

        if model_lower.startswith("qwen"):
            key = _read_env("DASHSCOPE_API_KEY", "QWEN_API_KEY")
            if key:
                url = _read_env("OPENAI_COMPAT_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
                return url, key

        if model_lower.startswith("deepseek"):
            key = _read_env("DEEPSEEK_API_KEY")
            if key:
                return _read_env("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1", key

        pulse_key = _read_env("PULSE_MODEL_API_KEY")
        if pulse_key:
            base_url = _read_env("PULSE_MODEL_BASE_URL", "OPENAI_COMPAT_BASE_URL")
            return base_url or "https://api.openai.com/v1", pulse_key

        openai_key = _read_env("OPENAI_API_KEY")
        if openai_key:
            return _read_env("OPENAI_BASE_URL") or "https://api.openai.com/v1", openai_key

        dashscope_key = _read_env("DASHSCOPE_API_KEY", "QWEN_API_KEY")
        if dashscope_key:
            url = _read_env("OPENAI_COMPAT_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            return url, dashscope_key

        deepseek_key = _read_env("DEEPSEEK_API_KEY")
        if deepseek_key:
            return _read_env("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1", deepseek_key

        raise RuntimeError(
            "No model API key found. Set OPENAI_API_KEY, "
            "DASHSCOPE_API_KEY/QWEN_API_KEY, or DEEPSEEK_API_KEY."
        )

    def build_client(self, model: str) -> Any:
        base_url, api_key = self.resolve_api_config(model)
        logger.debug(
            "llm_client_built model=%s endpoint=%s",
            model,
            _endpoint_for_log(base_url),
        )
        if self._client_factory is not None:
            return self._client_factory(model, base_url, api_key)
        return ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=0.1,
            timeout=60,
            max_retries=1,
        )

    @staticmethod
    def coerce_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "\n".join(parts).strip()
        return str(content)

    @staticmethod
    def _make_preview(text: str, *, max_chars: int = 500) -> str:
        """截取字符串前 N 字符用于事件 payload; 保留完整内容时不加省略号.

        用于 ``llm.invoke.ok`` 事件的 ``content_preview`` / ``out_preview`` 字段,
        让事后审计能真正还原 agent 说了什么 (老版本只记了 chars 数, 没法回放).
        """
        s = str(text or "")
        if len(s) <= max_chars:
            return s
        return s[:max_chars] + f"...(+{len(s) - max_chars} chars)"

    @staticmethod
    def _structured_preview(result: Any, *, max_chars: int = 500) -> str:
        """把 structured output 结果(pydantic/dict/list/...)序列化成一段短摘要."""
        try:
            if hasattr(result, "model_dump"):
                payload = result.model_dump(mode="json", exclude_none=True)
            elif hasattr(result, "dict"):
                payload = result.dict()
            else:
                payload = result
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
        return LLMRouter._make_preview(text, max_chars=max_chars)

    def invoke_structured(self, prompt_value: Any, schema: type[Any], *, route: str = "default") -> Any:
        candidates = self.candidate_models(route)
        logger.debug("llm_invoke_start kind=structured route=%s candidates=%s", route, candidates)
        errors: list[str] = []
        for model in candidates:
            try:
                llm = self.build_client(model).with_structured_output(schema)
                result = llm.invoke(prompt_value)
                schema_name = getattr(schema, "__name__", str(schema))
                logger.info(
                    "llm_invoke_ok kind=structured route=%s model=%s schema=%s",
                    route,
                    model,
                    schema_name,
                )
                self._emit(
                    EventTypes.LLM_INVOKE_OK,
                    kind="structured",
                    route=route,
                    model=model,
                    schema=schema_name,
                    content_preview=self._structured_preview(result),
                )
                return result
            except Exception as exc:  # pragma: no cover - environment dependent
                msg = str(exc)[:400]
                errors.append(f"{model}: {msg}")
                logger.warning(
                    "llm_attempt_failed kind=structured route=%s model=%s err=%s",
                    route,
                    model,
                    msg,
                )
        logger.error(
            "llm_invoke_exhausted kind=structured route=%s tried=%s",
            route,
            candidates,
        )
        self._emit(
            EventTypes.LLM_INVOKE_EXHAUSTED,
            kind="structured",
            route=route,
            tried=list(candidates),
            errors=errors,
        )
        raise RuntimeError(
            f"All models failed for structured output (route={route}): " + " | ".join(errors)
        )

    def invoke_text(self, prompt_value: Any, *, route: str = "default") -> str:
        candidates = self.candidate_models(route)
        logger.debug("llm_invoke_start kind=text route=%s candidates=%s", route, candidates)
        errors: list[str] = []
        for model in candidates:
            try:
                message = self.build_client(model).invoke(prompt_value)
                if isinstance(message, AIMessage):
                    out = self.coerce_text(message.content)
                else:
                    out = self.coerce_text(message)
                logger.info("llm_invoke_ok kind=text route=%s model=%s out_chars=%d", route, model, len(out))
                self._emit(
                    EventTypes.LLM_INVOKE_OK,
                    kind="text",
                    route=route,
                    model=model,
                    out_chars=len(out),
                    content_preview=self._make_preview(out),
                )
                return out
            except Exception as exc:  # pragma: no cover - environment dependent
                msg = str(exc)[:400]
                errors.append(f"{model}: {msg}")
                logger.warning(
                    "llm_attempt_failed kind=text route=%s model=%s err=%s",
                    route,
                    model,
                    msg,
                )
        logger.error("llm_invoke_exhausted kind=text route=%s tried=%s", route, candidates)
        self._emit(
            EventTypes.LLM_INVOKE_EXHAUSTED,
            kind="text",
            route=route,
            tried=list(candidates),
            errors=errors,
        )
        raise RuntimeError(f"All models failed for text output (route={route}): " + " | ".join(errors))

    def invoke_chat(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        route: str = "default",
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AIMessage:
        """Chat-style invocation with optional tool calling for ReAct loops.

        ``tool_choice`` (Contract B / L4, see ADR-001 §3.2): caller-specified
        tool-use policy forwarded verbatim to ``client.bind_tools``. Accepted
        shapes follow provider conventions — ``"auto"`` / ``"required"`` /
        ``"none"`` / provider-specific dict (e.g. OpenAI's
        ``{"type": "function", "function": {"name": "t"}}``).

        Invariants (enforced by ``test_router_tool_choice.py``):

        * ``tool_choice=None`` (default) → router does **not** pass any
          ``tool_choice`` kwarg to ``bind_tools``; provider default applies.
        * Any non-None value is forwarded unchanged — router is a transport,
          not a translator.
        * When ``tools`` is empty/None, ``bind_tools`` is skipped entirely,
          but the audit event still records the caller's intent (otherwise
          we'd hide the "Brain forced required with no tools" misconfig).
        """
        candidates = self.candidate_models(route)
        n_tools = len(tools or [])
        logger.debug(
            "llm_invoke_start kind=chat route=%s candidates=%s messages=%d tools=%d tool_choice=%s",
            route,
            candidates,
            len(messages),
            n_tools,
            tool_choice,
        )
        errors: list[str] = []
        for model in candidates:
            try:
                client = self.build_client(model)
                if tools:
                    if tool_choice is not None:
                        client = client.bind_tools(tools, tool_choice=tool_choice)
                    else:
                        client = client.bind_tools(tools)
                result = client.invoke(messages)
                if isinstance(result, AIMessage):
                    n_calls = len(result.tool_calls) if getattr(result, "tool_calls", None) else 0
                    content_len = len(self.coerce_text(result.content or ""))
                    logger.info(
                        "llm_invoke_ok kind=chat route=%s model=%s tool_calls=%d content_chars=%d",
                        route,
                        model,
                        n_calls,
                        content_len,
                    )
                    tool_call_summary: list[dict[str, Any]] = []
                    if n_calls:
                        for tc in (result.tool_calls or [])[:8]:
                            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "?")
                            args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                            tool_call_summary.append(
                                {
                                    "name": str(name or "?"),
                                    "args_preview": self._structured_preview(args, max_chars=200),
                                }
                            )
                    self._emit(
                        EventTypes.LLM_INVOKE_OK,
                        kind="chat",
                        route=route,
                        model=model,
                        tool_calls=n_calls,
                        content_chars=content_len,
                        messages=len(messages),
                        tools=n_tools,
                        tool_choice_applied=tool_choice,
                        content_preview=self._make_preview(self.coerce_text(result.content or "")),
                        tool_call_preview=tool_call_summary,
                    )
                    return result
                out = AIMessage(content=self.coerce_text(result))
                logger.info(
                    "llm_invoke_ok kind=chat route=%s model=%s tool_calls=0 content_chars=%d",
                    route,
                    model,
                    len(out.content or ""),
                )
                self._emit(
                    EventTypes.LLM_INVOKE_OK,
                    kind="chat",
                    route=route,
                    model=model,
                    tool_calls=0,
                    content_chars=len(out.content or ""),
                    messages=len(messages),
                    tools=n_tools,
                    tool_choice_applied=tool_choice,
                    content_preview=self._make_preview(out.content or ""),
                )
                return out
            except Exception as exc:
                msg = str(exc)[:400]
                errors.append(f"{model}: {msg}")
                logger.warning(
                    "llm_attempt_failed kind=chat route=%s model=%s err=%s",
                    route,
                    model,
                    msg,
                )
        logger.error(
            "llm_invoke_exhausted kind=chat route=%s tried=%s",
            route,
            candidates,
        )
        self._emit(
            EventTypes.LLM_INVOKE_EXHAUSTED,
            kind="chat",
            route=route,
            tried=list(candidates),
            errors=errors,
            messages=len(messages),
            tools=n_tools,
        )
        raise RuntimeError(f"All models failed for chat (route={route}): " + " | ".join(errors))

    # ── 便利 helper(业务层优先使用)────────────────────────────

    @staticmethod
    def strip_code_fence(text: str) -> str:
        """剥离 ``` ``` 代码围栏, 兼容可选语言标识。

        LLM 经常把 JSON 包在 ```json ... ``` 或 ``` ... ``` 里; 所有业务层想
        自己 parse JSON 之前都要先过这一步, 所以抽成公共 helper 避免每个
        组件复制粘贴相同的逻辑。对不带围栏的文本是 no-op。
        """
        cleaned = str(text or "").strip()
        if not cleaned.startswith("```"):
            return cleaned
        lines = cleaned.split("\n")
        if len(lines) < 2:
            return cleaned
        body = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        return "\n".join(body).strip()

    def invoke_json(
        self,
        prompt_value: Any,
        *,
        route: str = "default",
        default: Any = None,
    ) -> Any:
        """便利方法: 调用 LLM 并把结果解析为 JSON 对象。

        流程: ``invoke_text`` → ``strip_code_fence`` → ``json.loads``; 任何
        步骤失败都返回 ``default`` (默认 ``None``), 由调用方决定降级策略
        (通常是 heuristic fallback)。**不抛异常**, 这是它和 ``invoke_text``
        的主要区别 — 业务层很少需要区分 "LLM 宕机" vs "LLM 返回乱码"。

        想要严格的 schema 校验请直接用 :meth:`invoke_structured`。
        """
        try:
            raw = self.invoke_text(prompt_value, route=route)
        except Exception as exc:  # pragma: no cover - 环境相关
            logger.warning("LLMRouter.invoke_json: text invocation failed (%s)", exc)
            return default
        cleaned = self.strip_code_fence(raw)
        if not cleaned:
            return default
        try:
            return json.loads(cleaned)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.info(
                "LLMRouter.invoke_json: non-JSON response (route=%s, err=%s, head=%r)",
                route,
                exc,
                cleaned[:160],
            )
            return default
