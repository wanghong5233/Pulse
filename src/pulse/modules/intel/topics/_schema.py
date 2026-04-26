"""TopicConfig — single source of truth for one intel topic.

Loaded from YAML at startup. Every section maps directly onto a stage
of the deterministic DigestWorkflow:

  sources    → fetch.py    : multi-channel collectors
  scoring    → score.py    : LLM rubric per topic
  diversity  → diversify.py: anti-cocoon quotas / serendipity / contrarian
  publish    → publish.py  : channel + cron schedule
  memory     → publish.py  : ArchivalMemory promotion threshold

Validation runs at load time; an invalid topic file fails the whole
module bootstrap rather than silently skipping the topic — fail loud
beats "patrol registered but never produces digest".
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

SourceType = Literal["rss", "github_trending", "web_search"]


class SourceConfig(BaseModel):
    """One subscribed source within a topic."""

    type: SourceType
    url: str | None = None
    weight: float = Field(default=1.0, ge=0.0, le=2.0)
    label: str = ""
    language: str = ""
    spoken_language: str = ""
    since: str = ""
    query: str = ""
    max_results: int = Field(default=20, ge=1, le=200)

    @field_validator("url")
    @classmethod
    def _check_url_for_url_sources(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("url cannot be empty when provided")
        return value


class ScoringConfig(BaseModel):
    threshold: float = Field(default=6.0, ge=0.0, le=10.0)
    rubric_prompt: str = Field(default="")
    rubric_dimensions: list[str] = Field(default_factory=lambda: ["depth", "novelty", "impact"])

    @field_validator("rubric_prompt")
    @classmethod
    def _strip_prompt(cls, value: str) -> str:
        return str(value or "").strip()


class DiversityConfig(BaseModel):
    """Anti-information-cocoon controls applied in diversify.py.

    PR1 wires the values into the topic; the actual logic lands in
    ``pipeline/diversify.py`` (PR2). Keeping the schema here from day
    one avoids a breaking change later.
    """

    max_per_source: int = Field(default=2, ge=1, le=20)
    serendipity_slots: int = Field(default=1, ge=0, le=10)
    contrarian_bonus: float = Field(default=0.0, ge=0.0, le=2.0)


class PublishConfig(BaseModel):
    schedule_cron: str = Field(default="0 9 * * *", min_length=1)
    channel: str = Field(default="feishu")
    format: str = Field(default="digest_zh")
    peak_interval_seconds: int = Field(default=3600, ge=60, le=24 * 3600)
    offpeak_interval_seconds: int = Field(default=6 * 3600, ge=60, le=24 * 3600)


class MemoryConfig(BaseModel):
    """How a topic interacts with Pulse MemoryRuntime.

    PR1 records the threshold but does not call MemoryRuntime; PR3 wires
    promotion to ArchivalMemory.
    """

    promote_threshold: float = Field(default=8.5, ge=0.0, le=10.0)


class TopicConfig(BaseModel):
    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    display_name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    sources: list[SourceConfig] = Field(default_factory=list)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    diversity: DiversityConfig = Field(default_factory=DiversityConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    @field_validator("sources")
    @classmethod
    def _at_least_one_source(cls, value: list[SourceConfig]) -> list[SourceConfig]:
        if not value:
            raise ValueError("topic must declare at least one source")
        return value

    @property
    def patrol_name(self) -> str:
        return f"intel.digest.{self.id}"


def load_topic_file(path: Path) -> TopicConfig:
    """Parse one YAML file into a validated ``TopicConfig``.

    Errors propagate; callers decide whether to fail the whole startup
    or to skip and continue.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"failed to read topic file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"topic file {path} must be a YAML mapping, got {type(data).__name__}")
    if "id" not in data:
        data["id"] = path.stem
    try:
        return TopicConfig.model_validate(data)
    except ValidationError as exc:
        raise RuntimeError(f"invalid topic config {path}: {exc}") from exc


def discover_topic_files(topics_dir: Path) -> list[Path]:
    """Return active topic YAML files under ``topics_dir``.

    Filenames starting with ``_`` are templates / placeholders and are
    skipped. Sub-directories are not recursed (templates live in
    ``_examples/``, themselves prefixed and therefore ignored at the top
    level).
    """
    if not topics_dir.is_dir():
        return []
    return sorted(
        path
        for path in topics_dir.glob("*.yaml")
        if path.is_file() and not path.name.startswith("_")
    )


def load_topic_configs(topics_dir: Path) -> list[TopicConfig]:
    """Load every active topic, fail loud on any invalid file."""
    configs: list[TopicConfig] = []
    for path in discover_topic_files(topics_dir):
        config = load_topic_file(path)
        logger.info(
            "Loaded intel topic: id=%s sources=%d schedule=%s",
            config.id,
            len(config.sources),
            config.publish.schedule_cron,
        )
        configs.append(config)
    return configs
