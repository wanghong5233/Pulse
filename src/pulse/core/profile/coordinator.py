"""ProfileCoordinator — 跨 domain profile 的统一入口。

- 启动时 ``load_all()`` 让每个 domain 用 yaml 种子 memory
- ``after_tool_use`` hook 里按 tool 名拆出 domain, 调 ``sync_one(domain)``
- CLI ``pulse profile {load, dump, export, reset}`` 走这里

**不持有业务状态**: 只是 manager 的注册表 + 路由。每个 manager 的生命周期
由所属 module 负责。
"""

from __future__ import annotations

import logging
from typing import Any

from .base import DomainProfileError, DomainProfileManager

logger = logging.getLogger(__name__)


class ProfileCoordinator:
    """Domain profile manager 注册表 / 分派器。"""

    def __init__(self) -> None:
        self._managers: dict[str, DomainProfileManager] = {}

    # ── registry ─────────────────────────────────────────

    def register(self, manager: DomainProfileManager) -> None:
        domain = str(manager.domain or "").strip()
        if not domain:
            raise ValueError("DomainProfileManager.domain must be a non-empty string")
        if domain in self._managers:
            raise ValueError(f"duplicated profile domain: {domain}")
        self._managers[domain] = manager
        logger.info("profile manager registered: domain=%s yaml=%s", domain, manager.yaml_path)

    @property
    def domains(self) -> tuple[str, ...]:
        return tuple(self._managers.keys())

    def get(self, domain: str) -> DomainProfileManager | None:
        return self._managers.get(str(domain or "").strip())

    # ── lifecycle ────────────────────────────────────────

    def load_all(self) -> dict[str, str]:
        """启动时调用: 让每个 domain 把 yaml 种到 memory。

        返回 ``{domain: status}``, status 取 "loaded"/"skipped"/"error"。
        单个 domain 失败不影响其他 domain。
        """
        report: dict[str, str] = {}
        for domain, manager in self._managers.items():
            try:
                manager.load()
                report[domain] = "loaded"
            except DomainProfileError as exc:
                logger.error("profile load failed for domain=%s: %s", domain, exc)
                report[domain] = f"error: {exc}"
            except Exception as exc:
                logger.exception("profile load crashed for domain=%s", domain)
                report[domain] = f"error: {exc}"
        return report

    def sync_one(self, domain: str) -> bool:
        """after_tool_use hook 入口: 把 domain memory 当前状态写回 yaml。

        返回 True 表示本次有实际写入, False 表示无对应 manager。
        **永远不 raise**, Hook 回调端不关心 IO 异常。
        """
        manager = self._managers.get(str(domain or "").strip())
        if manager is None:
            return False
        try:
            manager.sync_to_yaml()
            return True
        except Exception:
            logger.warning("profile sync_to_yaml failed for domain=%s", domain, exc_info=True)
            return False

    def sync_all(self) -> dict[str, str]:
        report: dict[str, str] = {}
        for domain, manager in self._managers.items():
            try:
                manager.sync_to_yaml()
                report[domain] = "synced"
            except Exception as exc:
                logger.warning("profile sync_all: %s failed: %s", domain, exc)
                report[domain] = f"error: {exc}"
        return report

    def dump_all(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for domain, manager in self._managers.items():
            try:
                out[domain] = manager.dump_current()
            except Exception as exc:
                logger.warning("profile dump %s failed: %s", domain, exc)
                out[domain] = {"error": str(exc)}
        return out

    def reset_domain(self, domain: str) -> bool:
        manager = self._managers.get(str(domain or "").strip())
        if manager is None:
            return False
        manager.reset()
        return True
