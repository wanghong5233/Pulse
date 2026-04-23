"""System domain skill package.

Subcapabilities:
  - ``hello``    : 健康探针 / 默认回复
  - ``feedback`` : 用户反馈回路

See ``skill.py`` for the domain-level schema consumed by the router.
"""

from .skill import SKILL_SCHEMA

__all__ = ["SKILL_SCHEMA"]
