"""feedback_loop — Closed-loop preference learning from user feedback.

Architecture spec §2.4 / §2.8: Phase 3 module that collects user feedback
(ratings, corrections, implicit signals) and feeds them into the preference
learning pipeline (Track A) and optional DPO collection (Track B).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter

from ....core.module import BaseModule

logger = logging.getLogger(__name__)


class FeedbackLoopModule(BaseModule):
    name = "feedback_loop"
    description = "Collect user feedback and drive preference learning"
    route_prefix = "/api/modules/system/feedback"

    def __init__(self) -> None:
        super().__init__()
        self._evolution_engine: Any | None = None

    def bind_evolution_engine(self, evolution_engine: Any | None) -> None:
        self._evolution_engine = evolution_engine

    def register_routes(self, router: APIRouter) -> None:
        @router.post("/submit")
        async def submit_feedback(payload: dict[str, Any]) -> dict[str, Any]:
            feedback_type = str(payload.get("type") or "general").strip()
            content = str(payload.get("content") or "").strip()
            rating = payload.get("rating")
            session_id = str(payload.get("session_id") or "default").strip()
            assistant_text = str(payload.get("assistant_text") or payload.get("previous_assistant_text") or "").strip()
            collect_dpo_raw = payload.get("collect_dpo")
            collect_dpo = collect_dpo_raw if isinstance(collect_dpo_raw, bool) else None
            metadata_raw = payload.get("metadata")
            metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
            if not content:
                return {"ok": False, "error": "content is required"}
            return self._process_feedback(
                feedback_type=feedback_type,
                content=content,
                rating=rating,
                session_id=session_id,
                assistant_text=assistant_text,
                collect_dpo=collect_dpo,
                metadata=metadata,
            )

        @router.get("/stats")
        async def feedback_stats() -> dict[str, Any]:
            return {
                "module": self.name,
                "status": "active",
                "evolution_bound": self._evolution_engine is not None,
            }

    def _process_feedback(
        self,
        *,
        feedback_type: str,
        content: str,
        rating: Any,
        session_id: str,
        assistant_text: str = "",
        collect_dpo: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace_id = self.emit_stage_event(
            stage="submit",
            status="started",
            trace_id=str((metadata or {}).get("trace_id") or "").strip() or None,
            payload={
                "feedback_type": feedback_type,
                "session_id": session_id,
                "assistant_text_present": bool(assistant_text),
            },
        )
        try:
            from ...core.storage.engine import DatabaseEngine
            import uuid

            safe_metadata = dict(metadata or {})
            safe_metadata["session_id"] = session_id
            safe_metadata["trace_id"] = trace_id
            if collect_dpo is None:
                safe_metadata["collect_dpo"] = bool(assistant_text)
            else:
                safe_metadata["collect_dpo"] = bool(collect_dpo)

            db = DatabaseEngine()
            db.execute(
                """INSERT INTO corrections(id, session_id, user_text, assistant_text, correction_json, created_at)
                   VALUES (%s, %s, %s, %s, %s::jsonb, NOW())""",
                (
                    uuid.uuid4().hex,
                    session_id,
                    content[:2000],
                    assistant_text[:2000],
                    json.dumps(
                        {
                            "type": feedback_type,
                            "rating": rating,
                            "trace_id": trace_id,
                            "assistant_text_present": bool(assistant_text),
                            "collect_dpo": bool(safe_metadata.get("collect_dpo")),
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            evolution: dict[str, Any] | None = None
            evolution_error = ""
            if self._evolution_engine is not None:
                try:
                    evolution_result = self._evolution_engine.reflect_interaction(
                        user_text=content,
                        assistant_text=assistant_text,
                        metadata=safe_metadata,
                    )
                    evolution = evolution_result.to_dict()
                except Exception as exc:
                    evolution_error = str(exc)[:200]
                    logger.warning("feedback evolution failed: %s", exc)
            result = {
                "ok": True,
                "type": feedback_type,
                "recorded": True,
                "trace_id": trace_id,
                "assistant_text_present": bool(assistant_text),
                "collect_dpo": bool(safe_metadata.get("collect_dpo")),
                "evolution": evolution,
                "evolution_error": evolution_error or None,
            }
            self.emit_stage_event(
                stage="submit",
                status="completed",
                trace_id=trace_id,
                payload={
                    "feedback_type": feedback_type,
                    "session_id": session_id,
                    "evolution_applied": bool(evolution),
                    "collect_dpo": bool(safe_metadata.get("collect_dpo")),
                },
            )
            return result
        except Exception as exc:
            logger.warning("feedback recording failed: %s", exc)
            self.emit_stage_event(
                stage="submit",
                status="failed",
                trace_id=trace_id,
                payload={
                    "feedback_type": feedback_type,
                    "session_id": session_id,
                    "error": str(exc)[:200],
                },
            )
            return {"ok": False, "trace_id": trace_id, "error": str(exc)[:200]}

    def handle_intent(
        self,
        intent: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        if intent in ("feedback.submit", "feedback_loop"):
            return self._process_feedback(
                feedback_type="intent",
                content=text,
                rating=None,
                session_id=str((metadata or {}).get("session_id") or "default"),
                assistant_text=str((metadata or {}).get("assistant_text") or ""),
                collect_dpo=(metadata or {}).get("collect_dpo") if isinstance((metadata or {}).get("collect_dpo"), bool) else None,
                metadata=dict(metadata or {}),
            )
        return None


def get_module() -> FeedbackLoopModule:
    return FeedbackLoopModule()
