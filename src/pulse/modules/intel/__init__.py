"""Intel domain skill package.

Single deterministic-workflow module that subscribes multi-channel signals,
deduplicates / scores / summarises them per topic and publishes daily digests.
Topics are declared in ``topics/<id>.yaml`` and loaded at startup; adding a
topic is a YAML change, never a code change.

See ``skill.py`` for the domain-level schema consumed by the router and
``docs/architecture.md`` for the workflow contract.
"""

from .module import IntelModule, get_module
from .skill import SKILL_SCHEMA

__all__ = ["IntelModule", "SKILL_SCHEMA", "get_module"]
