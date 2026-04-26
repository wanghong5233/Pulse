"""Intel domain business configuration.

Pulse keeps kernel knobs in :mod:`pulse.core.config` and lets each
business module own its own ``BaseSettings`` subclass — same pattern
as :mod:`pulse.modules.job.config`. The intel module currently only
needs to expose how to talk to RSSHub:

  * which instances to try (self-hosted first, public fallbacks);
  * how long the per-instance health-check cache lives;
  * the request timeout when probing them.

Topics declare RSSHub feeds with an ``rsshub://<route>`` URL — see
:mod:`pulse.modules.intel.sources.rss`. The fetcher resolves that to
the first healthy base URL from this settings object, so swapping
self-hosted vs public is purely an env change, no YAML edits.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# RSSHub instance order matters: first responder wins. Defaults put the
# self-hosted profile container first (works for ``docker compose up``)
# and falls back to two public mirrors so a fresh checkout still gets
# *some* digest before the user wires their own infra.
_DEFAULT_RSSHUB_INSTANCES: str = (
    "http://rsshub:1200,https://rsshub.app,https://rsshub.rssforever.com"
)


class IntelSettings(BaseSettings):
    """Intel-domain knobs loaded from ``PULSE_INTEL_*`` env vars."""

    github_token: str = Field(
        default="",
        validation_alias=AliasChoices("PULSE_INTEL_GITHUB_TOKEN", "GITHUB_TOKEN"),
        description=(
            "Optional GitHub API token for github_trending. Supports the "
            "standard GITHUB_TOKEN name and the domain-scoped "
            "PULSE_INTEL_GITHUB_TOKEN name."
        ),
    )
    rsshub_instances: str = Field(
        default=_DEFAULT_RSSHUB_INSTANCES,
        description=(
            "Comma-separated RSSHub base URLs tried in order. The first "
            "instance whose recent health probe succeeded wins; on "
            "transport error the fetcher steps to the next."
        ),
    )
    rsshub_probe_timeout_sec: float = Field(
        default=4.0,
        ge=1.0,
        le=30.0,
        description="HTTP timeout for the RSSHub health probe.",
    )
    rsshub_health_ttl_sec: int = Field(
        default=300,
        ge=10,
        le=3600,
        description=(
            "How long a successful health-probe sticks. After this we "
            "re-probe before serving the next request."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="PULSE_INTEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("rsshub_instances")
    @classmethod
    def _strip_instances(cls, value: str) -> str:
        return str(value or "").strip()

    @property
    def rsshub_instance_list(self) -> list[str]:
        """Return the configured RSSHub base URLs in order, no trailing slash."""
        out: list[str] = []
        for raw in str(self.rsshub_instances or "").split(","):
            base = raw.strip()
            if not base:
                continue
            out.append(base.rstrip("/"))
        return out


@lru_cache(maxsize=1)
def get_intel_settings() -> IntelSettings:
    return IntelSettings()
