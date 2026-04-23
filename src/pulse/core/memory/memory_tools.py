from __future__ import annotations

import logging
from typing import Any

from ..tool import ToolRegistry, tool
from .archival_memory import ArchivalMemory
from .core_memory import CoreMemory
from .recall_memory import RecallMemory

logger = logging.getLogger(__name__)


def register_memory_tools(
    registry: ToolRegistry,
    *,
    core_memory: CoreMemory,
    recall_memory: RecallMemory,
    archival_memory: ArchivalMemory | None = None,
) -> None:
    @tool(
        name="memory_read",
        description="Read a core memory block (soul/user/prefs/context) or the full snapshot.",
        when_to_use=(
            "按 block 名读取 CoreMemory 的一个分区 (soul / user / prefs / context)。"
            "block 缺省返回全量 snapshot, token 成本约 4x 单 block, 只在需要全局概览时缺省。"
            "纯读, 不修改任何状态。"
        ),
        when_not_to_use=(
            "职责划分: 1) 对话历史 / 话题检索 → `memory_search`; "
            "2) 三元组事实反查 → `memory_archive` 的反向查询或 domain 专用工具; "
            "3) 写入 / 更新 → `memory_update`; "
            "4) domain 专用状态 (job profile / snapshot 等) → 对应 domain 的 `*.snapshot.get`。"
        ),
        schema={
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "One of: soul, user, prefs, context. Omit for full snapshot."},
            },
        },
    )
    def _memory_read(args: dict[str, Any]) -> dict[str, Any]:
        block = str(args.get("block") or "").strip().lower()
        if block:
            return {
                "block": block,
                "value": core_memory.read_block(block),
            }
        return {"snapshot": core_memory.snapshot()}

    @tool(
        name="memory_update",
        description="Update a core memory block (soul/user/context) or merge into prefs dict.",
        when_to_use=(
            "写入 / 合并 CoreMemory 的一个分区, 用于**跨会话持久、领域无关**的画像 / 偏好事实。"
            "参数约束: block=prefs 时 content 必须是 dict (走 update_preferences); "
            "block=soul/user/context 时 content 可为字符串或结构化值 (走 update_block); "
            "merge=true (默认) 与现有内容合并, false 则整块覆盖。"
        ),
        when_not_to_use=(
            "职责划分: 1) 对话流水 → RecallMemory 由 kernel 自动落, 无需手工调用本工具; "
            "2) 结构化三元组事实 (subject-predicate-object) → `memory_archive`; "
            "3) 领域专用偏好 (招聘 / 投资 / 学习等) → 对应 domain 的专属工具 (如 `job.memory.record` / "
            "`job.hard_constraint.set`), 不写入 CoreMemory 以免污染全局画像。"
        ),
        schema={
            "type": "object",
            "properties": {
                "block": {"type": "string", "description": "Block name: soul, user, prefs, context"},
                "content": {"description": "Content to update (dict for prefs)"},
                "merge": {"type": "boolean", "description": "Whether to merge with existing (default true)"},
            },
        },
    )
    def _memory_update(args: dict[str, Any]) -> dict[str, Any]:
        block = str(args.get("block") or "prefs").strip().lower()
        merge = bool(args.get("merge", True))
        content = args.get("content")
        if block == "prefs":
            if not isinstance(content, dict):
                raise ValueError("prefs update requires dict content")
            updated = core_memory.update_preferences(content)
            return {"block": "prefs", "updated": updated}
        updated = core_memory.update_block(block=block, content=content, merge=merge)
        return {"block": block, "updated": updated}

    @tool(
        name="memory_search",
        description=(
            "Search recall memory (conversation history) by keyword(s). "
            "Caller is responsible for expanding synonyms / variants of the query term "
            "before invoking (agentic search pattern)."
        ),
        when_to_use=(
            "对 RecallMemory (对话流水) 做 SQL ILIKE 关键词检索, 按 created_at DESC 排序返回候选。"
            "agentic search 契约: keywords 由调用方负责做同义词 / 变体扩展, 内核不改写; "
            "match=any 对应 OR, all 对应 AND; top_k 控制返回量 (默认 5)。"
            "结果是**候选行**, 语义排序 / 判断归 LLM 本身。"
        ),
        when_not_to_use=(
            "职责划分: 1) 画像 / 偏好 / 角色分区 → `memory_read`; "
            "2) 三元组事实检索 → `memory_archive` 反查; "
            "3) 公共互联网 → `web.search`; "
            "4) domain 专用的结构化历史 (投递 / 打招呼 / HR 对话) → 对应 domain 工具 (`job.*`), "
            "不走 RecallMemory。"
        ),
        schema={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of keywords / synonyms to match against conversation text.",
                },
                "query": {
                    "type": "string",
                    "description": "Single-keyword shortcut (used when `keywords` is empty).",
                },
                "match": {
                    "type": "string",
                    "enum": ["any", "all"],
                    "description": "any = OR match, all = AND match (default: any).",
                },
                "top_k": {"type": "integer", "description": "Max results (default 5)"},
                "session_id": {"type": "string", "description": "Optional session filter"},
            },
        },
    )
    def _memory_search(args: dict[str, Any]) -> dict[str, Any]:
        keywords_raw = args.get("keywords")
        if isinstance(keywords_raw, list) and keywords_raw:
            keywords = [str(k).strip() for k in keywords_raw if str(k or "").strip()]
        else:
            q = str(args.get("query") or "").strip()
            keywords = [q] if q else []
        if not keywords:
            raise ValueError("keywords (or query) is required")
        top_k_raw = args.get("top_k", 5)
        try:
            top_k = int(top_k_raw)
        except (TypeError, ValueError):
            logger.debug("memory_tools: top_k=%r not int, using default 5", top_k_raw)
            top_k = 5
        session_id = str(args.get("session_id") or "").strip() or None
        match_mode = str(args.get("match") or "any").strip().lower()
        rows = recall_memory.search_keyword(
            keywords=keywords,
            top_k=top_k,
            session_id=session_id,
            match=match_mode,
        )
        return {"keywords": keywords, "total": len(rows), "items": rows}

    @tool(
        name="memory_archive",
        description="Store an important fact as a subject-predicate-object triple in archival memory.",
        when_to_use=(
            "把一条**稳定的长期事实**以 (subject, predicate, object) 三元组写入 ArchivalMemory, "
            "供跨会话反查。confidence ∈ [0,1] (缺省 1.0), source 必须可溯源 "
            "(例: 'user_statement' / 'inferred' / <ingestion_pipeline_name>)。"
            "典型三元组形态: (user, lives_in, Beijing) / (project_pulse, phase, M4)。"
        ),
        when_not_to_use=(
            "职责划分与质量阈: 1) 对话流水一次性内容 → RecallMemory 自动记录, 不写 archival; "
            "2) domain 专用事件 (投递 / 聊天 / 投资动作等) → 对应 domain 的 event 通道; "
            "3) confidence < 0.6 的事实先向用户确认, 不得直接入库; "
            "4) 易变信息 (心情 / 当前在做什么) 不属于 archival 语义。"
        ),
        schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Entity or topic (e.g. 'user', 'project_pulse')"},
                "predicate": {"type": "string", "description": "Relationship or attribute (e.g. 'prefers', 'lives_in')"},
                "object": {"type": "string", "description": "Value or target (e.g. 'remote work', 'Beijing')"},
                "confidence": {"type": "number", "description": "Confidence 0-1 (default 1.0)"},
                "source": {"type": "string", "description": "Source of this fact (e.g. 'user_statement', 'inferred')"},
            },
            "required": ["subject", "predicate", "object"],
        },
    )
    def _memory_archive(args: dict[str, Any]) -> dict[str, Any]:
        if archival_memory is None:
            return {"ok": False, "error": "archival memory not available"}
        subject = str(args.get("subject") or "").strip()
        predicate = str(args.get("predicate") or "").strip()
        obj = str(args.get("object") or "").strip()
        if not subject or not predicate or not obj:
            raise ValueError("subject, predicate, and object are all required")
        confidence = max(0.0, min(float(args.get("confidence") or 1.0), 1.0))
        source = str(args.get("source") or "brain").strip()
        result = archival_memory.add_fact(
            subject=subject,
            predicate=predicate,
            object_value=obj,
            confidence=confidence,
            source=source,
        )
        return {"ok": True, "fact": result}

    registry.register_callable(_memory_read)
    registry.register_callable(_memory_update)
    registry.register_callable(_memory_search)
    registry.register_callable(_memory_archive)
