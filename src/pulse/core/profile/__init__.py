"""Pulse Profile Runtime — 内核侧协议与协调器。

职责:
  - 定义 ``DomainProfileManager`` 协议,由各业务 domain (job / mail / ...) 自己实现
  - ``ProfileCoordinator`` 把所有 domain 的 manager 聚合起来,提供统一入口
    (启动 load / after_tool_use sync / CLI dump/export/reset)

本目录**只有协议 + 协调**, 不承载任何 domain 具体 schema / 文件路径 /
字段映射。新 domain 加入时, 只需:
  1. 在自己 module 里实现 ``DomainProfileManager``
  2. 在 ``BaseModule.get_profile_manager()`` 里返回它

见 ``docs/Pulse-DomainMemory与Tool模式.md`` 的 "Domain Profile" 章节。
"""

from .base import DomainProfileError, DomainProfileManager, atomic_write_text
from .coordinator import ProfileCoordinator

__all__ = [
    "DomainProfileManager",
    "DomainProfileError",
    "ProfileCoordinator",
    "atomic_write_text",
]
