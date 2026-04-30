from __future__ import annotations

import asyncio
import csv
import inspect
import io
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..tools import register_builtin_tools
from .brain import Brain
from .verifier import CommitmentVerifier
from .channel import CliChannelAdapter, FeishuChannelAdapter, IncomingMessage, OutgoingMessage, verify_feishu_signature, WechatWorkChannelAdapter, WechatWorkBotAdapter
from .config import get_settings
from .cost import CostController
from .evolution_config import build_evolution_governance_options
from .events import EventBus, InMemoryEventStore
from .event_sinks import JsonlEventSink
from .learning import DomainPreferenceDispatcher, DPOCollector, PreferenceExtractor
from .learning.behavior_analyzer import BehaviorAnalyzer
from .llm.router import LLMRouter
from .memory import ArchivalMemory, CoreMemory, RecallMemory, register_memory_tools
from .memory.workspace_memory import WorkspaceMemory
from .memory.correction_detector import CorrectionDetector
from .mcp_client import MCPClient, MCPTransport
from .mcp_server import DEFAULT_PROTOCOL_VERSION, MCPServerAdapter
from .mcp_servers_config import load_mcp_servers, pick_preferred_http_server
from .mcp_transport_http import HttpMCPTransport
from .mcp_transport_stdio import StdioMCPTransport
from .module import ModuleRegistry
from .profile import ProfileCoordinator
from .runtime import AgentRuntime, RuntimeConfig
from .scheduler import PatrolEnabledStateStore
from .task_context import create_interactive_context
from .prompt_contract import PromptContractBuilder
from .hooks import HookContext, HookRegistry, HookResult, HookPoint
from .safety import (
    SAFETY_PLANE_ENFORCE,
    SAFETY_PLANE_OFF,
    SuspendedTaskStore,
    WorkspaceSuspendedTaskStore,
    try_resume_suspended_turn,
)
from .compaction import CompactionEngine
from .memory_reader import MemoryReaderAdapter
from .promotion import PromotionEngine
from .policy_config import build_policy_engine
from .router_config import build_intent_router
from .skill_generator import SkillGenerator
from .storage.engine import DatabaseEngine
from .soul import GovernanceRulesVersionStore, SoulEvolutionEngine, SoulGovernance
from .startup_check import (
    StartupReport,
    check_channel_wechat_bot,
    check_mcp_transport,
    check_and_abort,
    emit_report,
)
from .tool import ToolRegistry


def _build_configured_mcp_transport(settings: Any) -> MCPTransport | None:
    """Build primary HTTP transport (backward compat). See _build_all_mcp_transports for multi-server."""
    config_path = str(getattr(settings, "mcp_servers_config_path", "") or "").strip()
    preferred_server = str(getattr(settings, "mcp_preferred_server", "") or "").strip()
    configured_servers = load_mcp_servers(config_path)
    chosen = pick_preferred_http_server(configured_servers, preferred_name=preferred_server)
    _log = logging.getLogger(__name__)
    if chosen is not None:
        try:
            return HttpMCPTransport(
                base_url=chosen.url,
                timeout_sec=chosen.timeout_sec,
                auth_token=chosen.auth_token,
                transport_mode=chosen.transport,
            )
        except Exception as exc:
            _log.error(
                "MCP transport build failed for preferred server '%s' (url=%s mode=%s): %s",
                chosen.name, chosen.url, chosen.transport, exc,
            )

    base_url = str(getattr(settings, "mcp_http_base_url", "") or "").strip()
    if not base_url:
        return None
    timeout_sec = float(getattr(settings, "mcp_http_timeout_sec", 8.0) or 8.0)
    auth_token = str(getattr(settings, "mcp_http_auth_token", "") or "").strip()
    try:
        return HttpMCPTransport(
            base_url=base_url,
            timeout_sec=timeout_sec,
            auth_token=auth_token,
        )
    except Exception as exc:
        _log.error(
            "MCP transport build failed for env base_url=%s: %s",
            base_url, exc,
        )
        return None


def _build_all_mcp_transports(
    settings: Any,
    *,
    report: "StartupReport | None" = None,
) -> dict[str, MCPTransport]:
    """Build transports for all configured MCP servers (http + stdio).

    Fail-fast 原则: 单个 server 构造失败不应拖垮整个 Pulse 启动 (MCP 只是工具面,
    降级到无工具模式仍可对话), 但**必须显式记录**, 由 startup self-check 汇总
    展示给用户. 不允许 ``except Exception: pass`` 静默吞.

    可选的 ``report`` 参数用于把每个 server 的构造结果 (ready / failed) 汇总
    到启动健康面板 —— 参见 ``startup_check.check_mcp_transport``.
    """
    config_path = str(getattr(settings, "mcp_servers_config_path", "") or "").strip()
    configured_servers = load_mcp_servers(config_path)
    transports: dict[str, MCPTransport] = {}
    _log = logging.getLogger(__name__)
    for cfg in configured_servers:
        try:
            if cfg.transport == "stdio":
                transports[cfg.name] = StdioMCPTransport(
                    server_name=cfg.name,
                    command=cfg.command,
                    args=cfg.args,
                    env=cfg.env,
                    timeout_sec=cfg.timeout_sec,
                )
                if report is not None:
                    report.add(check_mcp_transport(name=cfg.name, built=True, url=""))
            elif cfg.transport in {"http", "streamable_http", "http_sse", "sse", "legacy_sse"} and cfg.url:
                transports[cfg.name] = HttpMCPTransport(
                    base_url=cfg.url,
                    timeout_sec=cfg.timeout_sec,
                    auth_token=cfg.auth_token,
                    transport_mode=cfg.transport,
                )
                if report is not None:
                    report.add(check_mcp_transport(name=cfg.name, built=True, url=cfg.url))
            else:
                _log.warning(
                    "MCP server '%s' skipped: unsupported transport='%s' url=%s",
                    cfg.name, cfg.transport, cfg.url,
                )
                if report is not None:
                    report.add(check_mcp_transport(
                        name=cfg.name, built=False, url=cfg.url,
                        error=f"unsupported transport='{cfg.transport}'",
                    ))
        except Exception as exc:
            _log.error(
                "MCP transport build failed for '%s' (transport=%s url=%s): %s",
                cfg.name, cfg.transport, cfg.url, exc,
            )
            if report is not None:
                report.add(check_mcp_transport(
                    name=cfg.name, built=False, url=cfg.url, error=str(exc)[:120],
                ))
    return transports


def _synthesize_reply_from_brain_result(brain_result: Any) -> str:
    """Best-effort reply fallback when Brain answer is empty.

    Contract:
      - If tool steps carry ActionReport, prefer its summary (ground truth).
      - Else if tools were used, return a minimal execution receipt.
      - Never fabricate business facts.
    """
    steps = list(getattr(brain_result, "steps", []) or [])
    for step in reversed(steps):
        report = getattr(step, "action_report", None)
        if not isinstance(report, dict):
            continue
        summary = str(report.get("summary") or "").strip()
        metrics = report.get("metrics")
        metric_parts: list[str] = []
        if isinstance(metrics, dict):
            for key, value in metrics.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metric_parts.append(f"{str(key)}={value}")
        if summary:
            if metric_parts:
                return f"{summary}（{', '.join(metric_parts[:3])}）"
            return summary
        action = str(report.get("action") or getattr(step, "tool_name", "") or "").strip()
        status = str(report.get("status") or "").strip() or "unknown"
        if action:
            return f"已执行 {action}，状态：{status}。"
    used_tools = [str(x).strip() for x in (getattr(brain_result, "used_tools", []) or []) if str(x).strip()]
    if used_tools:
        head = "、".join(used_tools[:3])
        if len(used_tools) > 3:
            head = f"{head} 等"
        return f"已完成本轮请求（调用工具：{head}）。"
    return ""


def _extract_action_report_urls(
    brain_result: Any,
    *,
    action: str,
) -> list[str]:
    """Collect detail URLs from the latest matching ActionReport."""
    steps = list(getattr(brain_result, "steps", []) or [])
    safe_action = str(action or "").strip()
    if not safe_action:
        return []
    for step in reversed(steps):
        report = getattr(step, "action_report", None)
        if not isinstance(report, dict):
            continue
        if str(report.get("action") or "").strip() != safe_action:
            continue
        details = report.get("details")
        if not isinstance(details, list):
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for item in details:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
        return urls
    return []


def _render_clickable_job_link(url: str) -> str:
    """Render markdown link with readable label for IM clients."""
    safe_url = str(url or "").strip()
    if not safe_url:
        return ""
    # Avoid breaking markdown when URL contains ')' characters.
    safe_url = safe_url.replace(")", "%29")
    return f"[查看职位]({safe_url})"


def _render_clickable_chat_link(url: str) -> str:
    safe_url = str(url or "").strip()
    if not safe_url:
        return ""
    safe_url = safe_url.replace(")", "%29")
    return f"[查看会话]({safe_url})"


def _patch_job_greet_detail_links(reply: str, brain_result: Any) -> str:
    """Patch detail placeholders/raw URLs into clickable markdown links."""
    text = str(reply or "").strip()
    if not text:
        return text
    urls = _extract_action_report_urls(brain_result, action="job.greet")
    if not urls:
        return text
    placeholder = "查看详情"
    patched = text
    used_urls: list[str] = []

    while placeholder in patched and len(used_urls) < len(urls):
        next_url = urls[len(used_urls)]
        patched = patched.replace(placeholder, _render_clickable_job_link(next_url), 1)
        used_urls.append(next_url)

    for url in urls:
        if url in used_urls:
            continue
        if f"]({url})" in patched:
            used_urls.append(url)
            continue
        if url in patched:
            patched = patched.replace(url, _render_clickable_job_link(url), 1)
            used_urls.append(url)

    if len(used_urls) >= len(urls):
        return patched
    remaining = [url for url in urls if url not in used_urls]
    lines = [f"{idx}. {_render_clickable_job_link(url)}" for idx, url in enumerate(remaining, start=1)]
    return patched.rstrip() + "\n\n岗位详情链接：\n" + "\n".join(lines)


def _safe_job_chat_title(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return " ".join(text.split())[:240]


def _with_conversation_id(url: str, conversation_id: str) -> str:
    safe_url = str(url or "").strip()
    safe_id = str(conversation_id or "").strip()[:120]
    if not safe_id:
        return safe_url
    if not safe_url:
        return f"https://www.zhipin.com/web/geek/chat?conversationId={safe_id}"
    try:
        parsed = urlparse(safe_url)
    except ValueError:
        return safe_url
    if not parsed.scheme or not parsed.netloc:
        return safe_url
    query = parse_qs(parsed.query, keep_blank_values=False)
    existing = [str(v or "").strip() for v in query.get("conversationId") or []]
    if any(existing):
        return safe_url
    query["conversationId"] = [safe_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _extract_job_chat_manual_entries(brain_result: Any) -> list[dict[str, str]]:
    steps = list(getattr(brain_result, "steps", []) or [])
    for step in reversed(steps):
        report = getattr(step, "action_report", None)
        if not isinstance(report, dict):
            continue
        if str(report.get("action") or "").strip() != "job.chat":
            continue
        details = report.get("details")
        if not isinstance(details, list):
            return []
        manual_entries: list[dict[str, str]] = []
        fallback_entries: list[dict[str, str]] = []
        seen_keys: set[str] = set()
        for item in details:
            if not isinstance(item, dict):
                continue
            extras = item.get("extras")
            extras_map = extras if isinstance(extras, dict) else {}
            conversation_id = str(
                extras_map.get("conversation_id") or ""
            ).strip()[:120]
            target = _safe_job_chat_title(
                item.get("target")
                or extras_map.get("conversation_title")
                or f"conversation:{conversation_id or '-'}"
            )
            card_title = _safe_job_chat_title(extras_map.get("card_title"))
            title = target
            if card_title and card_title not in title:
                title = f"{title} | 卡片: {card_title}"
            raw_url = str(item.get("url") or "").strip()
            if raw_url and not (
                raw_url.startswith("http://") or raw_url.startswith("https://")
            ):
                raw_url = ""
            resolved_url = _with_conversation_id(raw_url, conversation_id)
            key = f"{resolved_url}|{conversation_id}|{title}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            entry = {
                "url": resolved_url,
                "conversation_id": conversation_id,
                "title": title,
            }
            fallback_entries.append(entry)
            status = str(item.get("status") or "").strip().lower()
            manual_flag = False
            if isinstance(extras_map, dict):
                extra_value = extras_map.get("manual_required")
                if isinstance(extra_value, bool):
                    manual_flag = extra_value
                elif isinstance(extra_value, (int, float)):
                    manual_flag = extra_value != 0
                elif isinstance(extra_value, str):
                    manual_flag = extra_value.strip().lower() in {"1", "true", "yes", "y"}
            if manual_flag or status == "failed":
                manual_entries.append(entry)
        return manual_entries or fallback_entries
    return []


def _extract_job_chat_manual_urls(brain_result: Any) -> list[str]:
    return [
        entry["url"]
        for entry in _extract_job_chat_manual_entries(brain_result)
        if str(entry.get("url") or "").strip()
    ]


def _patch_job_chat_manual_links(reply: str, brain_result: Any) -> str:
    text = str(reply or "").strip()
    entries = _extract_job_chat_manual_entries(brain_result)
    if not entries:
        return text
    patched = text
    used_urls: set[str] = set()
    for entry in entries:
        url = str(entry.get("url") or "").strip()
        if not url:
            continue
        if f"]({url})" in patched:
            used_urls.add(url)
            continue
        if url in patched:
            patched = patched.replace(url, _render_clickable_chat_link(url), 1)
            used_urls.add(url)
    if all((not str(entry.get("url") or "").strip()) or (str(entry.get("url") or "").strip() in used_urls) for entry in entries):
        return patched
    remaining = [
        entry
        for entry in entries
        if (not str(entry.get("url") or "").strip())
        or (str(entry.get("url") or "").strip() not in used_urls)
    ]
    lines: list[str] = []
    for idx, entry in enumerate(remaining, start=1):
        title = _safe_job_chat_title(entry.get("title")) or "未命名会话"
        conversation_id = str(entry.get("conversation_id") or "").strip()
        if conversation_id and conversation_id not in title:
            title = f"{title}（会话ID: {conversation_id}）"
        url = str(entry.get("url") or "").strip()
        if url:
            lines.append(f"{idx}. {title}：{_render_clickable_chat_link(url)}")
        else:
            lines.append(f"{idx}. {title}")
    prefix = (patched.rstrip() + "\n\n") if patched else ""
    return prefix + "待你人工处理的会话链接：\n" + "\n".join(lines)


def create_app(
    *,
    llm_router_override: LLMRouter | None = None,
    mcp_transport: MCPTransport | None = None,
    skill_output_dir_override: str | None = None,
) -> FastAPI:
    from .logging_config import set_trace_id, setup_logging
    setup_logging()
    logger = logging.getLogger(__name__)

    settings = get_settings()
    module_registry = ModuleRegistry()
    module_registry.discover("pulse.modules")
    llm_router = llm_router_override or LLMRouter()
    intent_router = build_intent_router(
        llm_router=llm_router,
        config_path=settings.router_rules_path,
        fallback_intent="general.default",
        fallback_target="hello",
    )
    policy_engine = build_policy_engine(
        config_path=settings.policy_rules_path,
        blocked_keywords_env=settings.policy_blocked_keywords,
        confirm_keywords_env=settings.policy_confirm_keywords,
    )
    policy_engine.set_intent_policy(
        "skill.activate",
        action="confirm",
        reason="generated skill activation requires explicit confirmation",
    )
    policy_engine.set_intent_policy(
        "mcp.external.call",
        action="confirm",
        reason="external MCP call requires explicit confirmation on first use",
    )
    core_memory = CoreMemory(
        storage_path=settings.core_memory_path,
        soul_config_path=settings.soul_config_path,
    )
    storage_engine = DatabaseEngine()
    recall_memory = RecallMemory(db_engine=storage_engine)
    archival_memory = ArchivalMemory(db_engine=storage_engine)
    workspace_memory = WorkspaceMemory(db_engine=storage_engine)

    def _governance_overrides_from_env() -> tuple[str | None, dict[str, str]]:
        default_mode_override = (
            settings.evolution_default_mode if os.getenv("PULSE_EVOLUTION_DEFAULT_MODE", "").strip() else None
        )
        change_mode_overrides: dict[str, str] = {}
        if os.getenv("PULSE_EVOLUTION_PREFS_MODE", "").strip():
            change_mode_overrides["prefs_update"] = settings.evolution_prefs_mode
        if os.getenv("PULSE_EVOLUTION_SOUL_MODE", "").strip():
            change_mode_overrides["soul_update"] = settings.evolution_soul_mode
        if os.getenv("PULSE_EVOLUTION_BELIEF_MODE", "").strip():
            change_mode_overrides["belief_mutation"] = settings.evolution_belief_mode
        return default_mode_override, change_mode_overrides

    def _load_governance_options() -> dict[str, Any]:
        default_override, change_overrides = _governance_overrides_from_env()
        return build_evolution_governance_options(
            config_path=settings.evolution_rules_path,
            default_mode_override=default_override,
            change_mode_overrides=change_overrides,
        )

    governance_options = _load_governance_options()
    resolved_rules_path = str(governance_options.get("resolved_path") or settings.evolution_rules_path)
    governance = SoulGovernance(
        core_memory=core_memory,
        audit_path=settings.governance_audit_path,
        default_mode=str(governance_options.get("default_mode") or "autonomous"),
        change_modes=dict(governance_options.get("change_modes") or {}),
        risk_mode_overrides=dict(governance_options.get("risk_mode_overrides") or {}),
        change_risk_mode_overrides=dict(governance_options.get("change_risk_mode_overrides") or {}),
    )
    rules_version_store = GovernanceRulesVersionStore(storage_path=settings.governance_rules_versions_path)
    rules_version_store.record(
        rules=governance.mode_status(),
        source="startup_load",
        actor="system",
        metadata={"loaded_from": resolved_rules_path},
    )
    dpo_collector = DPOCollector(db_engine=storage_engine)
    preference_extractor = PreferenceExtractor(llm_router=llm_router)
    # DomainPreferenceDispatcher 把"自然语言业务偏好 → DomainMemory"从
    # "靠 LLM 调对工具"改成"reflection 架构强制持久化". 每个业务 module
    # 通过 BaseModule.get_preference_appliers() 注册自己的 applier, 这里
    # 先实例化一个空 dispatcher, 稍后遍历 module_registry 收集注册.
    # 发事件能力 (preference.domain.*) 与其它 EventBus 消费者一起在 ready
    # 时绑定, 见下文 ``evolution_engine.bind_domain_preference_dispatcher``.
    domain_preference_dispatcher = DomainPreferenceDispatcher()
    evolution_engine = SoulEvolutionEngine(
        governance=governance,
        archival_memory=archival_memory,
        preference_extractor=preference_extractor,
        domain_preference_dispatcher=domain_preference_dispatcher,
        dpo_collector=dpo_collector,
        dpo_auto_collect=False,
    )
    tool_registry = ToolRegistry()
    register_builtin_tools(tool_registry)
    register_memory_tools(
        tool_registry,
        core_memory=core_memory,
        recall_memory=recall_memory,
        archival_memory=archival_memory,
    )
    for module_tool in module_registry.as_tools():
        tool_registry.register(
            name=str(module_tool["name"]),
            handler=module_tool["handler"],  # type: ignore[arg-type]
            description=str(module_tool["description"]),
            when_to_use=str(module_tool.get("when_to_use") or ""),
            when_not_to_use=str(module_tool.get("when_not_to_use") or ""),
            ring=str(module_tool.get("ring") or "ring2_module"),  # type: ignore[arg-type]
            schema=dict(module_tool.get("schema") or {}),  # type: ignore[arg-type]
            metadata=dict(module_tool.get("metadata") or {}),
            extract_facts=module_tool.get("extract_facts"),  # type: ignore[arg-type]
        )
    skill_generator = SkillGenerator(
        tool_registry=tool_registry,
        output_dir=skill_output_dir_override or settings.generated_skills_dir,
        llm_router=llm_router,
    )
    cost_controller = CostController(daily_budget_usd=settings.brain_daily_budget_usd)
    # 启动健康自检: 汇总 channel/MCP/... 的 ready/failed, 在 lifespan startup 阶段打印一张表.
    # fatal 项 (比如已配置但 SDK 缺的 wechat-bot) 会让进程退出, 拒绝"静默假装在工作".
    startup_report = StartupReport()
    active_mcp_transport = mcp_transport or _build_configured_mcp_transport(settings)
    all_transports = _build_all_mcp_transports(settings, report=startup_report)
    mcp_client = MCPClient(transport=active_mcp_transport, transports=all_transports)
    approved_external_mcp_tools: set[str] = set()
    external_mcp_aliases: dict[str, tuple[str, str]] = {}

    def _slug_segment(value: str, *, fallback: str) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return fallback
        chars: list[str] = []
        for ch in raw:
            if ch.isalnum() or ch in {".", "_", "-"}:
                chars.append(ch)
            else:
                chars.append("_")
        joined = "".join(chars).strip("._-")
        while "__" in joined:
            joined = joined.replace("__", "_")
        return joined or fallback

    def _external_tool_alias(server: str, name: str) -> str:
        server_seg = _slug_segment(server, fallback="external")
        name_seg = _slug_segment(name, fallback="tool")
        return f"mcp.{server_seg}.{name_seg}"

    def _approval_key(server: str, name: str) -> str:
        return f"{server.strip().lower()}::{name.strip().lower()}"

    def _external_tool_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
        safe_schema = dict(schema or {})
        if str(safe_schema.get("type") or "").strip() != "object":
            safe_schema["type"] = "object"
        props_raw = safe_schema.get("properties")
        safe_props = dict(props_raw) if isinstance(props_raw, dict) else {}
        safe_props["_confirm"] = {
            "type": "boolean",
            "description": "Set true to confirm first-time external MCP call.",
        }
        safe_schema["properties"] = safe_props
        return safe_schema

    def _register_external_mcp_tools() -> list[dict[str, Any]]:
        discovered: list[dict[str, Any]] = []
        for item in mcp_client.list_tools():
            alias = _external_tool_alias(item.server, item.name)
            external_mcp_aliases[alias] = (item.server, item.name)
            discovered.append(
                {
                    "alias": alias,
                    "server": item.server,
                    "name": item.name,
                    "description": item.description,
                    "schema": item.schema,
                }
            )
            if tool_registry.get(alias) is not None:
                continue

            async def _handler(
                args: dict[str, Any],
                *,
                _server: str = item.server,
                _name: str = item.name,
                _alias: str = alias,
            ) -> dict[str, Any]:
                payload = dict(args or {})
                confirm = bool(payload.pop("_confirm", False))
                try:
                    payload_preview = json.dumps(payload, ensure_ascii=False)
                except Exception:
                    payload_preview = str(payload)
                policy = policy_engine.evaluate(
                    intent="mcp.external.call",
                    text=f"{_server}.{_name} {payload_preview}",
                    metadata={
                        "server": _server,
                        "name": _name,
                        "alias": _alias,
                        "arguments": payload,
                    },
                )
                if policy.action == "blocked":
                    return {
                        "ok": False,
                        "blocked": True,
                        "server": _server,
                        "name": _name,
                        "policy": {
                            "action": policy.action,
                            "reason": policy.reason,
                            "matched_rule": policy.matched_rule,
                        },
                    }
                approval_key = _approval_key(_server, _name)
                if policy.action == "confirm" and not confirm and approval_key not in approved_external_mcp_tools:
                    return {
                        "ok": False,
                        "needs_confirmation": True,
                        "server": _server,
                        "name": _name,
                        "policy": {
                            "action": policy.action,
                            "reason": policy.reason,
                            "matched_rule": policy.matched_rule,
                        },
                    }
                result = await mcp_client.call_tool(server=_server, name=_name, arguments=payload)
                approved_external_mcp_tools.add(approval_key)
                return {
                    "ok": True,
                    "server": _server,
                    "name": _name,
                    "result": result,
                }

            tool_registry.register(
                name=alias,
                handler=_handler,
                description=f"[{item.server}] {item.description or item.name}",
                ring="ring3_mcp",
                schema=_external_tool_schema(item.schema),
                metadata={
                    "external": True,
                    "server": item.server,
                    "external_name": item.name,
                },
            )
        return discovered

    _register_external_mcp_tools()
    correction_detector = CorrectionDetector(
        llm_router=llm_router,
        dpo_collector=dpo_collector,
        recall_memory=recall_memory,
        core_memory=core_memory,
        governance=governance,
    )
    behavior_analyzer = BehaviorAnalyzer(
        llm_router=llm_router,
        recall_memory=recall_memory,
    )
    # -- P1+P2: PromptContract + Hook + Compaction + Promotion --
    memory_reader = MemoryReaderAdapter(
        core_memory=core_memory,
        recall_memory=recall_memory,
        archival_memory=archival_memory,
        workspace_memory=workspace_memory,
    )
    tool_specs = list(tool_registry.list_tools())
    # Keep prompt-budget metadata behind LLMRouter's model-selection API.
    # Server should not duplicate route ordering or model-window lookup.
    _primary_model = llm_router.primary_model("planning")
    prompt_builder = PromptContractBuilder(
        memory=memory_reader,
        tool_specs=tool_specs,
        max_input_tokens=llm_router.input_token_budget("planning"),
        tokenizer_model=_primary_model,
    )
    # 收集所有 module 的 domain snapshot provider, 注入 Brain system prompt。
    # 见 docs/Pulse-DomainMemory与Tool模式.md §3.3 / §5.3。
    for _mod in module_registry.modules:
        try:
            _provider = _mod.get_domain_snapshot_provider()
        except Exception as _exc:
            logging.getLogger(__name__).warning(
                "module %s get_domain_snapshot_provider failed: %s", _mod.name, _exc
            )
            continue
        if _provider is not None:
            prompt_builder.register_domain_snapshot_provider(_provider)
    hooks = HookRegistry()

    def _policy_before_task(hctx: HookContext) -> HookResult:
        payload = dict(hctx.payload or {})
        query = str(payload.get("query") or "").strip()
        if not query:
            return HookResult()
        metadata = dict(hctx.ctx.extra or {})
        route_hint = payload.get("route_hint") or metadata.get("route_hint")
        if isinstance(route_hint, dict):
            metadata["route_hint"] = route_hint
        intent = str(metadata.get("intent") or "brain.run").strip() or "brain.run"
        decision = policy_engine.evaluate(intent=intent, text=query, metadata=metadata)
        if decision.action == "blocked":
            return HookResult(block=True, reason=decision.reason)
        if decision.action == "confirm":
            return HookResult(block=True, reason=f"confirmation required: {decision.reason}")
        return HookResult(injected={"policy_action": decision.action, "policy_reason": decision.reason})

    def _policy_before_tool(hctx: HookContext) -> HookResult:
        payload = dict(hctx.payload or {})
        tool_name = str(payload.get("tool_name") or "").strip()
        tool_args = payload.get("tool_args") or {}
        if not tool_name:
            return HookResult()
        try:
            tool_args_text = json.dumps(tool_args, ensure_ascii=False)
        except Exception:
            tool_args_text = str(tool_args)
        decision = policy_engine.evaluate(
            intent=f"tool.{tool_name}",
            text=f"{tool_name} {tool_args_text}",
            metadata={
                **dict(hctx.ctx.extra or {}),
                "tool_name": tool_name,
                "tool_args": tool_args,
                "task_id": hctx.ctx.task_id,
                "workspace_id": hctx.ctx.workspace_id,
            },
        )
        if decision.action == "blocked":
            return HookResult(block=True, reason=decision.reason)
        if decision.action == "confirm":
            return HookResult(block=True, reason=f"confirmation required: {decision.reason}")
        return HookResult(injected={"policy_action": decision.action, "policy_reason": decision.reason})

    def _governance_before_promotion(hctx: HookContext) -> HookResult:
        payload = dict(hctx.payload or {})
        risk_level = str(payload.get("risk") or "medium")
        assessment = governance.assess_change(
            change_type="promotion_fact",
            risk_level=risk_level,
            source="promotion_hook",
            actor="promotion_engine",
            payload={
                "subject": payload.get("subject"),
                "predicate": payload.get("predicate"),
                "object": payload.get("object"),
                "confidence": payload.get("confidence"),
                "workspace_id": hctx.ctx.workspace_id,
                "task_id": hctx.ctx.task_id,
            },
            reason="promotion candidate requires governance assessment",
        )
        if not assessment.get("ok"):
            return HookResult(block=True, reason=str(assessment.get("reason") or "blocked by governance"))
        return HookResult(injected={"governance_change_id": assessment.get("change_id")})

    hooks.register(HookPoint.before_task_start, _policy_before_task, name="policy.before_task", priority=20)
    hooks.register(HookPoint.before_tool_use, _policy_before_tool, name="policy.before_tool", priority=20)
    hooks.register(HookPoint.before_promotion, _governance_before_promotion, name="governance.before_promotion", priority=20)

    # ── Domain Profile 运行时 ──
    # memory 是单一事实源, yaml 是它的实时投影:
    #   1. 启动: load_all() 让各 domain 用 yaml 种 memory (冷启动)
    #   2. 运行: after_tool_use hook 在 mutating tool 成功后 rewrite 对应
    #      domain 的 yaml, 保证 "memory 与 yaml 相等" 的一致性契约
    # 见 docs/Pulse-DomainMemory与Tool模式.md "Domain Profile 管理" 章节。
    profile_coordinator = ProfileCoordinator()
    import logging as _logging  # noqa: PLC0415
    _profile_logger = _logging.getLogger(__name__)
    for _mod in module_registry.modules:
        try:
            _pm = _mod.get_profile_manager()
        except Exception as _exc:
            _profile_logger.warning(
                "module %s get_profile_manager failed: %s", _mod.name, _exc,
            )
            continue
        if _pm is not None:
            try:
                profile_coordinator.register(_pm)
            except ValueError as _exc:
                _profile_logger.warning(
                    "profile register rejected for module %s: %s", _mod.name, _exc,
                )
    _profile_logger.info(
        "profile_coordinator load_all report=%s", profile_coordinator.load_all(),
    )

    def _profile_after_tool(hctx: HookContext) -> HookResult:
        payload = dict(hctx.payload or {})
        if str(payload.get("status") or "") != "ok":
            return HookResult()
        tool_name = str(payload.get("tool_name") or "").strip()
        if not tool_name or "." not in tool_name:
            return HookResult()
        # 约定: intent tool 名形如 "<domain>.<capability>.<action>"。
        # 第一段就是 domain, 与 DomainProfileManager.domain 匹配。
        domain_seg = tool_name.split(".", 1)[0]
        if domain_seg in {"module", "mcp", "memory", "core"}:
            return HookResult()  # 内核/外部 tool, 无对应 profile domain
        profile_coordinator.sync_one(domain_seg)
        return HookResult()

    hooks.register(
        HookPoint.after_tool_use,
        _profile_after_tool,
        name="profile.after_tool",
        priority=200,
    )

    compaction = CompactionEngine()

    promotion = PromotionEngine(
        hooks=hooks,
        archival_memory=archival_memory,
        core_memory=core_memory,
    )

    # ToolUseContract C (ADR-001 §4.4): end-of-turn commitment auditor.
    # Runs on the same LLMRouter as Brain — routes internally via
    # ``classification`` (gpt-4o-mini tier), kill-switch at
    # ``PULSE_COMMITMENT_VERIFIER=off``.
    commitment_verifier = CommitmentVerifier(llm_router=llm_router)

    brain = Brain(
        tool_registry=tool_registry,
        llm_router=llm_router,
        cost_controller=cost_controller,
        max_steps=settings.brain_max_steps,
        core_memory=core_memory,
        recall_memory=recall_memory,
        archival_memory=archival_memory,
        workspace_memory=workspace_memory,
        memory_recent_limit=settings.memory_recent_limit,
        evolution_engine=evolution_engine,
        correction_detector=correction_detector,
        prompt_builder=prompt_builder,
        hooks=hooks,
        compaction=compaction,
        promotion=promotion,
        commitment_verifier=commitment_verifier,
    )
    mcp_server = MCPServerAdapter(tool_registry=tool_registry)
    mcp_sessions: dict[str, dict[str, Any]] = {}
    module_map = {module.name: module for module in module_registry.modules}
    feedback_loop_module = module_map.get("feedback_loop")
    if feedback_loop_module is not None and hasattr(feedback_loop_module, "bind_evolution_engine"):
        feedback_loop_module.bind_evolution_engine(evolution_engine)  # type: ignore[attr-defined]
    channel_adapters = {
        "cli": CliChannelAdapter(),
        "feishu": FeishuChannelAdapter(),
        "wechat-work": WechatWorkChannelAdapter(
            corp_id=settings.wechat_work_corp_id,
            agent_id=settings.wechat_work_agent_id,
            secret=settings.wechat_work_secret,
            token=settings.wechat_work_token,
            encoding_aes_key=settings.wechat_work_encoding_aes_key,
        ),
        "wechat-work-bot": WechatWorkBotAdapter(
            bot_id=settings.wechat_work_bot_id,
            bot_secret=settings.wechat_work_bot_secret,
        ),
    }
    event_bus = EventBus()
    event_store = InMemoryEventStore(max_events=settings.event_store_max_events)
    event_bus.subscribe_all(event_store.record)
    # Observability Plane: append-only 审计 sink (仅持久化 llm.*/tool.*/memory.*/policy.* 等)
    event_audit_sink = JsonlEventSink(
        directory=str(getattr(settings, "event_audit_dir", "./data/exports/events")),
    )
    event_bus.subscribe_all(event_audit_sink.handle)
    module_registry.bind_event_emitter(event_bus.publish)
    # 把 event_bus 运行期注入已构造好的核心组件, 使其可以发射 memory.*/llm.* 事件
    core_memory.bind_event_emitter(event_bus.publish)
    llm_router.bind_event_emitter(event_bus.publish)
    brain.bind_event_emitter(event_bus.publish)
    # DomainPreferenceDispatcher: 发 preference.domain.* 事件 + 收集各 module 注册的 applier
    domain_preference_dispatcher.bind_event_emitter(event_bus.publish)
    _domain_applier_logger = logging.getLogger(__name__)
    for _mod in module_registry.modules:
        try:
            _appliers = _mod.get_preference_appliers() or []
        except Exception as _exc:   # noqa: BLE001
            _domain_applier_logger.warning(
                "module %s get_preference_appliers failed: %s", _mod.name, _exc,
            )
            continue
        for _applier in _appliers:
            try:
                domain_preference_dispatcher.register(_applier)
                _domain_applier_logger.info(
                    "preference applier registered: domain=%s from module=%s",
                    getattr(_applier, "domain", "?"), _mod.name,
                )
            except (ValueError, TypeError) as _exc:
                _domain_applier_logger.warning(
                    "module %s applier %r registration failed: %s",
                    _mod.name, _applier, _exc,
                )

    # ── SafetyPlane (ADR-006-v2) ──────────────────────────────────────
    # v2 架构: policy 闸门迁到 service 层的 side-effect 入口 (_execute_*),
    # server 不再向 Brain 的 before_tool_use 注册 hook. 这里只做两件事:
    #   1. 装配 SuspendedTaskStore (ask 分支挂起任务的持久化后端)
    #   2. 把 store + workspace_id + mode 注入每个 module 的 service
    # mode=off 时 store 仍装配但 service.attach_safety_plane 被告知 off,
    # service 内部会跳过 policy gate (留后门给灰度回滚).
    _safety_logger = logging.getLogger(__name__ + ".safety")
    _safety_mode = getattr(settings, "safety_plane", SAFETY_PLANE_OFF)
    safety_workspace_id: str = str(
        getattr(settings, "safety_workspace_id", "default") or "default"
    )
    safety_suspended_store: SuspendedTaskStore | None = None
    safety_active_mode: str = SAFETY_PLANE_OFF
    # Resume → Re-execute 映射: module_name -> ResumedTaskExecutor. 用户答 "y"
    # 后, ``try_resume_suspended_turn`` 用这张表找到对应业务层回调把原 intent
    # 立即跑完. 没注册的 module 走降级路径 (resume_outcome.execution.status =
    # "executor_missing"), 用户会看到明文说明.
    safety_resumed_executors: dict[str, Any] = {}
    try:
        _safety_suspended_store = WorkspaceSuspendedTaskStore(
            facts=workspace_memory, events=event_bus,
        )
        safety_suspended_store = _safety_suspended_store
        safety_active_mode = _safety_mode
        # 单用户自部署, 所有 module 的 suspend / resume 落同一个 workspace_id.
        # 这里传给每个 module 的 value 完全一致 —— 只要 module 和 inbound
        # resume 都用 ``safety_workspace_id`` 查 store, 就不存在 "挂在 A
        # 查 B 查不到" 的幽灵. module 若有历史遗留 workspace_id, 在自己的
        # attach_safety_plane 里覆盖, 但需要保证 inbound resume 仍能命中.
        for _mod in module_registry.modules:
            try:
                _mod.attach_safety_plane(
                    suspended_store=_safety_suspended_store,
                    workspace_id=safety_workspace_id,
                    mode=_safety_mode,
                )
            except Exception as exc:  # noqa: BLE001
                _safety_logger.exception(
                    "SafetyPlane attach failed for module=%s; module will run in "
                    "legacy mode (no policy gate)",
                    _mod.name,
                )
                if _safety_mode == SAFETY_PLANE_ENFORCE:
                    raise RuntimeError(
                        f"SafetyPlane enforce attach failed for module={_mod.name}"
                    ) from exc
            # attach 成功与否都尝试收集 executor —— 两条路径互不耦合: attach
            # 只关心"挂起"侧, executor 只关心"恢复"侧, 一边坏了另一边仍可用.
            try:
                executor = _mod.get_resumed_task_executor()
            except Exception as exc:  # noqa: BLE001
                _safety_logger.exception(
                    "get_resumed_task_executor raised for module=%s; "
                    "Resume → Re-execute disabled for this module",
                    _mod.name,
                )
                if _safety_mode == SAFETY_PLANE_ENFORCE:
                    raise RuntimeError(
                        f"SafetyPlane enforce executor registration failed for module={_mod.name}"
                    ) from exc
                executor = None
            if executor is not None:
                safety_resumed_executors[_mod.name] = executor
        _safety_logger.info(
            "SafetyPlane ready: mode=%s, workspace_id=%s, modules_attached=%d, "
            "resumed_executors=%s",
            _safety_mode,
            safety_workspace_id,
            len(module_registry.modules),
            sorted(safety_resumed_executors.keys()),
        )
    except Exception as exc:  # noqa: BLE001
        _safety_logger.exception(
            "SafetyPlane bootstrap failed; degrading to off for this process "
            "(mode=%s)",
            _safety_mode,
        )
        if _safety_mode == SAFETY_PLANE_ENFORCE:
            raise RuntimeError(
                "SafetyPlane enforce bootstrap failed; refusing to run without gates"
            ) from exc
        safety_suspended_store = None
        safety_active_mode = SAFETY_PLANE_OFF
        safety_resumed_executors = {}

    def _preview(value: Any, *, max_chars: int = 300) -> str:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except Exception:
                text = str(value)
        text = text.strip()
        if len(text) > max_chars:
            return text[:max_chars] + "...(truncated)"
        return text

    def _emit_event(event_type: str, payload: dict[str, Any]) -> None:
        event_bus.publish(event_type, payload)

    def _format_sse_event(row: dict[str, Any]) -> str:
        return (
            f"id: {row.get('event_id')}\n"
            f"event: {row.get('event_type')}\n"
            f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
        )

    def _mcp_response_headers(*, session_id: str = "") -> dict[str, str]:
        headers = {
            "MCP-Protocol-Version": DEFAULT_PROTOCOL_VERSION,
            "Cache-Control": "no-store",
        }
        safe_session = str(session_id or "").strip()
        if safe_session:
            headers["Mcp-Session-Id"] = safe_session
        return headers

    def _mcp_lookup_session(request: Request) -> tuple[str, dict[str, Any] | None]:
        session_id = str(request.headers.get("mcp-session-id") or "").strip()
        if not session_id:
            return "", None
        return session_id, mcp_sessions.get(session_id)

    def _write_turn_meta(
        *,
        trace_id: str,
        message: IncomingMessage,
        brain_result: Any,
        response: dict[str, Any],
    ) -> None:
        """ADR-005 §3: one JSON summary per user turn.

        Dumped to ``logs/traces/<trace_id>/meta.json`` so post-mortem tools
        can ``cat`` a single file to answer "what happened?" without diffing
        .log lines.
        """
        try:
            from .logging_config import TRACES_SUBDIR

            log_dir = Path(os.getenv("PULSE_LOG_DIR", "logs"))

            tool_calls: list[dict[str, Any]] = []
            for step in getattr(brain_result, "steps", []) or []:
                if getattr(step, "action", "") == "use_tool":
                    tool_calls.append({
                        "index": int(getattr(step, "index", 0)),
                        "tool": str(getattr(step, "tool_name", "") or ""),
                        "args": dict(getattr(step, "tool_args", {}) or {}),
                    })

            target_dir = log_dir / TRACES_SUBDIR / trace_id
            target_dir.mkdir(parents=True, exist_ok=True)
            meta = {
                "trace_id": trace_id,
                "channel": message.channel,
                "user_id": message.user_id,
                "user_text": message.text,
                "latency_ms": int(response.get("latency_ms") or 0),
                "handled": bool(response.get("handled")),
                "answer": getattr(brain_result, "answer", "") or "",
                "used_tools": list(getattr(brain_result, "used_tools", []) or []),
                "stopped_reason": str(getattr(brain_result, "stopped_reason", "") or ""),
                "tool_calls": tool_calls,
                "route": dict(response.get("route") or {}),
                "policy": dict(response.get("policy") or {}),
            }
            (target_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # Meta dump is observability, never a hot path: log and carry on.
            logger.exception("trace.meta.write.failed trace_id=%s", trace_id)

    def _send_outgoing_safe(
        adapters: dict[str, Any],
        *,
        channel: str,
        user_id: str,
        text: str,
        kind: str,
    ) -> bool:
        """按 channel 取 adapter 发一条 outbound 文本, 吞所有异常留日志.

        返回 True 表示交付给 adapter (不代表远端投递成功, 仅 adapter.send
        没有 raise); False 表示 adapter 缺失或 send 抛错. 调用方用它决定
        是否发告警事件, 但绝不让它打断主链路 —— AskRequest 通知失败也不能
        让 Brain 的 turn 标成 server error.
        """
        if not text:
            return False
        adapter = adapters.get(channel)
        if adapter is None:
            logger.warning(
                "outgoing.adapter.missing channel=%s kind=%s; message dropped",
                channel, kind,
            )
            return False
        try:
            adapter.send(OutgoingMessage(
                channel=channel,
                target_id=user_id,
                text=text,
                metadata={"kind": kind},
            ))
            return True
        except Exception:  # noqa: BLE001
            logger.exception(
                "outgoing.send.failed channel=%s kind=%s user_id=%s",
                channel, kind, user_id,
            )
            return False

    async def _dispatch_channel_message(message: IncomingMessage) -> dict[str, Any]:
        metadata = dict(message.metadata)
        metadata["channel"] = message.channel
        metadata["user_id"] = message.user_id
        if not str(metadata.get("session_id") or "").strip():
            metadata["session_id"] = f"{message.channel}:{message.user_id}"

        session_id = str(metadata.get("session_id") or "")
        # Pulse 单用户自部署, IM 会话与 workspace 仍保持一对一 (session 就是
        # workspace), 但 SafetyPlane 的 SuspendedTaskStore 独立走全局
        # safety_workspace_id —— 这样 Brain 的会话态和 SafetyPlane 的挂起态
        # 可以按不同生命周期管理 (前者跟随 session 老化, 后者必须跨 session
        # 持久, 直到用户答 y/n 为止).
        ctx = create_interactive_context(
            session_id=session_id,
            workspace_id=session_id,
            extra={"channel": message.channel, "user_id": message.user_id},
        )
        trace_id = ctx.trace_id
        # ADR-005 §1: bind trace_id at the *single entry* of a user turn so
        # that every downstream ``logger.info`` (channel adapter, brain,
        # modules, connector, remote boss_mcp via header) lands in the
        # same per-trace bucket. Without this, earlier logs (policy/route/
        # router.resolve) get ``trace=-`` and fragment across buckets.
        set_trace_id(trace_id)
        started_at = time.perf_counter()
        logger.info(
            "channel.msg.received channel=%s user=%s text_chars=%d session=%s",
            message.channel,
            message.user_id,
            len(message.text or ""),
            metadata["session_id"],
        )
        _emit_event(
            "channel.message.received",
            {
                "trace_id": trace_id,
                "channel": message.channel,
                "user_id": message.user_id,
                "text_preview": _preview(message.text, max_chars=220),
            },
        )

        # 入站前置 Resume 检查: 全局 safety_workspace_id 若有 awaiting
        # SuspendedTask, 本条消息视为对它的回答, 不跑 IntentRouter / Brain,
        # 直接 resolve + 给用户回确认. Store 未初始化 (mode=off / bootstrap
        # 失败) 一律走常规链路.
        if (
            safety_suspended_store is not None
            and safety_active_mode == SAFETY_PLANE_ENFORCE
        ):
            resume_outcome = try_resume_suspended_turn(
                store=safety_suspended_store,
                workspace_id=safety_workspace_id,
                user_text=message.text,
                received_at=message.received_at,
                executors=safety_resumed_executors or None,
            )
            if resume_outcome.should_skip_brain:
                resume_latency_ms = int((time.perf_counter() - started_at) * 1000)
                execution_payload: dict[str, Any] | None = None
                if resume_outcome.execution is not None:
                    execution_payload = {
                        "status": resume_outcome.execution.status,
                        "ok": bool(resume_outcome.execution.ok),
                        "summary": resume_outcome.execution.summary,
                    }
                resume_response: dict[str, Any] = {
                    "channel": message.channel,
                    "user_id": message.user_id,
                    "text": message.text,
                    "trace_id": trace_id,
                    "handled": resume_outcome.kind == "resolved",
                    "result": {
                        "resume": {
                            "kind": resume_outcome.kind,
                            "task_id": (
                                resume_outcome.task.task_id
                                if resume_outcome.task is not None
                                else None
                            ),
                            "execution": execution_payload,
                        }
                    },
                    "latency_ms": resume_latency_ms,
                }
                if resume_outcome.should_reply:
                    _send_outgoing_safe(
                        channel_adapters,
                        channel=message.channel,
                        user_id=message.user_id,
                        text=resume_outcome.user_reply,
                        kind="resume_reply",
                    )
                _emit_event(
                    "channel.message.resumed",
                    {
                        "trace_id": trace_id,
                        "channel": message.channel,
                        "user_id": message.user_id,
                        "resume_kind": resume_outcome.kind,
                        "task_id": (
                            resume_outcome.task.task_id
                            if resume_outcome.task is not None
                            else None
                        ),
                        "execution_status": (
                            resume_outcome.execution.status
                            if resume_outcome.execution is not None
                            else None
                        ),
                        "execution_ok": (
                            bool(resume_outcome.execution.ok)
                            if resume_outcome.execution is not None
                            else None
                        ),
                        "execution_summary": (
                            resume_outcome.execution.summary
                            if resume_outcome.execution is not None
                            else None
                        ),
                        "execution_detail": (
                            dict(resume_outcome.execution.detail)
                            if resume_outcome.execution is not None
                            else None
                        ),
                        "latency_ms": resume_latency_ms,
                    },
                )
                logger.info(
                    "channel.msg.resumed channel=%s kind=%s task_id=%s "
                    "exec_status=%s exec_ok=%s latency_ms=%d",
                    message.channel,
                    resume_outcome.kind,
                    resume_outcome.task.task_id if resume_outcome.task else "-",
                    (
                        resume_outcome.execution.status
                        if resume_outcome.execution is not None
                        else "-"
                    ),
                    (
                        resume_outcome.execution.ok
                        if resume_outcome.execution is not None
                        else "-"
                    ),
                    resume_latency_ms,
                )
                return resume_response

        route = intent_router.resolve(message.text)
        policy = policy_engine.evaluate(
            intent=route.intent,
            text=message.text,
            metadata=metadata,
        )
        response: dict[str, Any] = {
            "channel": message.channel,
            "user_id": message.user_id,
            "text": message.text,
            "route": {
                "intent": route.intent,
                "target": route.target,
                "method": route.method,
                "confidence": route.confidence,
                "reason": route.reason,
            },
            "policy": {
                "action": policy.action,
                "reason": policy.reason,
                "matched_rule": policy.matched_rule,
            },
            "trace_id": trace_id,
        }
        _emit_event(
            "channel.message.routed",
            {
                "trace_id": trace_id,
                "channel": message.channel,
                "intent": route.intent,
                "target": route.target,
                "policy_action": policy.action,
                "policy_reason": policy.reason,
            },
        )
        if policy.action != "safe":
            response["handled"] = False
            response["result"] = None
            response["reply"] = (
                f"⚠️ 这条请求被路由策略拦截了：{policy.reason or policy.action}。"
                f"如需继续请换种方式表达或解除限制。"
            )
            response["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "channel.message.blocked",
                {
                    "trace_id": trace_id,
                    "channel": message.channel,
                    "intent": route.intent,
                    "target": route.target,
                    "policy_action": policy.action,
                    "policy_reason": policy.reason,
                    "latency_ms": response["latency_ms"],
                },
            )
            return response

        target_name = str(route.target or "").strip().lower()
        if target_name and target_name not in module_map:
            response["handled"] = False
            response["result"] = None
            response["error"] = f"target module not found: {target_name or '-'}"
            response["reply"] = (
                f"⚠️ 路由命中目标模块 `{target_name}` 但当前未注册（可能是模块加载失败）。"
                f"请检查后端启动日志。"
            )
            response["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "channel.message.error",
                {
                    "trace_id": trace_id,
                    "channel": message.channel,
                    "intent": route.intent,
                    "target": target_name,
                    "error": response["error"],
                    "latency_ms": response["latency_ms"],
                },
            )
            return response

        metadata["intent"] = route.intent
        # 只有规则/LLM 显式命中 (exact/prefix/llm) 时才把 route_hint 传给 Brain;
        # method=fallback 的 target 是 IntentRouter 的 fallback_target (通常是 hello),
        # 自然语言输入走到这里, 把 "intent detected 'general.default' targeting "
        # module 'hello'" 注进 system message 只会给 LLM 制造误导噪声 (F10).
        if target_name and route.method in ("exact", "prefix", "llm"):
            metadata["route_hint"] = {
                "intent": route.intent,
                "target": target_name,
                "tool_name": f"module.{target_name}",
                "method": route.method,
            }
        prefer_llm = settings.brain_prefer_llm
        prefer_llm_raw = metadata.pop("prefer_llm", None)
        if isinstance(prefer_llm_raw, bool):
            prefer_llm = prefer_llm_raw
        max_steps_raw = metadata.pop("max_steps", None)
        max_steps: int | None = None
        if isinstance(max_steps_raw, int):
            max_steps = max_steps_raw
        elif isinstance(max_steps_raw, str) and max_steps_raw.strip().isdigit():
            max_steps = int(max_steps_raw.strip())
        _emit_event(
            "brain.run.started",
            {
                "trace_id": trace_id,
                "source": "channel",
                "channel": message.channel,
                "query_preview": _preview(message.text, max_chars=220),
                "prefer_llm": bool(prefer_llm),
                "max_steps": max_steps,
            },
        )
        try:
            brain_result = await brain.run(
                query=message.text,
                ctx=ctx,
                metadata=metadata,
                max_steps=max_steps,
                prefer_llm=prefer_llm,
            )
        except Exception as exc:
            response["handled"] = False
            response["result"] = None
            response["error"] = str(exc)[:500]
            response["mode"] = "brain"
            response["reply"] = (
                f"⚠️ 处理你的消息时后端出现异常：{type(exc).__name__}: {str(exc)[:300]}。"
                f"trace_id={trace_id}，请查看后端日志定位。"
            )
            response["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
            logger.exception(
                "brain.run failed for trace_id=%s channel=%s: %s",
                trace_id,
                message.channel,
                exc,
            )
            _emit_event(
                "brain.run.failed",
                {
                    "trace_id": trace_id,
                    "source": "channel",
                    "channel": message.channel,
                    "error": response["error"],
                    "latency_ms": response["latency_ms"],
                },
            )
            return response

        route_tool = f"module.{target_name}" if target_name else ""
        module_result: Any = None
        if route_tool:
            for step in reversed(brain_result.steps):
                if step.action == "use_tool" and step.tool_name == route_tool:
                    module_result = step.observation
                    break

        response["handled"] = bool(brain_result.answer or brain_result.used_tools)
        response["mode"] = "brain"
        answer_text = str(brain_result.answer or "").strip()
        if not answer_text and list(brain_result.used_tools):
            answer_text = _synthesize_reply_from_brain_result(brain_result)
            if answer_text:
                _emit_event(
                    "channel.reply.synthesized",
                    {
                        "trace_id": trace_id,
                        "channel": message.channel,
                        "reason": "empty_brain_answer_with_used_tools",
                        "used_tools": list(brain_result.used_tools),
                    },
                )
        answer_text = _patch_job_greet_detail_links(answer_text, brain_result)
        answer_text = _patch_job_chat_manual_links(answer_text, brain_result)
        response["reply"] = answer_text
        response["brain"] = brain_result.to_dict()
        if module_result is not None:
            response["result"] = module_result
        else:
            response["result"] = {
                "answer": brain_result.answer,
                "used_tools": list(brain_result.used_tools),
                "stopped_reason": brain_result.stopped_reason,
            }
        response["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
        _emit_event(
            "brain.run.completed",
            {
                "trace_id": trace_id,
                "source": "channel",
                "channel": message.channel,
                "used_tools": list(brain_result.used_tools),
                "stopped_reason": brain_result.stopped_reason,
                "steps_total": len(brain_result.steps),
                "latency_ms": response["latency_ms"],
            },
        )
        for step in brain_result.steps:
            _emit_event(
                "brain.step",
                {
                    "trace_id": trace_id,
                    "source": "channel",
                    "channel": message.channel,
                    "index": int(step.index),
                    "action": step.action,
                    "tool_name": step.tool_name,
                    "tool_args": dict(step.tool_args or {}),
                    "observation_preview": _preview(step.observation, max_chars=240),
                },
            )
            if step.action == "use_tool" and step.tool_name:
                _emit_event(
                    "brain.tool.invoked",
                    {
                        "trace_id": trace_id,
                        "source": "channel",
                        "channel": message.channel,
                        "tool_name": step.tool_name,
                        "tool_args": dict(step.tool_args or {}),
                        "observation_preview": _preview(step.observation, max_chars=240),
                    },
                )

        # v2 起 Brain 不再参与 safety 判决; Ask 分支的 IM 外发由
        # Service 层自己完成 (通过 Notifier 回传到对应 channel), 或者由
        # ``channel.message.completed`` 之后的 patrol 循环再次扫到挂起任务
        # 时, 通过 WorkspaceSuspendedTaskStore 事件触发 channel adapter.
        # 因此 server 这里不再解析 Brain.stopped_reason 来做 ask 出站.
        _emit_event(
            "channel.message.completed",
            {
                "trace_id": trace_id,
                "channel": message.channel,
                "handled": bool(response.get("handled")),
                "latency_ms": response["latency_ms"],
            },
        )
        logger.info(
            "channel.msg.completed channel=%s handled=%s used_tools=%s "
            "steps=%d stopped=%s latency_ms=%d",
            message.channel,
            bool(response.get("handled")),
            list(brain_result.used_tools),
            len(brain_result.steps),
            brain_result.stopped_reason,
            response["latency_ms"],
        )
        # ADR-005 §3: per-turn meta.json — one glance tells the whole story
        # without grepping the .log files.
        _write_turn_meta(
            trace_id=trace_id,
            message=message,
            brain_result=brain_result,
            response=response,
        )
        return response

    for adapter in channel_adapters.values():
        adapter.set_handler(_dispatch_channel_message)

    runtime_config = RuntimeConfig()
    patrol_state_store = PatrolEnabledStateStore(
        path=Path(os.path.expanduser(settings.patrol_state_path)),
    )
    agent_runtime = AgentRuntime(
        event_emitter=event_bus.publish if event_bus else None,
        config=runtime_config,
        hooks=hooks,
        compaction_engine=compaction,
        promotion_engine=promotion,
        recall_memory=recall_memory,
        workspace_memory=workspace_memory,
        patrol_state_store=patrol_state_store,
    )

    for module in module_registry.modules:
        module.bind_runtime(agent_runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        for module in module_registry.modules:
            startup_result = module.on_startup()
            if inspect.isawaitable(startup_result):
                await startup_result
        agent_runtime.start()

        wechat_bot: WechatWorkBotAdapter = channel_adapters["wechat-work-bot"]  # type: ignore[assignment]
        # 先把 channel 状态补进 startup_report (MCP 那几条在 create_app 构造期已入库).
        startup_report.add(check_channel_wechat_bot(configured=wechat_bot.configured))
        # 打印启动自检报告 (stderr + app 日志). has_fatal 时 check_and_abort 会 raise,
        # 让 uvicorn 以非零码退出 —— 这是故意的: 已配置但起不来绝不静默继续.
        emit_report(startup_report)
        check_and_abort(startup_report)

        if wechat_bot.configured:
            await wechat_bot.start(dispatch_fn=_dispatch_channel_message)

        try:
            yield
        finally:
            if wechat_bot.configured:
                await wechat_bot.stop()
            try:
                disarm_result = agent_runtime.disarm_patrols(actor="lifespan:shutdown")
                if int(disarm_result.get("failed_count") or 0) > 0:
                    logger.warning(
                        "runtime shutdown disarm has failures failed=%s",
                        disarm_result.get("failed"),
                    )
            except OSError as exc:
                logger.warning("runtime shutdown disarm failed: %s", exc)
            agent_runtime.stop()
            for module in reversed(module_registry.modules):
                shutdown_result = module.on_shutdown()
                if inspect.isawaitable(shutdown_result):
                    await shutdown_result

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.event_bus = event_bus
    app.state.event_store = event_store
    app.state.module_registry = module_registry
    app.state.intent_router = intent_router
    app.state.policy_engine = policy_engine
    app.state.channel_adapters = channel_adapters
    app.state.dispatch_channel_message = _dispatch_channel_message
    app.state.tool_registry = tool_registry
    app.state.cost_controller = cost_controller
    app.state.brain = brain
    app.state.core_memory = core_memory
    app.state.recall_memory = recall_memory
    app.state.archival_memory = archival_memory
    app.state.workspace_memory = workspace_memory
    app.state.governance = governance
    app.state.evolution_rules = governance_options
    app.state.governance_rules_versions = rules_version_store
    app.state.evolution_engine = evolution_engine
    app.state.dpo_collector = dpo_collector
    app.state.skill_generator = skill_generator
    app.state.mcp_client = mcp_client
    app.state.mcp_server = mcp_server
    app.state.mcp_external_aliases = external_mcp_aliases
    app.state.mcp_external_approved = approved_external_mcp_tools
    app.state.behavior_analyzer = behavior_analyzer
    app.state.agent_runtime = agent_runtime

    def _csv_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (dict, list, tuple)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except Exception:
                return str(value)
        return str(value)

    def _audits_to_csv(rows: list[dict[str, Any]]) -> str:
        output = io.StringIO()
        fieldnames = [
            "change_id",
            "timestamp",
            "status",
            "type",
            "mode",
            "mode_reason",
            "risk_level",
            "source",
            "actor",
            "reason",
            "approved_by",
            "approved_at",
            "rolled_back_by",
            "rolled_back_at",
            "belief",
            "target",
            "updates",
            "before",
            "after",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for item in rows:
            writer.writerow({name: _csv_value(item.get(name)) for name in fieldnames})
        return output.getvalue()

    def _parse_datetime(raw: str | None) -> datetime | None:
        safe = str(raw or "").strip()
        if not safe:
            return None
        try:
            dt = datetime.fromisoformat(safe.replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _filter_rows_by_time(
        rows: list[dict[str, Any]],
        *,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> list[dict[str, Any]]:
        start_dt = _parse_datetime(start_at)
        end_dt = _parse_datetime(end_at)
        if start_dt is None and end_dt is None:
            return list(rows)
        result: list[dict[str, Any]] = []
        for item in rows:
            ts = _parse_datetime(str(item.get("timestamp") or ""))
            if ts is None:
                continue
            if start_dt is not None and ts < start_dt:
                continue
            if end_dt is not None and ts > end_dt:
                continue
            result.append(item)
        return result

    def _paginate_rows(
        rows: list[dict[str, Any]],
        *,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None, int, int]:
        safe_limit = max(1, min(int(limit), 5000))
        try:
            safe_cursor = max(0, int(str(cursor or "0").strip() or "0"))
        except (TypeError, ValueError):
            logging.getLogger(__name__).debug(
                "server: cursor=%r not int, defaulting to 0", cursor,
            )
            safe_cursor = 0
        total = len(rows)
        page = rows[safe_cursor : safe_cursor + safe_limit]
        next_cursor: str | None = None
        if safe_cursor + len(page) < total:
            next_cursor = str(safe_cursor + len(page))
        return page, next_cursor, total, safe_cursor

    def _build_trend_series(
        *,
        rows: list[dict[str, Any]],
        bucket: str,
        window_hours: int,
    ) -> list[dict[str, Any]]:
        safe_bucket = "day" if bucket == "day" else "hour"
        safe_window = max(1, min(int(window_hours), 24 * 30))
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        if safe_bucket == "hour":
            step = timedelta(hours=1)
            buckets_total = min(safe_window, 24 * 14)
            start = now - step * (buckets_total - 1)
            key_fmt = "%Y-%m-%dT%H:00:00Z"
        else:
            step = timedelta(days=1)
            buckets_total = max(1, min((safe_window + 23) // 24, 90))
            day_start = now.replace(hour=0)
            start = day_start - step * (buckets_total - 1)
            key_fmt = "%Y-%m-%d"

        counts: dict[str, int] = {}
        for idx in range(buckets_total):
            bucket_dt = start + step * idx
            counts[bucket_dt.strftime(key_fmt)] = 0

        end = start + step * buckets_total
        for item in rows:
            ts = _parse_datetime(str(item.get("timestamp") or ""))
            if ts is None or ts < start or ts >= end:
                continue
            if safe_bucket == "hour":
                key = ts.replace(minute=0, second=0, microsecond=0).strftime(key_fmt)
            else:
                key = ts.replace(hour=0, minute=0, second=0, microsecond=0).strftime(key_fmt)
            if key in counts:
                counts[key] += 1

        return [{"bucket": key, "count": count} for key, count in counts.items()]

    def _persist_rules_file(*, rules: dict[str, Any], persist: bool) -> tuple[bool, str | None]:
        if not persist:
            return False, None
        resolved_path = str((getattr(app.state, "evolution_rules", {}) or {}).get("resolved_path") or resolved_rules_path)
        target = Path(resolved_path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        payload = {
            "default_mode": str(rules.get("default_mode") or "autonomous"),
            "change_modes": dict(rules.get("change_modes") or {}),
            "risk_mode_overrides": dict(rules.get("risk_mode_overrides") or {}),
            "change_risk_mode_overrides": dict(rules.get("change_risk_mode_overrides") or {}),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, str(target.resolve())

    def _count_status_in_window(
        rows: list[dict[str, Any]],
        *,
        status: str,
        start: datetime,
        end: datetime,
    ) -> int:
        safe_status = str(status or "").strip().lower()
        total = 0
        for item in rows:
            if str(item.get("status") or "").strip().lower() != safe_status:
                continue
            ts = _parse_datetime(str(item.get("timestamp") or ""))
            if ts is None or ts < start or ts >= end:
                continue
            total += 1
        return total

    def _detect_audit_alerts(
        *,
        rows: list[dict[str, Any]],
        window_hours: int,
    ) -> list[dict[str, Any]]:
        safe_window = max(1, min(int(window_hours), 24 * 30))
        now = datetime.now(timezone.utc)
        current_start = now - timedelta(hours=safe_window)
        previous_start = current_start - timedelta(hours=safe_window)

        pending_current = _count_status_in_window(rows, status="pending_approval", start=current_start, end=now)
        pending_previous = _count_status_in_window(rows, status="pending_approval", start=previous_start, end=current_start)
        gated_current = _count_status_in_window(rows, status="blocked_by_gate", start=current_start, end=now)
        gated_previous = _count_status_in_window(rows, status="blocked_by_gate", start=previous_start, end=current_start)
        rejected_current = _count_status_in_window(rows, status="rejected", start=current_start, end=now)

        alerts: list[dict[str, Any]] = []
        if pending_current >= 8:
            alerts.append(
                {
                    "level": "warning",
                    "type": "pending_backlog",
                    "message": "Pending approvals backlog is high.",
                    "current": pending_current,
                    "previous": pending_previous,
                }
            )
        if pending_current >= 4 and pending_current >= max(1, pending_previous) * 2:
            alerts.append(
                {
                    "level": "warning",
                    "type": "pending_spike",
                    "message": "Pending approvals increased sharply.",
                    "current": pending_current,
                    "previous": pending_previous,
                }
            )
        if gated_current >= 3:
            alerts.append(
                {
                    "level": "critical",
                    "type": "gated_spike",
                    "message": "Gated changes are unusually high.",
                    "current": gated_current,
                    "previous": gated_previous,
                }
            )
        if rejected_current >= 6:
            alerts.append(
                {
                    "level": "warning",
                    "type": "rejected_spike",
                    "message": "Rejected changes are unusually high.",
                    "current": rejected_current,
                }
            )
        return alerts

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app": settings.app_name,
            "environment": settings.environment,
            "modules": [module.name for module in module_registry.modules],
        }

    # -- Agent Runtime control plane ----------------------------------------

    @app.get("/api/runtime/status")
    async def runtime_status() -> dict[str, Any]:
        return {"ok": True, "result": agent_runtime.status()}

    @app.post("/api/runtime/start")
    async def runtime_start() -> dict[str, Any]:
        started = agent_runtime.start()
        return {"ok": started, "result": agent_runtime.status()}

    @app.post("/api/runtime/stop")
    async def runtime_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        # Safety-default: manual stop disarms patrol lifecycle unless caller
        # explicitly opts out (disarm_patrols=false).
        disarm_patrols = bool(body.get("disarm_patrols", True))
        disarm_result: dict[str, Any] | None = None
        if disarm_patrols:
            disarm_result = agent_runtime.disarm_patrols(actor="rest:runtime_stop")
        stopped = agent_runtime.stop()
        return {
            "ok": True,
            "result": {
                "stopped": bool(stopped),
                "disarm_patrols": disarm_patrols,
                "disarm": disarm_result,
                "status": agent_runtime.status(),
            },
        }

    @app.post("/api/runtime/trigger")
    async def runtime_trigger() -> dict[str, Any]:
        ran = await agent_runtime.trigger_once()
        return {"ok": True, "result": {"ran_tasks": ran}}

    @app.post("/api/runtime/reset/{task_name}")
    async def runtime_reset(task_name: str) -> dict[str, Any]:
        ok = agent_runtime.reset_circuit_breaker(task_name)
        return {"ok": ok, "result": {"task_name": task_name}}

    @app.post("/api/runtime/wake")
    async def runtime_wake() -> dict[str, Any]:
        result = await asyncio.to_thread(agent_runtime.manual_wake)
        return {"ok": True, "result": result}

    @app.get("/api/runtime/heartbeat")
    async def runtime_heartbeat() -> dict[str, Any]:
        result = await asyncio.to_thread(agent_runtime.heartbeat)
        return {"ok": True, "result": result}

    @app.post("/api/runtime/takeover")
    async def runtime_takeover(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = (payload or {}).get("reason", "manual")
        result = agent_runtime.request_takeover(reason=reason)
        return {"ok": True, "result": result}

    @app.post("/api/runtime/pause")
    async def runtime_pause(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        reason = (payload or {}).get("reason", "manual_pause")
        result = agent_runtime.pause_patrols(reason=reason)
        return {"ok": True, "result": result}

    @app.post("/api/runtime/release")
    async def runtime_release(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        auto_restart = (payload or {}).get("auto_restart", True)
        result = agent_runtime.release_takeover(auto_restart=auto_restart)
        return {"ok": True, "result": result}

    @app.get("/api/runtime/checkpoints")
    async def runtime_checkpoints() -> dict[str, Any]:
        return {"ok": True, "result": agent_runtime.list_checkpoints()}

    # -- per-patrol control plane (ADR-004 §6.1) ---------------------------

    @app.get("/api/runtime/patrols")
    async def runtime_list_patrols() -> dict[str, Any]:
        patrols = agent_runtime.list_patrols()
        return {"ok": True, "result": {"patrols": patrols, "total": len(patrols)}}

    @app.get("/api/runtime/patrols/{name}")
    async def runtime_patrol_status(name: str) -> dict[str, Any]:
        snapshot = agent_runtime.get_patrol_stats(name)
        if snapshot is None:
            return {"ok": False, "error": f"patrol not found: {name}", "result": None}
        return {"ok": True, "result": snapshot}

    @app.post("/api/runtime/patrols/{name}/enable")
    async def runtime_patrol_enable(name: str) -> dict[str, Any]:
        try:
            ok = agent_runtime.enable_patrol(name, actor="rest")
        except OSError as exc:
            return {"ok": False, "error": f"patrol enable persistence failed: {exc}"}
        if not ok:
            return {"ok": False, "error": f"patrol not found or not controllable: {name}"}
        return {"ok": True, "result": {"name": name, "enabled": True}}

    @app.post("/api/runtime/patrols/{name}/disable")
    async def runtime_patrol_disable(name: str) -> dict[str, Any]:
        try:
            ok = agent_runtime.disable_patrol(name, actor="rest")
        except OSError as exc:
            return {"ok": False, "error": f"patrol disable persistence failed: {exc}"}
        if not ok:
            return {"ok": False, "error": f"patrol not found or not controllable: {name}"}
        return {"ok": True, "result": {"name": name, "enabled": False}}

    @app.post("/api/runtime/patrols/{name}/trigger")
    async def runtime_patrol_trigger(name: str) -> dict[str, Any]:
        result = await asyncio.to_thread(agent_runtime.run_patrol_once, name)
        return {"ok": bool(result.get("ok")), "result": result}

    @app.get("/api/runtime/subagents")
    async def runtime_subagents(parent_task_id: str | None = None) -> dict[str, Any]:
        return {"ok": True, "result": agent_runtime.list_subagents(parent_task_id)}

    # -- system info routes -------------------------------------------------

    @app.get("/api/system/router/status")
    async def router_status() -> dict[str, Any]:
        return {
            "known_intents": intent_router.known_intents(),
            "router_rules_path": settings.router_rules_path,
        }

    @app.get("/api/system/policy/status")
    async def policy_status() -> dict[str, Any]:
        return {
            "policy_rules_path": settings.policy_rules_path,
            "blocked_keywords_from_env": bool(settings.policy_blocked_keywords.strip()),
            "confirm_keywords_from_env": bool(settings.policy_confirm_keywords.strip()),
        }

    @app.post("/api/system/behavior-analysis")
    async def behavior_analysis(session_id: str = "default", lookback: int = 50) -> dict:
        proposals = app.state.behavior_analyzer.analyze_recent_behavior(
            session_id=session_id, lookback_turns=lookback,
        )
        return {"proposals": proposals, "count": len(proposals)}

    @app.get("/api/system/events/recent")
    async def system_events_recent(
        limit: int = 100,
        event_type: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        rows = event_store.recent(
            limit=max(1, min(limit, 2000)),
            event_type=event_type,
            trace_id=trace_id,
        )
        return {"total": len(rows), "items": rows}

    @app.get("/api/system/events/stats")
    async def system_events_stats(window_minutes: int = 60) -> dict[str, Any]:
        return {
            "ok": True,
            "result": event_store.stats(window_minutes=max(1, min(window_minutes, 24 * 60))),
        }

    @app.get("/api/system/events/export", response_model=None)
    async def system_events_export(
        limit: int = 1000,
        event_type: str | None = None,
        trace_id: str | None = None,
        format: str = "json",
    ) -> Any:
        safe_limit = max(1, min(limit, 5000))
        safe_format = str(format or "json").strip().lower()
        rows = event_store.export(
            limit=safe_limit,
            event_type=event_type,
            trace_id=trace_id,
        )
        if safe_format == "jsonl":
            content = ""
            if rows:
                content = "\n".join(json.dumps(item, ensure_ascii=False) for item in rows) + "\n"
            return Response(
                content=content,
                media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store"},
            )
        return {
            "ok": True,
            "total": len(rows),
            "retention": event_store.retention(),
            "items": rows,
        }

    @app.get("/api/system/events/stream")
    async def system_events_stream(
        replay_last: int = 0,
        event_type: str | None = None,
        trace_id: str | None = None,
        heartbeat_sec: int = 15,
        buffer_size: int = 200,
        max_events: int = 0,
    ) -> StreamingResponse:
        safe_replay = max(0, min(replay_last, 500))
        safe_heartbeat = max(1, min(heartbeat_sec, 60))
        safe_max_events = max(0, min(max_events, 1000))
        subscription = event_store.subscribe(
            event_type=event_type,
            trace_id=trace_id,
            buffer_size=max(10, min(buffer_size, 2000)),
        )
        replay_rows = event_store.export(
            limit=safe_replay,
            event_type=event_type,
            trace_id=trace_id,
        )

        def _iter_events():  # noqa: ANN202
            emitted = 0
            try:
                for row in replay_rows:
                    yield _format_sse_event(row)
                    emitted += 1
                    if safe_max_events and emitted >= safe_max_events:
                        return
                while True:
                    row = subscription.poll(timeout_sec=float(safe_heartbeat))
                    if row is None:
                        yield ": keep-alive\n\n"
                        continue
                    yield _format_sse_event(row)
                    emitted += 1
                    if safe_max_events and emitted >= safe_max_events:
                        return
            finally:
                subscription.close()

        return StreamingResponse(
            _iter_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/system/events/clear")
    async def system_events_clear(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        confirm = bool(request_payload.get("confirm", False))
        if not confirm:
            return {"ok": False, "needs_confirmation": True}
        removed = event_store.clear()
        return {"ok": True, "removed": removed}

    @app.get("/api/brain/tools")
    async def brain_tools() -> dict[str, Any]:
        _register_external_mcp_tools()
        tools = tool_registry.list_tools()
        return {
            "total": len(tools),
            "items": [
                {
                    "name": item.name,
                    "description": item.description,
                    "ring": item.ring,
                    "schema": item.schema,
                    "metadata": item.metadata,
                }
                for item in tools
            ],
        }

    @app.get("/api/brain/cost/status")
    async def brain_cost_status() -> dict[str, Any]:
        return cost_controller.status()

    @app.get("/api/memory/core")
    async def memory_core() -> dict[str, Any]:
        return {
            "snapshot": core_memory.snapshot(),
            "system_prompt": core_memory.build_system_prompt(max_chars=1200),
        }

    @app.post("/api/memory/core/update")
    async def memory_core_update(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        block = str(request_payload.get("block") or "").strip().lower()
        if not block:
            raise HTTPException(status_code=400, detail="block is required")
        content = request_payload.get("content")
        merge = bool(request_payload.get("merge", True))
        try:
            if block == "prefs":
                if not isinstance(content, dict):
                    raise HTTPException(status_code=400, detail="prefs update requires dict content")
                updated = core_memory.update_preferences(content)
            else:
                updated = core_memory.update_block(block=block, content=content, merge=merge)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "block": block, "updated": updated}

    @app.get("/api/memory/recall/recent")
    async def memory_recall_recent(limit: int = 20, session_id: str | None = None) -> dict[str, Any]:
        rows = recall_memory.recent(limit=max(1, min(limit, 200)), session_id=session_id)
        return {"total": len(rows), "items": rows}

    @app.post("/api/memory/search")
    async def memory_search(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        query = str(request_payload.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        top_k_raw = request_payload.get("top_k", 5)
        try:
            top_k = int(top_k_raw)
        except (TypeError, ValueError):
            logging.getLogger(__name__).debug(
                "/api/memory/search: top_k=%r not int, using 5", top_k_raw,
            )
            top_k = 5
        session_id = str(request_payload.get("session_id") or "").strip() or None
        keywords_raw = request_payload.get("keywords")
        if isinstance(keywords_raw, list) and keywords_raw:
            keywords = [str(k).strip() for k in keywords_raw if str(k or "").strip()]
        else:
            keywords = [query]
        match_mode = str(request_payload.get("match") or "any").strip().lower()
        rows = recall_memory.search_keyword(
            keywords=keywords,
            top_k=max(1, min(top_k, 30)),
            session_id=session_id,
            match=match_mode,
        )
        return {"query": query, "keywords": keywords, "total": len(rows), "items": rows}

    @app.get("/api/memory/archival/recent")
    async def memory_archival_recent(limit: int = 20) -> dict[str, Any]:
        rows = archival_memory.recent(limit=max(1, min(limit, 500)))
        return {"total": len(rows), "items": rows}

    @app.post("/api/memory/archival/query")
    async def memory_archival_query(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        subject = str(request_payload.get("subject") or "").strip() or None
        predicate = str(request_payload.get("predicate") or "").strip() or None
        keyword = str(request_payload.get("keyword") or "").strip() or None
        limit_raw = request_payload.get("limit", 30)
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            logging.getLogger(__name__).debug(
                "/api/memory/archival/query: limit=%r not int, using 30", limit_raw,
            )
            limit = 30
        rows = archival_memory.query(
            subject=subject,
            predicate=predicate,
            keyword=keyword,
            limit=max(1, min(limit, 300)),
        )
        return {
            "total": len(rows),
            "items": rows,
            "filters": {"subject": subject, "predicate": predicate, "keyword": keyword},
        }

    @app.get("/api/evolution/status")
    async def evolution_status() -> dict[str, Any]:
        context_block = core_memory.read_block("context")
        context = context_block if isinstance(context_block, dict) else {}
        beliefs = context.get("beliefs") if isinstance(context.get("beliefs"), dict) else {}
        governance_modes = governance.mode_status()
        stats = governance.audit_stats(window_hours=24)
        loaded_rules = dict(getattr(app.state, "evolution_rules", {}) or {})
        latest_rules_version = rules_version_store.latest()
        return {
            "audit_total": stats["total"],
            "audit_in_24h": stats["in_window"],
            "archival_total": archival_memory.count(),
            "dpo_pairs_total": dpo_collector.count(),
            "governance_mode": governance_modes["default_mode"],
            "governance_change_modes": governance_modes["change_modes"],
            "governance_risk_modes": governance_modes.get("risk_mode_overrides", {}),
            "governance_change_risk_modes": governance_modes.get("change_risk_mode_overrides", {}),
            "evolution_rules_path": str(loaded_rules.get("resolved_path") or settings.evolution_rules_path),
            "rules_versions_total": rules_version_store.count(),
            "rules_current_version_id": str((latest_rules_version or {}).get("version_id") or ""),
            "core_beliefs": list(beliefs.get("core") or []),
            "mutable_beliefs": list(beliefs.get("mutable") or []),
        }

    @app.get("/api/evolution/audits")
    async def evolution_audits(
        limit: int = 30,
        cursor: str | None = None,
        status: str | None = None,
        change_type: str | None = None,
        mode: str | None = None,
        risk_level: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> dict[str, Any]:
        rows = governance.list_audits(
            limit=5000,
            status=status,
            change_type=change_type,
            mode=mode,
            risk_level=risk_level,
        )
        filtered_rows = _filter_rows_by_time(rows, start_at=start_at, end_at=end_at)
        page_rows, next_cursor, total, used_cursor = _paginate_rows(
            filtered_rows,
            limit=max(1, min(limit, 5000)),
            cursor=cursor,
        )
        return {
            "total": total,
            "cursor": str(used_cursor),
            "next_cursor": next_cursor,
            "items": page_rows,
        }

    @app.get("/api/evolution/audits/stats")
    async def evolution_audits_stats(window_hours: int = 24) -> dict[str, Any]:
        stats = governance.audit_stats(window_hours=max(1, min(window_hours, 24 * 30)))
        return {"ok": True, "result": stats}

    @app.get("/api/evolution/audits/export")
    async def evolution_audits_export(
        format: str = "json",
        limit: int = 1000,
        cursor: str | None = None,
        status: str | None = None,
        change_type: str | None = None,
        mode: str | None = None,
        risk_level: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> Any:
        safe_format = str(format or "json").strip().lower()
        safe_limit = max(1, min(limit, 5000))
        rows = governance.list_audits(
            limit=5000,
            status=status,
            change_type=change_type,
            mode=mode,
            risk_level=risk_level,
        )
        filtered_rows = _filter_rows_by_time(rows, start_at=start_at, end_at=end_at)
        page_rows, next_cursor, total, used_cursor = _paginate_rows(
            filtered_rows,
            limit=safe_limit,
            cursor=cursor,
        )
        filters = {
            "status": status,
            "change_type": change_type,
            "mode": mode,
            "risk_level": risk_level,
            "start_at": start_at,
            "end_at": end_at,
            "limit": safe_limit,
            "cursor": str(used_cursor),
        }
        if safe_format == "csv":
            csv_text = _audits_to_csv(page_rows)
            headers = {"Content-Disposition": "attachment; filename=evolution_audits.csv"}
            if next_cursor is not None:
                headers["X-Next-Cursor"] = next_cursor
            return Response(
                content=csv_text,
                media_type="text/csv; charset=utf-8",
                headers=headers,
            )
        if safe_format != "json":
            raise HTTPException(status_code=400, detail="format must be json or csv")
        return {
            "ok": True,
            "format": "json",
            "total": total,
            "cursor": str(used_cursor),
            "next_cursor": next_cursor,
            "filters": filters,
            "items": page_rows,
        }

    @app.get("/api/evolution/dashboard")
    async def evolution_dashboard(window_hours: int = 24, recent_limit: int = 10) -> dict[str, Any]:
        safe_window = max(1, min(window_hours, 24 * 30))
        safe_recent_limit = max(1, min(recent_limit, 50))
        context_block = core_memory.read_block("context")
        context = context_block if isinstance(context_block, dict) else {}
        beliefs = context.get("beliefs") if isinstance(context.get("beliefs"), dict) else {}
        stats = governance.audit_stats(window_hours=safe_window)
        all_recent_for_window = governance.list_audits(limit=5000)
        window_rows = _filter_rows_by_time(
            all_recent_for_window,
            start_at=(datetime.now(timezone.utc) - timedelta(hours=safe_window)).isoformat(),
            end_at=datetime.now(timezone.utc).isoformat(),
        )
        alerts = _detect_audit_alerts(rows=window_rows, window_hours=safe_window)
        pending = governance.list_audits(status="pending_approval", limit=200)
        recent = governance.list_audits(limit=safe_recent_limit)
        governance_mode = governance.mode_status()
        return {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "window_hours": safe_window,
            "governance": governance_mode,
            "audits": {
                "stats": stats,
                "pending_total": len(pending),
                "pending_items": pending[:5],
                "recent": recent,
                "trends": {
                    "hourly": _build_trend_series(rows=window_rows, bucket="hour", window_hours=safe_window),
                    "daily": _build_trend_series(rows=window_rows, bucket="day", window_hours=safe_window),
                },
                "alerts": alerts,
            },
            "memory": {
                "archival_total": archival_memory.count(),
                "dpo_pairs_total": dpo_collector.count(),
                "core_beliefs_total": len(list(beliefs.get("core") or [])),
                "mutable_beliefs_total": len(list(beliefs.get("mutable") or [])),
            },
        }

    @app.get("/api/evolution/governance/mode")
    async def evolution_governance_mode() -> dict[str, Any]:
        return {"ok": True, "result": governance.mode_status()}

    @app.get("/api/evolution/governance/versions")
    async def evolution_governance_versions(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        page = rules_version_store.list_versions(limit=max(1, min(limit, 500)), cursor=cursor)
        latest = rules_version_store.latest()
        return {
            "ok": True,
            "total": int(page.get("total") or 0),
            "cursor": str(page.get("cursor") or "0"),
            "next_cursor": page.get("next_cursor"),
            "current_version_id": str((latest or {}).get("version_id") or ""),
            "items": list(page.get("items") or []),
        }

    @app.get("/api/evolution/governance/versions/diff")
    async def evolution_governance_versions_diff(
        from_version_id: str | None = None,
        to_version_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        diff = rules_version_store.diff_versions(
            from_version_id=from_version_id,
            to_version_id=to_version_id,
        )
        changes = list(diff.get("changes") or [])
        safe_limit = max(1, min(limit, 1000))
        return {
            "ok": bool(diff.get("ok")),
            "from_version_id": diff.get("from_version_id"),
            "to_version_id": diff.get("to_version_id"),
            "summary": diff.get("summary") or {"total": 0, "added": 0, "removed": 0, "updated": 0},
            "changes_total": len(changes),
            "changes": changes[:safe_limit],
            "reason": diff.get("reason"),
        }

    @app.post("/api/evolution/governance/versions/rollback")
    async def evolution_governance_version_rollback(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        version_id = str(request_payload.get("version_id") or "").strip()
        if not version_id:
            raise HTTPException(status_code=400, detail="version_id is required")
        confirm = bool(request_payload.get("confirm", False))
        if not confirm:
            return {"ok": False, "needs_confirmation": True}
        persist = bool(request_payload.get("persist", True))
        target = rules_version_store.get(version_id=version_id)
        if target is None:
            raise HTTPException(status_code=404, detail=f"version_id not found: {version_id}")
        rules = dict(target.get("rules") or {})
        apply_result = governance.replace_modes(
            default_mode=str(rules.get("default_mode") or "autonomous"),
            change_modes=dict(rules.get("change_modes") or {}),
            risk_mode_overrides=dict(rules.get("risk_mode_overrides") or {}),
            change_risk_mode_overrides=dict(rules.get("change_risk_mode_overrides") or {}),
        )
        app.state.evolution_rules = {
            **rules,
            "resolved_path": str(getattr(app.state, "evolution_rules", {}).get("resolved_path") or settings.evolution_rules_path),
        }
        persisted = False
        persisted_path: str | None = None
        try:
            persisted, persisted_path = _persist_rules_file(rules=apply_result, persist=persist)
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc)[:500],
                "result": apply_result,
                "rolled_back_from_version_id": version_id,
            }
        new_version = rules_version_store.record(
            rules=apply_result,
            source="version_rollback",
            actor="api",
            metadata={"from_version_id": version_id},
            dedupe=False,
        )
        return {
            "ok": True,
            "result": apply_result,
            "rolled_back_from_version_id": version_id,
            "new_version_id": str(new_version.get("version_id") or ""),
            "persisted": persisted,
            "persisted_path": persisted_path,
        }

    @app.post("/api/evolution/governance/reload")
    async def evolution_governance_reload(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        confirm = bool(request_payload.get("confirm", False))
        if not confirm:
            return {"ok": False, "needs_confirmation": True}
        options = _load_governance_options()
        result = governance.replace_modes(
            default_mode=str(options.get("default_mode") or "autonomous"),
            change_modes=dict(options.get("change_modes") or {}),
            risk_mode_overrides=dict(options.get("risk_mode_overrides") or {}),
            change_risk_mode_overrides=dict(options.get("change_risk_mode_overrides") or {}),
        )
        app.state.evolution_rules = options
        new_version = rules_version_store.record(
            rules=result,
            source="reload_from_file",
            actor="api",
            metadata={"loaded_from": str(options.get("resolved_path") or settings.evolution_rules_path)},
            dedupe=False,
        )
        return {
            "ok": True,
            "result": result,
            "loaded_from": str(options.get("resolved_path") or settings.evolution_rules_path),
            "new_version_id": str(new_version.get("version_id") or ""),
        }

    @app.post("/api/evolution/governance/mode")
    async def evolution_governance_mode_update(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        mode = str(request_payload.get("mode") or "").strip()
        if not mode:
            raise HTTPException(status_code=400, detail="mode is required")
        change_type = str(request_payload.get("change_type") or "").strip() or None
        risk_level = str(request_payload.get("risk_level") or "").strip() or None
        persist = bool(request_payload.get("persist", True))
        result = governance.set_mode(mode=mode, change_type=change_type, risk_level=risk_level)
        persisted = False
        persisted_path: str | None = None
        try:
            persisted, persisted_path = _persist_rules_file(rules=result, persist=persist)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500], "result": result}
        new_version = rules_version_store.record(
            rules=result,
            source="manual_mode_update",
            actor="api",
            metadata={"change_type": change_type, "risk_level": risk_level, "mode": mode},
            dedupe=False,
        )
        return {
            "ok": True,
            "result": result,
            "new_version_id": str(new_version.get("version_id") or ""),
            "persisted": persisted,
            "persisted_path": persisted_path,
        }

    @app.post("/api/evolution/governance/approve")
    async def evolution_governance_approve(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        change_id = str(request_payload.get("change_id") or "").strip()
        if not change_id:
            raise HTTPException(status_code=400, detail="change_id is required")
        confirm = bool(request_payload.get("confirm", False))
        if not confirm:
            return {"ok": False, "needs_confirmation": True}
        try:
            result = governance.approve_change(change_id=change_id, actor="api")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": bool(result.get("ok")), "result": result}

    @app.post("/api/evolution/reflect")
    async def evolution_reflect(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        user_text = str(request_payload.get("user_text") or "").strip()
        assistant_text = str(request_payload.get("assistant_text") or "").strip()
        if not user_text:
            raise HTTPException(status_code=400, detail="user_text is required")
        metadata_raw = request_payload.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        result = evolution_engine.reflect_interaction(
            user_text=user_text,
            assistant_text=assistant_text,
            metadata=metadata,
        )
        return {"ok": True, "result": result.to_dict()}

    @app.post("/api/evolution/rollback")
    async def evolution_rollback(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        change_id = str(request_payload.get("change_id") or "").strip()
        if not change_id:
            raise HTTPException(status_code=400, detail="change_id is required")
        confirm = bool(request_payload.get("confirm", False))
        if not confirm:
            return {"ok": False, "needs_confirmation": True}
        try:
            result = governance.rollback(change_id=change_id, actor="api")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": bool(result.get("ok")), "result": result}

    @app.get("/api/learning/dpo/status")
    async def learning_dpo_status() -> dict[str, Any]:
        return {
            "ok": True,
            "total": dpo_collector.count(),
            "auto_collect": bool(settings.dpo_auto_collect),
            "storage_path": settings.dpo_pairs_path,
        }

    @app.get("/api/learning/dpo/recent")
    async def learning_dpo_recent(limit: int = 20) -> dict[str, Any]:
        rows = dpo_collector.recent(limit=max(1, min(limit, 200)))
        return {"total": len(rows), "items": rows}

    @app.post("/api/learning/dpo/collect")
    async def learning_dpo_collect(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        prompt = str(request_payload.get("prompt") or "").strip()
        chosen = str(request_payload.get("chosen") or "").strip()
        rejected = str(request_payload.get("rejected") or "").strip()
        if not prompt or not chosen or not rejected:
            raise HTTPException(status_code=400, detail="prompt/chosen/rejected are required")
        metadata_raw = request_payload.get("metadata")
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
        try:
            pair = dpo_collector.add_pair(
                prompt=prompt,
                chosen=chosen,
                rejected=rejected,
                metadata=metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "item": pair}

    @app.get("/api/skills/list")
    async def skills_list(status: str | None = None) -> dict[str, Any]:
        rows = skill_generator.list_skills(status=status)
        return {"total": len(rows), "items": rows}

    @app.post("/api/skills/generate")
    async def skills_generate(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        prompt = str(request_payload.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        tool_name = str(request_payload.get("tool_name") or "").strip() or None
        description = str(request_payload.get("description") or "").strip() or None
        code_override_raw = request_payload.get("code")
        code_override = str(code_override_raw).strip() if isinstance(code_override_raw, str) else None
        try:
            record = skill_generator.create_skill(
                prompt=prompt,
                tool_name=tool_name,
                description=description,
                code_override=code_override,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": str(record.get("status") or "") != "blocked",
            "skill": record,
            "policy": {
                "action": "confirm",
                "reason": "generated skill requires explicit activation confirmation",
            },
        }

    @app.post("/api/skills/activate")
    async def skills_activate(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        skill_id = str(request_payload.get("skill_id") or "").strip()
        if not skill_id:
            raise HTTPException(status_code=400, detail="skill_id is required")
        confirm = bool(request_payload.get("confirm", False))
        policy = policy_engine.evaluate(
            intent="skill.activate",
            text=f"activate generated skill {skill_id}",
            metadata={"generated_skill": True, "skill_id": skill_id},
        )
        if policy.action == "blocked":
            raise HTTPException(status_code=403, detail=policy.reason)
        if policy.action == "confirm" and not confirm:
            return {
                "ok": False,
                "needs_confirmation": True,
                "policy": {
                    "action": policy.action,
                    "reason": policy.reason,
                    "matched_rule": policy.matched_rule,
                },
            }
        try:
            result = skill_generator.activate_skill(skill_id=skill_id, confirm=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": bool(result.get("ok")),
            "result": result,
            "policy": {
                "action": policy.action,
                "reason": policy.reason,
                "matched_rule": policy.matched_rule,
            },
        }

    @app.post("/api/brain/run")
    async def brain_run(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        query = str(request_payload.get("query") or "").strip()
        if not query:
            raise HTTPException(status_code=400, detail="query is required")
        max_steps_raw = request_payload.get("max_steps")
        max_steps = None
        if isinstance(max_steps_raw, int):
            max_steps = max_steps_raw
        elif isinstance(max_steps_raw, str) and max_steps_raw.strip().isdigit():
            max_steps = int(max_steps_raw.strip())
        prefer_llm_raw = request_payload.get("prefer_llm")
        if isinstance(prefer_llm_raw, bool):
            prefer_llm = prefer_llm_raw
        else:
            prefer_llm = settings.brain_prefer_llm
        metadata = request_payload.get("metadata")
        safe_metadata = metadata if isinstance(metadata, dict) else {}
        safe_metadata.setdefault("intent", "brain.run")
        api_ctx = create_interactive_context(
            session_id=str(safe_metadata.get("session_id") or ""),
        )
        trace_id = api_ctx.trace_id
        started_at = time.perf_counter()
        _emit_event(
            "brain.run.started",
            {
                "trace_id": trace_id,
                "source": "api",
                "query_preview": _preview(query, max_chars=220),
                "prefer_llm": bool(prefer_llm),
                "max_steps": max_steps,
            },
        )
        try:
            result = await brain.run(
                query=query,
                ctx=api_ctx,
                metadata=safe_metadata,
                max_steps=max_steps,
                prefer_llm=prefer_llm,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "brain.run.failed",
                {
                    "trace_id": trace_id,
                    "source": "api",
                    "error": str(exc)[:500],
                    "latency_ms": latency_ms,
                },
            )
            raise
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        _emit_event(
            "brain.run.completed",
            {
                "trace_id": trace_id,
                "source": "api",
                "used_tools": list(result.used_tools),
                "stopped_reason": result.stopped_reason,
                "steps_total": len(result.steps),
                "latency_ms": latency_ms,
            },
        )
        for step in result.steps:
            _emit_event(
                "brain.step",
                {
                    "trace_id": trace_id,
                    "source": "api",
                    "index": int(step.index),
                    "action": step.action,
                    "tool_name": step.tool_name,
                    "tool_args": dict(step.tool_args or {}),
                    "observation_preview": _preview(step.observation, max_chars=240),
                },
            )
            if step.action == "use_tool" and step.tool_name:
                _emit_event(
                    "brain.tool.invoked",
                    {
                        "trace_id": trace_id,
                        "source": "api",
                        "tool_name": step.tool_name,
                        "tool_args": dict(step.tool_args or {}),
                        "observation_preview": _preview(step.observation, max_chars=240),
                    },
                )
        return {
            "ok": True,
            "trace_id": trace_id,
            "result": result.to_dict(),
            "cost": cost_controller.status(),
            "latency_ms": latency_ms,
        }

    @app.post("/mcp", response_model=None)
    async def mcp_streamable_http(request: Request, payload: dict[str, Any] | None = Body(default=None)) -> Response:
        message = payload or {}
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="invalid MCP JSON-RPC payload")
        method = str(message.get("method") or "").strip()
        if not method:
            raise HTTPException(status_code=400, detail="method is required")

        if method == "initialize":
            response_payload = await mcp_server.handle_jsonrpc(message)
            session_id = f"mcp_{uuid.uuid4().hex[:16]}"
            init_params = message.get("params")
            init_payload = dict(init_params) if isinstance(init_params, dict) else {}
            mcp_sessions[session_id] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "initialized": False,
                "client_info": dict(init_payload.get("clientInfo") or {}) if isinstance(init_payload.get("clientInfo"), dict) else {},
                "protocol_version": str(init_payload.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION).strip() or DEFAULT_PROTOCOL_VERSION,
            }
            return Response(
                content=json.dumps(response_payload or {}, ensure_ascii=False),
                media_type="application/json",
                headers=_mcp_response_headers(session_id=session_id),
            )

        session_id, session = _mcp_lookup_session(request)
        if session is None:
            return Response(status_code=404, headers=_mcp_response_headers())

        if message.get("id") is None:
            if method == "notifications/initialized":
                session["initialized"] = True
            else:
                await mcp_server.handle_jsonrpc(message)
            return Response(status_code=202, headers=_mcp_response_headers(session_id=session_id))

        response_payload = await mcp_server.handle_jsonrpc(message)
        return Response(
            content=json.dumps(response_payload or {}, ensure_ascii=False),
            media_type="application/json",
            headers=_mcp_response_headers(session_id=session_id),
        )

    @app.get("/api/mcp/tools")
    async def mcp_tools() -> dict[str, Any]:
        external_tools = _register_external_mcp_tools()
        local_tools = mcp_server.list_tools()
        return {
            "external_enabled": active_mcp_transport is not None,
            "local_total": len(local_tools),
            "external_total": len(external_tools),
            "local_tools": local_tools,
            "external_tools": external_tools,
        }

    @app.post("/api/mcp/call")
    async def mcp_call(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        name = str(request_payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="name is required")
        arguments_raw = request_payload.get("arguments")
        arguments = arguments_raw if isinstance(arguments_raw, dict) else {}
        server_name = str(request_payload.get("server") or "").strip()
        confirm = bool(request_payload.get("confirm", False))
        trace_id = str(request_payload.get("trace_id") or "").strip() or f"trace_{uuid.uuid4().hex[:12]}"
        started_at = time.perf_counter()
        _emit_event(
            "mcp.call.started",
            {
                "trace_id": trace_id,
                "server": server_name or "local",
                "name": name,
                "arguments": dict(arguments),
                "confirm": confirm,
            },
        )
        try:
            if server_name:
                _register_external_mcp_tools()
                try:
                    payload_preview = json.dumps(arguments, ensure_ascii=False)
                except Exception:
                    payload_preview = str(arguments)
                policy = policy_engine.evaluate(
                    intent="mcp.external.call",
                    text=f"{server_name}.{name} {payload_preview}",
                    metadata={"server": server_name, "name": name, "arguments": arguments},
                )
                if policy.action == "blocked":
                    raise HTTPException(status_code=403, detail=policy.reason)
                approval_key = _approval_key(server_name, name)
                if policy.action == "confirm" and not confirm and approval_key not in approved_external_mcp_tools:
                    latency_ms = int((time.perf_counter() - started_at) * 1000)
                    _emit_event(
                        "mcp.call.needs_confirmation",
                        {
                            "trace_id": trace_id,
                            "server": server_name,
                            "name": name,
                            "policy_action": policy.action,
                            "policy_reason": policy.reason,
                            "latency_ms": latency_ms,
                        },
                    )
                    return {
                        "ok": False,
                        "mode": "external",
                        "trace_id": trace_id,
                        "needs_confirmation": True,
                        "server": server_name,
                        "name": name,
                        "policy": {
                            "action": policy.action,
                            "reason": policy.reason,
                            "matched_rule": policy.matched_rule,
                        },
                    }
                result = await mcp_client.call_tool(server=server_name, name=name, arguments=arguments)
                approved_external_mcp_tools.add(approval_key)
                latency_ms = int((time.perf_counter() - started_at) * 1000)
                _emit_event(
                    "mcp.call.completed",
                    {
                        "trace_id": trace_id,
                        "mode": "external",
                        "server": server_name,
                        "name": name,
                        "latency_ms": latency_ms,
                        "result_preview": _preview(result, max_chars=260),
                    },
                )
                return {
                    "ok": True,
                    "mode": "external",
                    "trace_id": trace_id,
                    "server": server_name,
                    "name": name,
                    "result": result,
                    "latency_ms": latency_ms,
                }
            result = await mcp_server.call_tool(name=name, arguments=arguments)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "mcp.call.completed",
                {
                    "trace_id": trace_id,
                    "mode": "local",
                    "server": "local",
                    "name": name,
                    "latency_ms": latency_ms,
                    "result_preview": _preview(result, max_chars=260),
                },
            )
            return {"ok": True, "mode": "local", "trace_id": trace_id, "latency_ms": latency_ms, **result}
        except HTTPException:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "mcp.call.failed",
                {
                    "trace_id": trace_id,
                    "server": server_name or "local",
                    "name": name,
                    "error": "http_exception",
                    "latency_ms": latency_ms,
                },
            )
            raise
        except KeyError as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "mcp.call.failed",
                {
                    "trace_id": trace_id,
                    "server": server_name or "local",
                    "name": name,
                    "error": str(exc)[:500],
                    "latency_ms": latency_ms,
                },
            )
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "mcp.call.failed",
                {
                    "trace_id": trace_id,
                    "server": server_name or "local",
                    "name": name,
                    "error": str(exc)[:500],
                    "latency_ms": latency_ms,
                },
            )
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _emit_event(
                "mcp.call.failed",
                {
                    "trace_id": trace_id,
                    "server": server_name or "local",
                    "name": name,
                    "error": str(exc)[:500],
                    "latency_ms": latency_ms,
                },
            )
            raise HTTPException(status_code=500, detail=str(exc)[:500]) from exc

    @app.post("/api/system/route/resolve")
    async def resolve_route(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        text = str(request_payload.get("text") or "").strip()
        hinted_intent = str(request_payload.get("intent") or "").strip()
        route = intent_router.resolve(text)
        policy = policy_engine.evaluate(
            intent=hinted_intent or route.intent,
            text=text,
            metadata=request_payload,
        )
        return {
            "text": text,
            "route": {
                "intent": route.intent,
                "target": route.target,
                "method": route.method,
                "confidence": route.confidence,
                "reason": route.reason,
            },
            "policy": {
                "action": policy.action,
                "reason": policy.reason,
                "matched_rule": policy.matched_rule,
            },
        }

    @app.post("/api/channel/cli/ingest")
    async def cli_ingest(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
        request_payload = payload or {}
        text = str(request_payload.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="text is required")
        adapter = channel_adapters["cli"]
        message = adapter.parse_incoming(text)
        if message is None:
            raise HTTPException(status_code=400, detail="invalid cli payload")
        user_id = str(request_payload.get("user_id") or "").strip()
        if user_id:
            message.user_id = user_id
        metadata = request_payload.get("metadata")
        if isinstance(metadata, dict):
            message.metadata.update(metadata)
        result = adapter.dispatch(message)
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "result": result}

    @app.post("/api/channel/feishu/events")
    async def feishu_events(
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        request_payload = payload or {}
        challenge = request_payload.get("challenge")
        if isinstance(challenge, str) and challenge:
            return {"challenge": challenge}

        body_bytes = await request.body()
        body_text = body_bytes.decode("utf-8", errors="ignore")
        secret = settings.feishu_sign_secret.strip()
        if secret:
            signature = request.headers.get("X-Lark-Signature", "")
            timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
            nonce = request.headers.get("X-Lark-Request-Nonce", "")
            if not verify_feishu_signature(
                secret=secret,
                timestamp=timestamp,
                nonce=nonce,
                body=body_text or json.dumps(request_payload, ensure_ascii=False),
                signature=signature,
            ):
                raise HTTPException(status_code=401, detail="invalid feishu signature")

        adapter = channel_adapters["feishu"]
        message = adapter.parse_incoming(request_payload)
        if message is None:
            return {"ok": True, "ignored": True}
        result = adapter.dispatch(message)
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "ignored": False, "result": result}

    @app.get("/api/channel/wechat-work/events")
    async def wechat_work_verify(
        msg_signature: str = "",
        timestamp: str = "",
        nonce: str = "",
        echostr: str = "",
    ) -> Response:
        """URL verification callback for WeCom."""
        adapter: WechatWorkChannelAdapter = channel_adapters["wechat-work"]  # type: ignore[assignment]
        if not adapter.configured:
            raise HTTPException(status_code=503, detail="wechat-work channel not configured")
        decrypted = adapter.verify_url(msg_signature, timestamp, nonce, echostr)
        if decrypted is None:
            raise HTTPException(status_code=403, detail="signature verification failed")
        return Response(content=decrypted, media_type="text/plain")

    @app.post("/api/channel/wechat-work/events")
    async def wechat_work_events(
        request: Request,
        msg_signature: str = "",
        timestamp: str = "",
        nonce: str = "",
    ) -> Response:
        """Receive encrypted messages from WeCom."""
        adapter: WechatWorkChannelAdapter = channel_adapters["wechat-work"]  # type: ignore[assignment]
        if not adapter.configured:
            raise HTTPException(status_code=503, detail="wechat-work channel not configured")

        body_bytes = await request.body()
        xml_body = body_bytes.decode("utf-8", errors="ignore")

        message = adapter.parse_incoming({
            "xml_body": xml_body,
            "msg_signature": msg_signature,
            "timestamp": timestamp,
            "nonce": nonce,
        })
        if message is None:
            return Response(content="success", media_type="text/plain")

        result = adapter.dispatch(message)
        if inspect.isawaitable(result):
            result = await result

        reply_text = ""
        if isinstance(result, dict):
            reply_text = str(result.get("reply") or result.get("answer") or "")
            if not reply_text and "brain" in result:
                reply_text = ""
            brain_result = result.get("result")
            if not reply_text and "brain" not in result and isinstance(brain_result, dict):
                reply_text = str(brain_result.get("answer") or brain_result.get("text") or "")
            elif not reply_text and "brain" not in result and isinstance(brain_result, str):
                reply_text = brain_result
        if reply_text:
            adapter.send(OutgoingMessage(
                channel="wechat-work",
                target_id=message.user_id,
                text=reply_text,
            ))

        return Response(content="success", media_type="text/plain")

    module_registry.attach_to_app(app)
    return app
