"""Job domain skill package.

Subcapabilities:
  - ``greet``   : scan + 打招呼
  - ``chat``    : HR conversation processing

Platform drivers live in ``_connectors/`` and must implement
``JobPlatformConnector`` (see ``_connectors/base.py``).

See ``skill.py`` for the domain-level schema consumed by the router.
"""

from .skill import SKILL_SCHEMA

__all__ = ["SKILL_SCHEMA"]
