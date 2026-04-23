"""Job-domain platform connectors.

Each subpackage (``boss/``, future ``liepin/``, ``zhilian/`` ...) implements
``JobPlatformConnector`` and registers itself with the connector registry
on import.

Business code should interact with connectors **only** through the registry:

    from pulse.modules.job._connectors import build_connector
    connector = build_connector("boss")

Direct imports of concrete classes (``BossPlatformConnector``) are allowed
for type hints and tests, but must not be used to construct instances from
module/service code.
"""

from .base import JobPlatformConnector
from .boss import BossPlatformConnector, build_boss_platform_connector  # registers boss
from .registry import (
    build_connector,
    default_platform,
    register_platform_factory,
    registered_platforms,
)

__all__ = [
    "JobPlatformConnector",
    "BossPlatformConnector",
    "build_boss_platform_connector",
    "build_connector",
    "default_platform",
    "register_platform_factory",
    "registered_platforms",
]
