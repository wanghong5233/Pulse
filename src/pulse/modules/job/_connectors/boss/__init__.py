from pulse.modules.job.shared.enums import PlatformProvider

from ..registry import register_platform_factory
from .connector import BossPlatformConnector, build_boss_platform_connector

register_platform_factory(PlatformProvider.BOSS.value, build_boss_platform_connector)

__all__ = ["BossPlatformConnector", "build_boss_platform_connector"]
