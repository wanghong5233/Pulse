"""Pulse kernel — wall-clock primitives.

不变量: Pulse 按 Asia/Shanghai 解读"今天 / 现在 / 周几", 进程 / 容器 /
VPN 出口都不影响这条语义. 时区常量在本模块独家拥有, 其它代码 import
不复制.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

try:
    from zoneinfo import ZoneInfo  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — runtime requires py3.11+
    from backports.zoneinfo import ZoneInfo  # type: ignore[import-not-found,no-redef]

__all__ = (
    "RUNTIME_TZ",
    "local_now",
    "local_today",
    "local_today_start_utc",
    "to_local",
)


RUNTIME_TZ = ZoneInfo("Asia/Shanghai")


def local_now() -> datetime:
    return datetime.now(RUNTIME_TZ)


def local_today() -> date:
    return local_now().date()


def local_today_start_utc() -> datetime:
    """北京今天 00:00 对应的 UTC 瞬时.

    存储层 ``created_at`` 一般是 UTC; 直接 ``DATE_TRUNC('day', NOW() AT
    TIME ZONE 'UTC')`` 会按 UTC 切天 — UTC+8 用户在凌晨 0-8 点看到的是
    昨日配额未释放. 由 Python 端按 wall clock 算好瞬时再传给 SQL, 跟
    数据库 tz 状态完全解耦.
    """
    start_local = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def to_local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError(
            "to_local requires aware datetime; naive datetime has no "
            "defined wall-clock semantics across processes"
        )
    return moment.astimezone(RUNTIME_TZ)
