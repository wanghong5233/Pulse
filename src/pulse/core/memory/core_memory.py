from __future__ import annotations

import hashlib
import json
import logging
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from ..event_types import EventTypes, make_payload
from ..logging_config import get_trace_id

logger = logging.getLogger(__name__)

EventEmitter = Callable[[str, dict[str, Any]], None]


def _content_hash(value: Any) -> str:
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        data = str(value).encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()[:16]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _resolve_path(raw_path: str | None, *, default_path: Path) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        return default_path
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return (_repo_root() / candidate).resolve()


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass

    try:
        import yaml  # type: ignore

        parsed_yaml = yaml.safe_load(text)
        return parsed_yaml if isinstance(parsed_yaml, dict) else {}
    except Exception as exc:
        logger.warning("failed to parse core memory config as yaml: %s", exc)
        return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)  # type: ignore[index]
        else:
            merged[key] = deepcopy(value)
    return merged


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _split_tagged_values(values: list[str]) -> tuple[list[str], list[str]]:
    core_items: list[str] = []
    mutable_items: list[str] = []
    for item in values:
        safe = str(item).strip()
        lowered = safe.lower()
        if lowered.startswith("[core]"):
            core_items.append(safe)
        elif lowered.startswith("[mutable]"):
            mutable_items.append(safe)
    return core_items, mutable_items


class CoreMemory:
    """Persistent core memory blocks: soul, user, prefs, context."""

    def __init__(
        self,
        *,
        storage_path: str | None = None,
        soul_config_path: str | None = None,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        default_storage = Path.home() / ".pulse" / "core_memory.json"
        self._storage_path = _resolve_path(storage_path, default_path=default_storage)
        self._soul_config_path = _resolve_path(
            soul_config_path,
            default_path=(_repo_root() / "config" / "soul.yaml").resolve(),
        )
        self._lock = threading.Lock()
        self._data = self._build_default_data()
        self._load_persisted()
        self._event_emitter = event_emitter

    def bind_event_emitter(self, emitter: EventEmitter | None) -> None:
        self._event_emitter = emitter

    def _emit(self, event_type: str, **fields: Any) -> None:
        emitter = self._event_emitter
        if emitter is None:
            return
        try:
            payload = make_payload(
                trace_id=get_trace_id(),
                actor="core_memory",
                **fields,
            )
            emitter(event_type, payload)
        except Exception:  # pragma: no cover - 观测侧不阻塞主流程
            logger.debug("core_memory event emit failed", exc_info=True)

    def _build_default_data(self) -> dict[str, Any]:
        default_soul = {
            "assistant_prefix": "Pulse",
            "role": "个人智能助手",
            "tone": "calm, pragmatic, concise",
            "principles": [
                "Respect user intent and safety policies.",
                "Prefer actionable and testable outputs.",
                "Explain trade-offs briefly when relevant.",
            ],
            "style_rules": [
                "Answer in concise structured Chinese.",
                "Be direct and avoid unnecessary filler.",
            ],
            "boundaries": [],
        }
        soul_file = _load_yaml_or_json(self._soul_config_path)
        legacy_soul = soul_file.get("soul") if isinstance(soul_file.get("soul"), dict) else {}
        merged_soul = _deep_merge(default_soul, legacy_soul) if isinstance(legacy_soul, dict) else deepcopy(default_soul)

        identity = soul_file.get("identity") if isinstance(soul_file.get("identity"), dict) else {}
        style = soul_file.get("style") if isinstance(soul_file.get("style"), dict) else {}
        values = _string_list(soul_file.get("values"))
        boundaries = _string_list(soul_file.get("boundaries"))

        name = str(identity.get("name") or "").strip()
        role = str(identity.get("role") or "").strip()
        if name:
            merged_soul["assistant_prefix"] = name
        if role:
            merged_soul["role"] = role
        tone = str(style.get("tone") or "").strip()
        if tone:
            merged_soul["tone"] = tone
        style_rules = _string_list(style.get("rules"))
        if style_rules:
            merged_soul["style_rules"] = style_rules
        if values:
            merged_soul["principles"] = values
        if boundaries:
            merged_soul["boundaries"] = boundaries

        core_beliefs, mutable_beliefs = _split_tagged_values(values)
        return {
            "soul": merged_soul,
            "user": {"name": "", "goals": []},
            # prefs 保持空 dict: CoreMemory 只承载**跨 domain**的通用偏好,
            # 业务专属字段 (求职城市 / 薪资等) 走各自 Domain Profile, 见
            # ``src/pulse/modules/job/profile/schema.py`` 与
            # ``docs/Pulse-DomainMemory与Tool模式.md`` 分层约定。
            "prefs": {},
            "context": {
                "beliefs": {
                    "core": core_beliefs or [
                        "Respect user intent and safety policies.",
                        "Do not execute dangerous actions without confirmation.",
                    ],
                    "mutable": mutable_beliefs,
                }
            },
        }

    def _load_persisted(self) -> None:
        path = self._storage_path
        if not path.is_file():
            return
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("failed to load persisted core memory: %s", exc)
            return
        if not isinstance(parsed, dict):
            return
        with self._lock:
            self._data = _deep_merge(self._data, parsed)

    def _save(self) -> None:
        path = self._storage_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._data)

    def read_block(self, block: str) -> Any:
        key = str(block or "").strip().lower()
        with self._lock:
            return deepcopy(self._data.get(key))

    def update_block(self, *, block: str, content: Any, merge: bool = True) -> dict[str, Any]:
        key = str(block or "").strip().lower()
        if key not in {"soul", "user", "prefs", "context"}:
            raise ValueError(f"unsupported core memory block: {key}")
        with self._lock:
            current = self._data.get(key)
            hash_before = _content_hash(current)
            if merge and isinstance(current, dict) and isinstance(content, dict):
                self._data[key] = _deep_merge(current, content)
            else:
                self._data[key] = deepcopy(content)
            self._save()
            updated = deepcopy(self._data[key])
        hash_after = _content_hash(updated)
        if hash_before != hash_after:
            logger.info(
                "core_memory_updated block=%s merge=%s hash=%s->%s",
                key,
                merge,
                hash_before,
                hash_after,
            )
            self._emit(
                EventTypes.MEMORY_CORE_UPDATED,
                block=key,
                merge=merge,
                hash_before=hash_before,
                hash_after=hash_after,
            )
        return updated

    def update_preferences(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise ValueError("updates must be a dict")
        return self.update_block(block="prefs", content=updates, merge=True)

    def preference(self, key: str, default: Any = None) -> Any:
        safe_key = str(key or "").strip()
        with self._lock:
            prefs = self._data.get("prefs")
            if not isinstance(prefs, dict):
                return default
            return deepcopy(prefs.get(safe_key, default))

    def build_system_prompt(self, *, max_chars: int = 1200) -> str:
        snapshot = self.snapshot()
        soul = snapshot.get("soul") if isinstance(snapshot.get("soul"), dict) else {}
        user = snapshot.get("user") if isinstance(snapshot.get("user"), dict) else {}
        prefs = snapshot.get("prefs") if isinstance(snapshot.get("prefs"), dict) else {}
        context = snapshot.get("context") if isinstance(snapshot.get("context"), dict) else {}

        lines = [
            "You are Pulse assistant.",
            f"Role: {soul.get('role', '')}",
            f"Tone: {soul.get('tone', '')}",
            f"Assistant prefix: {soul.get('assistant_prefix', 'Pulse')}",
            f"Style rules: {soul.get('style_rules', [])}",
            f"Boundaries: {soul.get('boundaries', [])}",
            f"User profile: {user}",
            f"User preferences: {prefs}",
            f"Context: {context}",
        ]
        text = "\n".join(lines).strip()
        if len(text) > max_chars:
            return text[:max_chars] + "...(truncated)"
        return text
