"""Domain Profile 协议 — 每个业务 domain 自己实现一份。

核心契约: **memory 是运行时单一事实源, yaml 是 memory 的持久化投影**。

  - 写路径:
      Brain mutation  → 写 memory → ``sync_to_yaml()`` 立即刷 yaml
      用户 vim 改 yaml → 必须 ``pulse profile load`` 才进 memory
  - 读路径:
      所有业务 (Brain / HTTP / snapshot) 只读 memory, 不读 yaml

同步保证:
  - ``sync_to_yaml`` 必须原子 (tmp + rename), 失败不能半写
  - ``load`` 必须全量替换 memory (先清空 domain 前缀, 再写入), 否则
    yaml 删掉一项、memory 保留 → 违反"yaml 与 memory 相等"契约
  - 所有方法都要 swallow IO/schema 异常并记日志, 不能把 profile 的
    单点问题冒到 Brain / 业务层
"""

from __future__ import annotations

import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DomainProfileError(Exception):
    """Profile 层的业务/IO 异常。业务侧 catch 它即可,不必关心底层。"""


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """原子写文本: 先写到同目录临时文件, fsync 后 rename 覆盖。

    Windows 上 ``os.replace`` 是原子的 (同卷), Linux 同理。任何中途失败
    (磁盘满 / 权限 / kill) 都不会让 ``path`` 处于半写状态。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class DomainProfileManager(ABC):
    """Domain-level profile 生命周期协议。

    每个有 profile 的业务 module 实现一个子类 (如 ``JobProfileManager``),
    在 ``BaseModule.get_profile_manager()`` 里返回实例。``ProfileCoordinator``
    会在启动 / tool_use 后按 ``domain`` 找到对应 manager 触发动作。
    """

    #: 短 domain 标识, 与 intent tool 名前缀一致 ("job" / "mail" / ...)。
    domain: str = ""

    #: yaml 镜像文件路径 (绝对路径推荐, 便于跨 CWD 调用)。
    yaml_path: Path

    # ── 核心生命周期 ──────────────────────────────────────

    @abstractmethod
    def load(self) -> None:
        """读 yaml → 全量替换 domain memory。

        实现契约:
          - yaml 不存在 → no-op (首次启动正常情况)
          - yaml schema 不合法 → 抛 ``DomainProfileError``, **不能**静默
            部分写入 (否则 memory 陷入半确定状态)
          - 写入前必须清空 domain 前缀下所有 facts, 再按 yaml 重建
        """

    @abstractmethod
    def sync_to_yaml(self) -> None:
        """读 memory 当前快照 → 原子覆盖 yaml。

        实现契约:
          - 幂等: 重复调用结果一致
          - 原子: 用 ``atomic_write_text`` 避免半写
          - 失败只记 warning, 不 raise (Hook 调用方不关心 IO 异常)
        """

    @abstractmethod
    def dump_current(self) -> dict[str, Any]:
        """返回 memory 当前状态的 schema dict, 不落盘。

        用于 CLI ``pulse profile dump`` / HTTP debug 端点。
        """

    @abstractmethod
    def reset(self) -> None:
        """清空 domain memory + 重写 yaml 为空 schema。

        用于 CLI ``pulse profile reset`` 与测试。谨慎使用。
        """

    # ── 非抽象便捷方法 ────────────────────────────────────

    def __repr__(self) -> str:
        return f"<{type(self).__name__} domain={self.domain!r} yaml={self.yaml_path}>"
