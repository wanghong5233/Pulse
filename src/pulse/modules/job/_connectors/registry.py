"""Connector registry for the job domain.

Keeps platform selection explicit and test-friendly. A new platform (e.g.
Liepin, Zhilian) joins the registry by calling
:func:`register_platform_factory` at import time from its own
``_connectors/<platform>/__init__.py``.

Business code depends only on :class:`~..._connectors.base.JobPlatformConnector`
and :func:`build_connector`; it never imports concrete classes.
"""

from __future__ import annotations

import threading
from typing import Callable

from pulse.modules.job.shared.enums import PlatformProvider

from .base import JobPlatformConnector

ConnectorFactory = Callable[[], JobPlatformConnector]


class _Registry:
    def __init__(self) -> None:
        self._factories: dict[str, ConnectorFactory] = {}
        self._lock = threading.Lock()

    def register(self, platform: str, factory: ConnectorFactory) -> None:
        key = (platform or "").strip().lower()
        if not key:
            raise ValueError("platform name must be non-empty")
        with self._lock:
            self._factories[key] = factory

    def build(self, platform: str) -> JobPlatformConnector:
        key = (platform or "").strip().lower()
        with self._lock:
            factory = self._factories.get(key)
        if factory is None:
            raise KeyError(f"job connector not registered for platform '{platform}'")
        return factory()

    def registered(self) -> list[str]:
        with self._lock:
            return sorted(self._factories)


_registry = _Registry()


def register_platform_factory(platform: str, factory: ConnectorFactory) -> None:
    """Register a platform factory. Safe to call multiple times (last wins)."""
    _registry.register(platform, factory)


def build_connector(platform: str | None = None) -> JobPlatformConnector:
    """Build a connector for ``platform``; falls back to the default platform."""
    return _registry.build(platform or default_platform())


def registered_platforms() -> list[str]:
    return _registry.registered()


def default_platform() -> str:
    """Currently hard-coded to BOSS.

    Once multiple platforms are implemented, this should read from
    ``JobSettings.default_platform`` (to be added) or a per-user preference.
    """
    return PlatformProvider.BOSS.value
