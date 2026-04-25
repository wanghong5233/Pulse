from __future__ import annotations

import importlib
import inspect
import json
import logging
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from uuid import uuid4

from fastapi import APIRouter, FastAPI

if TYPE_CHECKING:
    from .profile.base import DomainProfileManager
    from .runtime import AgentRuntime

logger = logging.getLogger(__name__)

EventEmitter = Callable[[str, dict[str, Any]], None]


def _preview_payload(payload: dict[str, Any] | None, *, max_chars: int = 400) -> str:
    """Compact one-line payload rendering for ``emit_stage_event`` log mirror.

    We intentionally keep this dumb: ``json.dumps`` with ``default=str`` so
    datetime / enum / dataclass show up; truncated to ``max_chars`` so a
    4KB JSON blob does not pollute one log line.
    """
    if not payload:
        return "{}"
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        text = str(payload)
    if len(text) > max_chars:
        return text[:max_chars] + "...(truncated)"
    return text

# Intent handler 签名: 接结构化 kwargs, 返回任意可序列化值(同步或异步均可)。
IntentHandler = Callable[..., Any | Awaitable[Any]]


@dataclass(slots=True)
class IntentSpec:
    """单个 intent 的工具规格,直接映射为 LLM tool_use schema。

    见 ``docs/Pulse-DomainMemory与Tool模式.md`` §4.2。

    与旧的粗粒度 ``handle_intent(intent, text, metadata)`` 路径的区别:
      - 每个 intent 是一个**独立的 LLM tool**,有自己的 JSON Schema
      - LLM 直接从自然语言抽取结构化 kwargs,Brain 不做 regex 解析
      - Module 不再需要手写 intent 分发逻辑

    Fields:
      name: 完整 tool 名, 必须形如 ``<domain>.<capability>.<action>``,
            例如 ``job.profile.block_keyword``。
      description: 给 LLM 的语义说明。推荐**多写"当用户说X时使用"的触发提示**,
                   降低 Brain 误判率。
      parameters_schema: OpenAI 兼容的 JSON Schema (``{"type":"object", ...}``),
                         供 Brain 生成结构化 tool_use 参数。
      handler: 接结构化 kwargs 的处理函数(同步或异步均可)。签名: ``(**kwargs) -> Any``。
      examples: few-shot 示例,形如 ``[{"input": "我不投大厂", "kwargs": {"keyword": "大厂"}}]``。
                Brain 可在 system prompt 中展示(当前 P0 不强制渲染)。
      mutates: 是否修改 memory。``True`` 时 Brain ReAct loop 会在调用后强制刷新 snapshot。
      requires_confirmation: 是否需要 HITL 确认(高风险操作)。
      risk_level: 0=安全,1=中风险(默认需回执),2=高风险(需 HITL)。
      tags: 自由标签,用于筛选/审计。
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: IntentHandler
    # ToolUseContract §4.1 契约 A: 让 module 以 intent 粒度声明
    # 「什么时候该触发 / 不该触发」, 由 PromptContractBuilder 渲染到 tool 卡片.
    # 留空等价「未声明」, 不会阻塞注册.
    when_to_use: str = ""
    when_not_to_use: str = ""
    examples: list[dict[str, Any]] = field(default_factory=list)
    mutates: bool = False
    requires_confirmation: bool = False
    risk_level: int = 0
    tags: list[str] = field(default_factory=list)
    # ToolUseContract §4.5 (Contract C v2): 把 observation 投影成扁平
    # {str: 标量} dict, 供 CommitmentVerifier.TurnEvidence 的
    # Receipt.extracted_facts 使用. 约束: 只允许 str/int/float/bool;
    # 禁止 PII/长文本; <=10 个键; 纯函数, 不抛异常.
    # 留空 → 回退 ToolSpec._default_extract_facts (顶层标量白名单).
    extract_facts: Callable[[Any], dict[str, Any]] | None = None


def _preview_payload(value: object, *, max_chars: int = 500) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = text.strip()
    if len(text) > max_chars:
        return text[:max_chars] + "...(truncated)"
    return text


class BaseModule(ABC):
    """Base contract for all Pulse business modules.

    除原有的 REST 路由 + ``handle_intent`` 粗粒度入口外, 子类可通过
    ``intents`` 属性暴露**细粒度 Intent 工具**(见 ``IntentSpec``),
    被 ``ModuleRegistry.as_tools()`` 转换为 Brain 可直接 tool_use 的
    LLM tools。

    迁移策略:
      - 新模块: 只填 ``intents``, 不再写 ``handle_intent`` 分发逻辑。
      - 旧模块: ``intents`` 留空即可, 继续走 ``handle_intent``(粗粒度)。
      - 共存: 两者都填时两个路径同时暴露。
    """

    name: str = ""
    description: str = ""
    route_prefix: str | None = None
    tags: list[str] | None = None

    # 子类覆盖(class-level 或 __init__ 里赋值)。
    intents: list[IntentSpec] = []

    def __init__(self) -> None:
        self._event_emitter: EventEmitter | None = None
        self._runtime: AgentRuntime | None = None

    @abstractmethod
    def register_routes(self, router: APIRouter) -> None:
        """Register module routes into the provided router."""

    def on_startup(self) -> None:
        """Optional startup hook."""

    def on_shutdown(self) -> None:
        """Optional shutdown hook."""

    def handle_intent(
        self,
        intent: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Optional intent handling hook for channel ingress routing."""
        return None

    def get_profile_manager(self) -> "DomainProfileManager | None":
        """可选 domain profile manager: 由 ``ProfileCoordinator`` 统一管理。

        有可持久化人类画像的 module (当前只有 ``job``) 覆盖本方法,
        返回一个 ``DomainProfileManager`` 子类实例, 负责:
          - 启动时从 ``yaml`` 种 memory (``load``)
          - Brain mutation 成功后把 memory 写回 yaml (``sync_to_yaml``)
          - CLI ``pulse profile {dump,export,reset}`` 的 domain 入口

        约定:
          - ``manager.domain`` 必须与该 module intent tool 的 ``<domain>.*`` 前缀一致,
            否则 ``after_tool_use`` hook 无法正确路由
          - 返回 ``None`` (默认) 表示该 module 无需 profile 持久化

        见 ``docs/Pulse-DomainMemory与Tool模式.md`` "Domain Profile 管理" 章节。
        """
        return None

    def get_preference_appliers(self) -> "list[Any]":
        """可选 domain preference appliers: 返回 ``list[DomainPreferenceApplier]``.

        由 ``SoulEvolutionEngine`` 的 ``DomainPreferenceDispatcher`` 统一收集,
        把 ``PreferenceExtractor`` 抽出的业务域偏好 (如 job.* hard_constraint /
        memory item) 架构强制持久化到对应 DomainMemory.

        约定:
          - 每个 applier 必须有 ``domain`` 属性和 ``supported_ops`` 元组
          - applier 实例化成本应较低 (dispatcher 不缓存, 每次 reflection 用一次)
          - 默认返回空列表 — 不需要偏好持久化的 module 不用覆盖

        见 ``docs/Pulse-DomainMemory与Tool模式.md`` §2.2 以及
        ``pulse.core.learning.domain_preference_dispatcher``.
        """
        return []

    def get_domain_snapshot_provider(self) -> Callable[[Any], str] | None:
        """可选 domain snapshot provider: 返回一个 ``(TaskContext) -> str`` 函数。

        函数返回的 markdown 片段会被 ``PromptContractBuilder`` 追加到 Brain 的
        system prompt 中 (``interactiveTurn`` / ``taskPrompt`` 两种模式),
        用于把领域 memory 状态(如 Job 域的黑名单 / 偏好) 暴露给 Brain。

        默认返回 None — 不需要 domain snapshot 的 module 不用覆盖。
        约定:
          - provider 必须是**只读的**, 不得改状态
          - 异常应当自吞并返回空字符串; Builder 会进一步兜底
          - 返回内容尽量短 (< 2KB), 避免撑爆 system prompt

        见 ``docs/Pulse-DomainMemory与Tool模式.md`` §3.3 / §5.3。
        """
        return None

    def bind_runtime(self, runtime: AgentRuntime | None) -> None:
        """Inject the AgentRuntime so the module can register patrol tasks
        during on_startup.  Called by server.py before on_startup."""
        self._runtime = runtime

    def bind_event_emitter(self, emitter: EventEmitter | None) -> None:
        self._event_emitter = emitter

    def attach_safety_plane(
        self,
        *,
        suspended_store: Any,
        workspace_id: str,
        mode: str,
    ) -> None:
        """延迟注入 SafetyPlane 依赖.

        ModuleRegistry.discover() 在 ``workspace_memory`` / ``event_bus`` 就绪
        **之前**就发生, 所以 module service 无法在 ``__init__`` 里直接拿到
        ``SuspendedTaskStore``. server.py 先走完 bootstrap, 再回头给每个需要
        授权闸门的 module 调一次 ``attach_safety_plane`` 补齐依赖.

        默认实现 no-op —— 大部分 module 不产生 side-effect, 也就不需要 policy
        gate. 需要做 policy 检查的 module 重写此方法, 把依赖存到各自 service.

        参数故意宽泛 (``Any`` + ``str``), 避免 BaseModule 对 safety 子模块产生
        强依赖 —— core 层不应该 import 具体 module 的东西, safety 也不该 import
        回 core.module. 具体 module 重写时自己断言类型.
        """
        _ = suspended_store, workspace_id, mode
        return None

    def get_resumed_task_executor(self) -> Callable[..., Any] | None:
        """可选: 返回一个实现 :class:`ResumedTaskExecutor` Protocol 的 callable.

        server.py 在入站 Resume 回路 (``try_resume_suspended_turn``) 中查一张
        ``module_name -> executor`` 表: 任务从挂起到归档后, 立刻把原 intent 就
        地重跑, 不再等下一轮 patrol. 具体契约见
        ``pulse.core.safety.resume.ResumedTaskExecutor``.

        默认返回 None —— 没有对外副作用的 module (如只读模块) 不必实现.
        需要 "Resume 后立刻把动作落下" 的 module 必须实现, 否则用户确认
        后只会得到确认回执, 原始外部动作不会立即发生.

        本方法与 ``attach_safety_plane`` 一样参数宽泛, 避免 core.module 对
        safety 子模块产生反向依赖. 具体 module 重写时用更紧的类型.
        """
        return None

    def emit_event(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        if self._event_emitter is None:
            return
        try:
            self._event_emitter(event_type, dict(payload or {}))
        except Exception:
            logger.exception("module event emit failed: module=%s event_type=%s", self.name, event_type)

    def emit_stage_event(
        self,
        *,
        stage: str,
        status: str,
        trace_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        safe_trace_id = str(trace_id or "").strip() or f"trace_{uuid4().hex[:12]}"
        safe_stage = str(stage or "").strip() or "unknown"
        safe_status = str(status or "").strip() or "unknown"
        row = {
            "trace_id": safe_trace_id,
            "module": self.name,
            "stage": safe_stage,
            "status": safe_status,
        }
        row.update(dict(payload or {}))
        self.emit_event(f"module.{self.name}.{safe_stage}.{safe_status}", row)
        # ADR-005 §4: stage events are the primary audit signal for every
        # module workflow (job_chat, job_greet, ...). Previously they only
        # reached the event bus; now we also mirror one structured line per
        # stage transition into the logs so ``tail -f pulse.log`` shows the
        # full chain without a separate SSE subscriber.
        try:
            module_logger = logging.getLogger(f"pulse.modules.{self.name}")
            payload_preview = _preview_payload(payload)
            module_logger.info(
                "stage module=%s stage=%s status=%s trace=%s payload=%s",
                self.name,
                safe_stage,
                safe_status,
                safe_trace_id,
                payload_preview,
                extra={"trace_id": safe_trace_id},
            )
        except Exception:  # pragma: no cover — logging must never break the app
            logger.exception(
                "stage.log.emit.failed module=%s stage=%s",
                self.name,
                safe_stage,
            )
        return safe_trace_id


class ModuleRegistry:
    def __init__(self) -> None:
        self._modules: dict[str, BaseModule] = {}
        self._event_emitter: EventEmitter | None = None

    @property
    def modules(self) -> tuple[BaseModule, ...]:
        return tuple(self._modules.values())

    def register(self, module: BaseModule) -> None:
        if not module.name:
            raise ValueError("module.name must be non-empty")
        if module.name in self._modules:
            raise ValueError(f"duplicated module name: {module.name}")
        module.bind_event_emitter(self._event_emitter)
        self._modules[module.name] = module

    def bind_event_emitter(self, emitter: EventEmitter | None) -> None:
        self._event_emitter = emitter
        for module in self._modules.values():
            module.bind_event_emitter(emitter)

    def discover(self, package_name: str = "pulse.modules") -> list[BaseModule]:
        """Recursively discover ``module.py`` files under the package tree.

        Convention:
          - Any sub-package whose name starts with ``_`` is skipped
            (e.g. ``_connectors``, ``_shared``, ``__pycache__``).
          - Any sub-package named ``shared`` is skipped (domain-internal helpers).
          - A module is only collected when the containing package has
            a ``module.py`` that exposes either a ``module: BaseModule``
            attribute or a ``get_module()`` factory.
        """
        root_package = importlib.import_module(package_name)
        root_path = getattr(root_package, "__path__", None)
        if root_path is None:
            return []

        discovered: list[BaseModule] = []
        self._walk_package(package_name, list(root_path), discovered)
        return discovered

    def _walk_package(
        self,
        package_name: str,
        package_path: list[str],
        discovered: list[BaseModule],
    ) -> None:
        for child in pkgutil.iter_modules(package_path):
            if not child.ispkg:
                continue
            if child.name.startswith("_") or child.name == "shared":
                continue

            child_package = f"{package_name}.{child.name}"
            module_path = f"{child_package}.module"
            try:
                candidate_module = importlib.import_module(module_path)
                module = self._extract_module(candidate_module)
            except ModuleNotFoundError as exc:
                if exc.name != module_path:
                    raise
                module = None

            if module is not None:
                self.register(module)
                discovered.append(module)

            try:
                child_pkg = importlib.import_module(child_package)
            except ModuleNotFoundError:
                continue
            child_path = getattr(child_pkg, "__path__", None)
            if child_path is None:
                continue
            self._walk_package(child_package, list(child_path), discovered)

    def attach_to_app(self, app: FastAPI) -> None:
        for module in self._modules.values():
            prefix = module.route_prefix or f"/api/modules/{module.name}"
            tags = module.tags or [module.name]
            router = APIRouter(prefix=prefix, tags=tags)
            module.register_routes(router)
            app.include_router(router)

    def as_tools(self) -> list[dict[str, object]]:
        """把所有已注册 module 导出为 LLM tool 描述符列表。

        输出两类 tool:
          - **Intent-level**(首选): ``module.intents`` 中每个 ``IntentSpec``
            各生成一个独立 tool (带 JSON Schema), LLM 可直接抽参。
          - **Coarse fallback**(兼容): 没有声明 ``intents`` 的 module 继续
            暴露 ``module.<name>`` 粗粒度 tool, 走 ``handle_intent`` 分发。
            这是向后兼容路径, 新 module 不应使用。
        """
        tools: list[dict[str, object]] = []
        for module in self._modules.values():
            intents = list(getattr(module, "intents", []) or [])
            if intents:
                tools.extend(self._build_intent_tools(module, intents))
            else:
                tools.append(self._build_coarse_tool(module))
        return tools

    def _build_intent_tools(
        self,
        module: BaseModule,
        intents: list[IntentSpec],
    ) -> list[dict[str, object]]:
        """把 module.intents 展开为 intent 粒度 tool 描述符。"""
        out: list[dict[str, object]] = []
        for intent in intents:
            tool_name = str(intent.name or "").strip()
            if not tool_name:
                logger.warning("module %s exposes an intent with empty name", module.name)
                continue
            handler = self._wrap_intent_handler(module=module, intent=intent)
            schema = self._normalize_intent_schema(intent.parameters_schema)
            out.append(
                {
                    "name": tool_name,
                    "description": str(intent.description or tool_name)[:1024],
                    "when_to_use": str(intent.when_to_use or "").strip(),
                    "when_not_to_use": str(intent.when_not_to_use or "").strip(),
                    "ring": "ring2_module",
                    "handler": handler,
                    "schema": schema,
                    "extract_facts": intent.extract_facts,
                    "metadata": {
                        "module_name": module.name,
                        "intent": tool_name,
                        "mutates": bool(intent.mutates),
                        "requires_confirmation": bool(intent.requires_confirmation),
                        "risk_level": int(intent.risk_level or 0),
                        "tags": list(intent.tags or []),
                        "route_prefix": module.route_prefix or f"/api/modules/{module.name}",
                    },
                }
            )
        return out

    def _wrap_intent_handler(
        self,
        *,
        module: BaseModule,
        intent: IntentSpec,
    ) -> Callable[[dict[str, Any]], Any]:
        """包装 IntentSpec.handler: 结构化 kwargs + 事件发射 + pipeline_runs 记录。

        返回的 handler 签名仍是 ``(payload_dict) -> Any``,适配 ``ToolRegistry``。
        内部把 dict 解包为 kwargs 调用业务 handler, 支持同步/异步。
        """
        import time

        registry = self

        async def _async_run(payload: dict[str, Any]) -> Any:
            safe_payload = dict(payload or {})
            metadata_raw = safe_payload.pop("_metadata", None)
            metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
            trace_id = str(metadata.get("trace_id") or "").strip() or f"trace_{uuid4().hex[:12]}"
            module.emit_stage_event(
                stage="intent",
                status="started",
                trace_id=trace_id,
                payload={
                    "intent": intent.name,
                    "trigger_source": "module_tool_intent",
                    "kwargs_preview": _preview_payload(safe_payload, max_chars=220),
                },
            )
            t0 = time.monotonic()
            try:
                raw = intent.handler(**safe_payload)
                if inspect.isawaitable(raw):
                    result = await raw
                else:
                    result = raw
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                module.emit_stage_event(
                    stage="intent",
                    status="failed",
                    trace_id=trace_id,
                    payload={
                        "intent": intent.name,
                        "trigger_source": "module_tool_intent",
                        "error": str(exc)[:500],
                        "elapsed_ms": elapsed_ms,
                    },
                )
                raise
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            registry._record_pipeline_run(
                module_name=module.name,
                intent=intent.name,
                result=result,
                elapsed_ms=elapsed_ms,
            )
            status = "completed"
            if isinstance(result, dict) and result.get("ok") is False:
                status = "failed"
            module.emit_stage_event(
                stage="intent",
                status=status,
                trace_id=trace_id,
                payload={
                    "intent": intent.name,
                    "trigger_source": "module_tool_intent",
                    "elapsed_ms": elapsed_ms,
                    "result_preview": _preview_payload(result, max_chars=260),
                },
            )
            return result

        def _handler(payload: dict[str, Any]) -> Any:
            return _async_run(payload)

        return _handler

    @staticmethod
    def _normalize_intent_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
        """标准化 IntentSpec.parameters_schema 为 OpenAI 兼容 JSON Schema。"""
        safe = dict(schema or {})
        if str(safe.get("type") or "").strip() != "object":
            safe["type"] = "object"
        props = safe.get("properties")
        if not isinstance(props, dict):
            safe["properties"] = {}
        if "additionalProperties" not in safe:
            safe["additionalProperties"] = False
        return safe

    def _build_coarse_tool(self, module: BaseModule) -> dict[str, object]:
        """兼容路径: 为没有声明 intents 的 module 生成 ``module.<name>`` 粗粒度 tool。

        新 module 不应依赖此路径, 迁移计划见 docs/Pulse-DomainMemory与Tool模式.md §7.3。
        """
        tool_name = f"module.{module.name}"
        registry = self

        def _handler(payload: dict[str, object], current_module: BaseModule = module) -> object:
            import time
            safe_payload = dict(payload or {})
            intent_name = str(safe_payload.get("intent") or f"module.{current_module.name}")
            text = str(safe_payload.get("text") or "")
            metadata_raw = safe_payload.get("metadata")
            metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
            trace_id = str(metadata.get("trace_id") or "").strip() or f"trace_{uuid4().hex[:12]}"
            metadata["trace_id"] = trace_id
            current_module.emit_stage_event(
                stage="intent",
                status="started",
                trace_id=trace_id,
                payload={
                    "intent": intent_name,
                    "trigger_source": "module_tool",
                    "text_preview": _preview_payload(text, max_chars=220),
                },
            )
            t0 = time.monotonic()
            try:
                result = current_module.handle_intent(intent_name, text, metadata)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                current_module.emit_stage_event(
                    stage="intent",
                    status="failed",
                    trace_id=trace_id,
                    payload={
                        "intent": intent_name,
                        "trigger_source": "module_tool",
                        "error": str(exc)[:500],
                        "elapsed_ms": elapsed_ms,
                    },
                )
                raise
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            registry._record_pipeline_run(
                module_name=current_module.name,
                intent=intent_name,
                result=result,
                elapsed_ms=elapsed_ms,
            )
            status = "completed"
            if isinstance(result, dict) and result.get("ok") is False:
                status = "failed"
            current_module.emit_stage_event(
                stage="intent",
                status=status,
                trace_id=trace_id,
                payload={
                    "intent": intent_name,
                    "trigger_source": "module_tool",
                    "elapsed_ms": elapsed_ms,
                    "result_preview": _preview_payload(result, max_chars=260),
                },
            )
            return result

        return {
            "name": tool_name,
            "description": str(module.description or f"Module tool for {module.name}"),
            "ring": "ring2_module",
            "handler": _handler,
            "metadata": {
                "module_name": module.name,
                "route_prefix": module.route_prefix or f"/api/modules/{module.name}",
            },
        }

    # jsonb 列的软上限: 避免一条记录撑爆 pipeline_runs 表 + 保护 log payload.
    # 4000 是历史值, 实际 PG jsonb 上限远高于此; 留到 8000 给出格式喘息.
    _PIPELINE_OUTPUT_SOFT_LIMIT = 8000

    @staticmethod
    def _record_pipeline_run(
        *,
        module_name: str,
        intent: str,
        result: object,
        elapsed_ms: int,
    ) -> None:
        """把一次 intent 执行结果落到 ``pipeline_runs`` 表, 审计用.

        历史 bug(2026-04): 直接把 ``json.dumps(...)[:4000]`` 塞进 ``jsonb`` 列,
        遇到大 payload(含 ISO 时间戳)时会**在 JSON 字符串字面量中间**切断,
        DB 报 ``invalid input syntax for type json: Token "2026-0..." invalid``.

        修复: 序列化后**先判长度**, 超限改写成合法 fallback JSON(带 ``_truncated``
        标记), 永远不把非法 JSON 送进 DB.
        """
        try:
            import json
            import uuid
            from .storage.engine import DatabaseEngine
            db = DatabaseEngine()

            status = "ok"
            if isinstance(result, dict) and result.get("ok") is False:
                status = "error"

            output_json = ModuleRegistry._safe_output_json(result)

            finished_at = datetime.now(timezone.utc)
            started_at = finished_at - timedelta(milliseconds=max(0, int(elapsed_ms)))
            db.execute(
                """INSERT INTO pipeline_runs(id, module_name, trigger_source, input_json, output_json, status, started_at, finished_at)
                   VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)""",
                (uuid.uuid4().hex, module_name, intent, "{}", output_json, status, started_at, finished_at),
            )
        except Exception as exc:
            logger.warning(
                "failed to record pipeline run for %s intent=%s elapsed_ms=%d err=%s",
                module_name,
                intent,
                elapsed_ms,
                str(exc)[:300],
            )

    @staticmethod
    def _safe_output_json(result: object) -> str:
        """序列化 result 为**一定合法**的 JSON 字符串, 超过软上限转 fallback.

        关键: 禁止对完整 JSON 做字符级 slice, 因为 slice 可能切断字符串 literal /
        对象结构 → DB 侧抛 ``invalid input syntax for type json``.
        """
        import json

        if not isinstance(result, dict):
            return "{}"
        try:
            full = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            return json.dumps(
                {"_serialize_error": str(exc)[:200], "_type": type(result).__name__},
                ensure_ascii=False,
            )
        if len(full) <= ModuleRegistry._PIPELINE_OUTPUT_SOFT_LIMIT:
            return full
        # 超限: 返回一个"结构化 preview", 而不是被切断的非法 JSON.
        preview_keys = sorted(result.keys())[:10]
        fallback = {
            "_truncated": True,
            "_original_length": len(full),
            "_keys_preview": preview_keys,
            "_head": full[:600],
            "ok": result.get("ok"),
            "status": result.get("status"),
        }
        return json.dumps(fallback, ensure_ascii=False, default=str)

    @staticmethod
    def _extract_module(candidate_module: ModuleType) -> BaseModule | None:
        direct = getattr(candidate_module, "module", None)
        if isinstance(direct, BaseModule):
            return direct

        factory = getattr(candidate_module, "get_module", None)
        if callable(factory):
            module = factory()
            if not isinstance(module, BaseModule):
                raise TypeError("get_module() must return BaseModule")
            return module
        return None
