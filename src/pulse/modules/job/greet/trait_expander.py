"""Expand trait labels into concrete company sets for deterministic gates.

This component keeps the "semantic understanding" in the LLM while making
runtime decisions deterministic:

1) LLM expands mass-noun company traits (e.g. ``大厂``) into concrete companies.
2) Expansion is cached in ``JobMemory`` (`job.derived.*`) with TTL.
3) ``JobGreetService`` performs literal company-name veto before matcher scoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pulse.core.llm.router import LLMRouter

from ..memory import JobMemory, JobMemorySnapshot, TraitCompanySet

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ExpandedTrait:
    trait: str
    companies: list[str]


class TraitCompanyExpander:
    """LLM-backed trait expander with durable cache."""

    _ROUTE = "job_match"
    _MAX_COMPANIES = 40

    def __init__(
        self,
        llm_router: LLMRouter,
        *,
        preferences: JobMemory | None,
        ttl_hours: int = 24 * 7,
    ) -> None:
        if ttl_hours <= 0:
            raise ValueError(f"ttl_hours must be >0, got {ttl_hours}")
        self._llm = llm_router
        self._preferences = preferences
        self._ttl = timedelta(hours=ttl_hours)

    def resolve_avoid_trait_companies(
        self,
        *,
        snapshot: JobMemorySnapshot | None,
    ) -> dict[str, set[str]]:
        """Resolve active ``avoid_trait`` labels into company-name sets.

        Returns:
            ``{trait -> {company_names...}}``.
            Traits classified as ``non_company_trait`` map to an empty set.

        Failure policy:
            - cache hit (fresh) → use cache
            - cache stale + refresh failed → use stale cache
            - no cache + refresh failed → raise ``RuntimeError`` (fail-loud)
        """
        if snapshot is None:
            return {}
        traits = self._active_avoid_traits(snapshot)
        if not traits:
            return {}
        out: dict[str, set[str]] = {}
        for trait in traits:
            record = self._resolve_one(trait=trait)
            out[trait] = set(record.companies)
        return out

    def _active_avoid_traits(self, snapshot: JobMemorySnapshot) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in snapshot.active_items():
            if item.type != "avoid_trait":
                continue
            trait = str(item.target or "").strip()
            if not trait:
                continue
            marker = trait.casefold()
            if marker in seen:
                continue
            seen.add(marker)
            out.append(trait)
        return out

    def _resolve_one(self, *, trait: str) -> TraitCompanySet:
        cached = self._read_cache(trait=trait)
        if cached is not None and not cached.is_expired:
            return cached

        refreshed = self._expand_with_llm(trait=trait)
        if refreshed is not None:
            return self._write_cache(refreshed)

        if cached is not None:
            logger.warning(
                "trait_expander refresh failed; using stale cache trait=%s expires_at=%s",
                cached.trait,
                cached.expires_at,
            )
            return cached

        raise RuntimeError(
            f"trait_expander unavailable for avoid_trait={trait!r}; no cache to reuse"
        )

    def _read_cache(self, *, trait: str) -> TraitCompanySet | None:
        if self._preferences is None:
            return None
        return self._preferences.get_trait_company_set(
            trait_type="avoid_trait",
            trait=trait,
        )

    def _write_cache(self, expanded: _ExpandedTrait) -> TraitCompanySet:
        now = datetime.now(timezone.utc)
        updated_at = now.isoformat()
        expires_at = (now + self._ttl).isoformat()
        model = self._llm.primary_model(self._ROUTE)
        if self._preferences is None:
            return TraitCompanySet(
                trait_type="avoid_trait",
                trait=expanded.trait,
                companies=list(expanded.companies),
                model=model,
                updated_at=updated_at,
                expires_at=expires_at,
            )
        return self._preferences.set_trait_company_set(
            trait_type="avoid_trait",
            trait=expanded.trait,
            companies=list(expanded.companies),
            model=model,
            updated_at=updated_at,
            expires_at=expires_at,
        )

    def _expand_with_llm(self, *, trait: str) -> _ExpandedTrait | None:
        payload = self._llm.invoke_json(
            [
                _system(_SYSTEM_PROMPT),
                _user(
                    "Expand this user trait label into concrete company names.\n"
                    f"- trait_type: avoid_trait\n"
                    f"- trait: {trait}\n"
                    "\n"
                    "Return JSON only."
                ),
            ],
            route=self._ROUTE,
            default=None,
        )
        if not isinstance(payload, dict):
            return None
        mode = str(payload.get("mode") or "").strip().lower()
        if mode not in ("company_set", "non_company_trait"):
            return None
        echoed_trait = str(payload.get("trait") or "").strip() or trait
        companies = _coerce_company_list(payload.get("companies"))
        if mode == "company_set" and not companies:
            return None
        if mode == "non_company_trait":
            companies = []
        return _ExpandedTrait(
            trait=echoed_trait,
            companies=companies,
        )


def _coerce_company_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        out.append(text)
        if len(out) >= TraitCompanyExpander._MAX_COMPANIES:
            break
    return out


def _system(content: str) -> Any:
    from langchain_core.messages import SystemMessage
    return SystemMessage(content=content)


def _user(content: str) -> Any:
    from langchain_core.messages import HumanMessage
    return HumanMessage(content=content)


_SYSTEM_PROMPT = (
    "You expand user company-trait labels into concrete company sets for a "
    "deterministic policy gate.\n\n"
    "Goal: convert an avoid_trait label (e.g., 大厂 / 外包 / 国企) into high-confidence "
    "company names in the Chinese job market.\n\n"
    "Rules:\n"
    "1) Return JSON only.\n"
    "2) If the trait is mainly a company-category label, set mode='company_set' and "
    "return canonical company names.\n"
    "3) If the trait is not primarily company-category (e.g., role content constraint), "
    "set mode='non_company_trait' and return companies=[].\n"
    "4) Prefer precision over recall: include only names you are confident about.\n"
    "5) No prose outside JSON.\n\n"
    "Schema:\n"
    "{\"mode\":\"company_set|non_company_trait\","
    "\"trait\":\"<echo trait>\","
    "\"companies\":[\"<company>\",\"...\"],"
    "\"reason\":\"<short note>\"}"
)


__all__ = ["TraitCompanyExpander"]

