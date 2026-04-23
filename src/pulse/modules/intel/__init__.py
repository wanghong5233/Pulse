"""Intel domain skill package.

Subcapabilities:
  - ``interview`` : 面经情报收集与日报推送
  - ``techradar`` : 技术雷达信号收集与日报推送
  - ``query``     : 本地情报知识库语义检索

See ``skill.py`` for the domain-level schema consumed by the router.
"""

from .skill import SKILL_SCHEMA

__all__ = ["SKILL_SCHEMA"]
