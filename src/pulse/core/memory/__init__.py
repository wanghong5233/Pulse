"""Memory subsystem for Pulse.

顶层 facade, 把常用 memory 组件集中 export, 外部调用方无需深入子模块。
分类:
  - 六层 memory 实现: Core / Recall / Archival / Workspace / Operational
  - 统一信封: MemoryEnvelope + Layer/Scope/Kind 枚举
  - 业务侧 view: WorkspaceMemory.Fact (facade 返回的结构化行)
  - 工具注册入口: register_memory_tools
"""

from .archival_memory import ArchivalMemory
from .core_memory import CoreMemory
from .envelope import (
    MemoryEnvelope,
    MemoryKind,
    MemoryLayer,
    MemoryScope,
    conversation_envelope,
    envelope_from_task_context,
    fact_envelope,
    tool_call_envelope,
)
from .memory_tools import register_memory_tools
from .operational_memory import OperationalMemory
from .recall_memory import RecallMemory
from .workspace_memory import Fact, WorkspaceMemory

__all__ = [
    # 六层 memory
    "CoreMemory",
    "RecallMemory",
    "ArchivalMemory",
    "WorkspaceMemory",
    "OperationalMemory",
    # 信封与枚举
    "MemoryEnvelope",
    "MemoryKind",
    "MemoryLayer",
    "MemoryScope",
    # envelope 工厂
    "envelope_from_task_context",
    "conversation_envelope",
    "tool_call_envelope",
    "fact_envelope",
    # facade 返回类型
    "Fact",
    # 工具注册
    "register_memory_tools",
]
