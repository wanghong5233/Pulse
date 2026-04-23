"""Email domain skill package.

Subcapabilities:
  - ``tracker`` : IMAP + LLM 分类 + DB 持久化的邮件追踪能力

See ``skill.py`` for the domain-level schema consumed by the router.
"""

from .skill import SKILL_SCHEMA

__all__ = ["SKILL_SCHEMA"]
