from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ....core.intel_store import IntelKnowledgeStore
from ....core.llm.router import LLMRouter
from ....core.module import BaseModule
from ....core.notify.notifier import ConsoleNotifier, Notification
from ....core.notify.webhook import build_payload, post_webhook, resolve_webhook_url
from ....core.scheduler import BackgroundSchedulerRunner, ScheduleTask
from ....core.tools.web_search import search_web

logger = logging.getLogger(__name__)


class InterviewCollectRequest(BaseModel):
    keyword: str = Field(default="AI Agent", min_length=1, max_length=120)
    max_items: int = Field(default=10, ge=1, le=50)
    source: str = Field(default="web_search", max_length=30)


class InterviewDailyPushRequest(BaseModel):
    keyword: str = Field(default="AI Agent", min_length=1, max_length=120)
    max_items: int = Field(default=6, ge=1, le=30)
    channel: str = Field(default="feishu", max_length=30)


def _read_int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        value = default
    return max(min_value, min(value, max_value))


def _extract_keyword(text: str, *, fallback: str = "AI Agent") -> str:
    raw = str(text or "").strip()
    lowered = raw.lower()
    for prefix in ("/intel interview collect", "/intel interview report", "interview"):
        if lowered.startswith(prefix):
            candidate = raw[len(prefix) :].strip()
            if candidate:
                return candidate
    return raw or fallback


def _infer_round(text: str) -> str:
    lowered = text.lower()
    if "hr" in lowered:
        return "HR 面"
    if "终面" in text or "final" in lowered:
        return "终面"
    if "二面" in text or "second" in lowered:
        return "二面"
    if "三面" in text or "third" in lowered:
        return "三面"
    return "一面"


def _infer_company(title: str, url: str) -> str:
    cleaned = re.sub(r"\s+", " ", title or "").strip()
    if cleaned:
        parts = re.split(r"[-_|:：·]+", cleaned)
        if parts:
            head = parts[0].strip()
            if len(head) >= 2:
                return head[:40]
    if "://" in url:
        host = url.split("://", 1)[1].split("/", 1)[0]
        if host:
            return host[:40]
    return "Unknown"


def _infer_role(keyword: str, text: str) -> str:
    lowered = text.lower()
    if "intern" in lowered or "实习" in text:
        return "Intern"
    if "engineer" in lowered or "工程师" in text:
        return "Engineer"
    if "product" in lowered or "产品" in text:
        return "Product"
    return f"{keyword} Candidate"


def _build_tips(text: str) -> str:
    lowered = text.lower()
    if "rag" in lowered:
        return "重点准备 RAG 召回、重排和线上评测指标。"
    if "mcp" in lowered:
        return "准备 MCP tool schema、超时治理和调用审计案例。"
    if "agent" in lowered or "智能体" in text:
        return "准备多工具编排、失败恢复和成本控制案例。"
    return "准备一个可观测、可回滚的端到端项目案例。"


def _collect_web_search_reports(keyword: str, *, max_items: int) -> tuple[list[dict[str, Any]], list[str]]:
    safe_keyword = keyword.strip() or "AI Agent"
    safe_max = max(1, min(max_items, 50))
    queries = (
        f"{safe_keyword} 面经",
        f"{safe_keyword} interview experience",
        f"{safe_keyword} 实习 面试",
    )
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for query in queries:
        if len(rows) >= safe_max:
            break
        try:
            hits = search_web(query, max_results=min(10, safe_max * 2))
        except Exception as exc:
            errors.append(str(exc)[:240])
            continue
        for hit in hits:
            if len(rows) >= safe_max:
                break
            cache_key = f"{hit.url}|{hit.title}".strip().lower()
            if not cache_key or cache_key in seen:
                continue
            seen.add(cache_key)
            source_text = f"{hit.title} {hit.snippet}".strip()
            digest = hashlib.sha1(cache_key.encode("utf-8")).hexdigest()[:12]
            rows.append(
                {
                    "id": f"iv-{digest}",
                    "keyword": safe_keyword,
                    "company": _infer_company(hit.title, hit.url),
                    "role": _infer_role(safe_keyword, source_text),
                    "round": _infer_round(source_text),
                    "highlights": (hit.snippet or hit.title or "")[:300],
                    "tips": _build_tips(source_text),
                    "title": hit.title,
                    "source_url": hit.url,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
            )
    return rows, errors


class IntelInterviewModule(BaseModule):
    name = "intel_interview"
    description = "Interview intelligence collector (web-search pipeline)"
    route_prefix = "/api/modules/intel/interview"
    tags = ["intel", "intel_interview"]

    def __init__(self) -> None:
        super().__init__()
        self._notifier = ConsoleNotifier()
        self._scheduler: BackgroundSchedulerRunner | None = None
        self._scheduler_lock = threading.Lock()
        self._knowledge_store = IntelKnowledgeStore(storage_path=os.getenv("PULSE_INTEL_KNOWLEDGE_PATH", ""))
        self._llm_router = LLMRouter()

    def _enrich_items_with_llm(self, *, keyword: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for item in items:
            prompt = (
                "You extract interview-intelligence fields from web snippets. "
                "Return ONLY valid JSON with keys: "
                "{\"company\":\"...\",\"role\":\"...\",\"round\":\"...\","
                "\"highlights\":\"...\",\"tips\":\"...\"}\n\n"
                f"Keyword: {keyword}\n"
                f"Title: {str(item.get('title') or '')[:300]}\n"
                f"Snippet: {str(item.get('highlights') or '')[:800]}\n"
                f"Source URL: {str(item.get('source_url') or '')[:300]}"
            )
            try:
                raw = self._llm_router.invoke_text(prompt, route="classification")
                cleaned = raw.strip()
                if cleaned.startswith("```"):
                    lines = cleaned.split("\n")
                    cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                parsed = json.loads(cleaned)
                merged = dict(item)
                for key in ("company", "role", "round", "highlights", "tips"):
                    value = str(parsed.get(key) or "").strip()
                    if value:
                        merged[key] = value
                enriched.append(merged)
            except Exception as exc:
                logger.warning("intel_interview llm enrichment failed: %s", exc)
                enriched.append(dict(item))
        return enriched

    @staticmethod
    def _to_knowledge_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in items:
            rows.append(
                {
                    "id": item.get("id"),
                    "category": "interview",
                    "title": str(item.get("title") or item.get("company") or "Interview Intel"),
                    "content": str(item.get("highlights") or ""),
                    "summary": str(item.get("tips") or ""),
                    "source_url": str(item.get("source_url") or ""),
                    "source": "web_search",
                    "tags": ["interview", "intel", str(item.get("keyword") or "").strip()],
                    "collected_at": str(item.get("collected_at") or datetime.now(timezone.utc).isoformat()),
                    "metadata": {
                        "company": item.get("company"),
                        "role": item.get("role"),
                        "round": item.get("round"),
                    },
                }
            )
        return rows

    def run_collect(self, *, keyword: str, max_items: int, source: str = "web_search") -> dict[str, Any]:
        requested_source = str(source or "web_search").strip().lower() or "web_search"
        source_aliases = {"web_search": "web_search", "real": "web_search"}
        effective_source = source_aliases.get(requested_source)
        if effective_source is None:
            return {
                "ok": False,
                "keyword": keyword,
                "source": requested_source,
                "total": 0,
                "items": [],
                "errors": [f"unsupported source={requested_source}"],
            }
        items, errors = _collect_web_search_reports(keyword, max_items=max_items)
        if items:
            items = self._enrich_items_with_llm(keyword=keyword, items=items)
        persisted_docs = self._knowledge_store.append(self._to_knowledge_rows(items))
        return {
            "ok": len(items) > 0,
            "keyword": keyword,
            "source": effective_source,
            "requested_source": requested_source,
            "source_alias_applied": requested_source != effective_source,
            "collect_pipeline": "web_search",
            "total": len(items),
            "items": items,
            "persisted_docs": persisted_docs,
            "errors": errors,
        }

    def run_daily_report(self, *, keyword: str, max_items: int) -> dict[str, Any]:
        collect_result = self.run_collect(keyword=keyword, max_items=max_items, source="web_search")
        items = list(collect_result.get("items") or [])
        companies = sorted({str(item.get("company") or "") for item in items if item.get("company")})
        return {
            "keyword": keyword,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "report_count": len(items),
                "companies": companies,
                "persisted_docs": int(collect_result.get("persisted_docs") or 0),
            },
            "items": items,
            "errors": list(collect_result.get("errors") or []),
        }

    def run_daily_push(self, *, keyword: str, max_items: int, channel: str) -> dict[str, Any]:
        report = self.run_daily_report(keyword=keyword, max_items=max_items)
        report_count = int(report["summary"]["report_count"])
        safe_channel = str(channel or "").strip().lower() or "feishu"
        self._notifier.send(
            Notification(
                level="info",
                title="intel_interview daily report",
                content=f"keyword={keyword}; count={report_count}",
                metadata={"channel": safe_channel},
            )
        )

        delivery = {"webhook_sent": False, "webhook_error": None}
        if safe_channel == "feishu":
            payload = build_payload(
                f"已生成 {report_count} 条面经情报摘要。",
                mode="feishu_card",
                title="Pulse 面经日报",
                level="info",
                fields=[("关键词", keyword), ("条目数", str(report_count))],
                footer_text="Pulse",
            )
            webhook_url = os.getenv("FEISHU_WEBHOOK_URL", "").strip() or resolve_webhook_url()
            ok, err = post_webhook(payload, webhook_url=webhook_url)
            delivery["webhook_sent"] = bool(ok)
            delivery["webhook_error"] = err

        return {
            "ok": True,
            "channel": safe_channel,
            "pushed_count": report_count,
            "delivery": delivery,
            "report": report,
        }

    def _get_scheduler(self) -> BackgroundSchedulerRunner:
        with self._scheduler_lock:
            if self._scheduler is not None:
                return self._scheduler

            tick_seconds = _read_int_env(
                "INTEL_INTERVIEW_SCHED_TICK_SEC",
                15,
                min_value=1,
                max_value=3600,
            )
            interval_seconds = _read_int_env(
                "INTEL_INTERVIEW_PUSH_INTERVAL_SEC",
                3600,
                min_value=30,
                max_value=24 * 3600,
            )
            keyword = os.getenv("INTEL_INTERVIEW_DEFAULT_KEYWORD", "AI Agent").strip() or "AI Agent"
            max_items = _read_int_env(
                "INTEL_INTERVIEW_DEFAULT_MAX_ITEMS",
                6,
                min_value=1,
                max_value=30,
            )
            channel = os.getenv("INTEL_INTERVIEW_DEFAULT_CHANNEL", "feishu").strip() or "feishu"

            scheduler = BackgroundSchedulerRunner(tick_seconds=tick_seconds)
            scheduler.register(
                ScheduleTask(
                    name=f"{self.name}.daily_push",
                    interval_seconds=interval_seconds,
                    run_immediately=True,
                    handler=lambda: self.run_daily_push(
                        keyword=keyword,
                        max_items=max_items,
                        channel=channel,
                    ),
                )
            )
            self._scheduler = scheduler
            return scheduler

    def on_shutdown(self) -> None:
        scheduler = self._scheduler
        if scheduler is not None:
            scheduler.stop()

    def handle_intent(
        self,
        intent: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        _ = metadata
        if intent == "intel.interview.collect":
            keyword = _extract_keyword(text)
            return self.run_collect(keyword=keyword, max_items=6, source="web_search")
        if intent == "intel.interview.report":
            keyword = _extract_keyword(text)
            return self.run_daily_report(keyword=keyword, max_items=6)
        return None

    def register_routes(self, router: APIRouter) -> None:
        @router.get("/health")
        async def health() -> dict[str, Any]:
            return {
                "module": self.name,
                "status": "ok",
                "mode": "web_search",
                "collect_pipeline": "web_search",
                "knowledge_path": self._knowledge_store.storage_path,
            }

        @router.post("/collect")
        async def collect(payload: InterviewCollectRequest) -> dict[str, Any]:
            return self.run_collect(
                keyword=payload.keyword,
                max_items=payload.max_items,
                source=payload.source,
            )

        @router.get("/daily-report")
        async def daily_report(keyword: str = "AI Agent", max_items: int = 6) -> dict[str, Any]:
            return self.run_daily_report(
                keyword=keyword,
                max_items=max(1, min(max_items, 30)),
            )

        @router.post("/daily-push")
        async def daily_push(payload: InterviewDailyPushRequest) -> dict[str, Any]:
            return self.run_daily_push(
                keyword=payload.keyword,
                max_items=payload.max_items,
                channel=payload.channel,
            )

        @router.get("/schedule/status")
        async def schedule_status() -> dict[str, Any]:
            scheduler = self._get_scheduler()
            return scheduler.status()

        @router.post("/schedule/start")
        async def schedule_start() -> dict[str, Any]:
            scheduler = self._get_scheduler()
            started = scheduler.start()
            return {"ok": True, "started": bool(started), "status": scheduler.status()}

        @router.post("/schedule/stop")
        async def schedule_stop() -> dict[str, Any]:
            scheduler = self._get_scheduler()
            stopped = scheduler.stop()
            return {"ok": True, "stopped": bool(stopped), "status": scheduler.status()}

        @router.post("/schedule/trigger")
        async def schedule_trigger() -> dict[str, Any]:
            scheduler = self._get_scheduler()
            ran_tasks = await scheduler.run_once()
            return {"ok": True, "ran_tasks": ran_tasks, "status": scheduler.status()}


module = IntelInterviewModule()
