"""Scheduler 活跃 / 高峰窗口判定 —— **强制** Asia/Shanghai.

上游 ``SchedulerEngine`` 统一传 ``datetime.now(timezone.utc)`` 做 wall-clock
加减, 但 "工作日 9-18 点" 这种工作时间窗必须按**北京时间**解读:

* 代码跑在 VPN / 海外 VPS / WSL 时, 系统本地时区常常是 UTC 或其它, 用
  ``now.hour`` 直接读会把北京 09:00 当成 UTC 01:00, 整天判 inactive.
* ``datetime.now(timezone.utc)`` 本身不受 VPN 影响 (wall-clock 时刻确定),
  所以问题不在"取时间", 而在"按哪个 tz 解读小时 / 周末".

方案: 本模块把传入的 aware datetime 显式换算到 ``Asia/Shanghai``, 再读
``.hour`` / ``.weekday()``. naive datetime 为防御性兜底一律拒绝 —— 否则
无法保证解读语义, 出问题无法审计.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from pulse.core.clock import RUNTIME_TZ as _BEIJING_TZ


def _as_beijing(now: datetime) -> datetime:
    """Convert *aware* datetime to Asia/Shanghai; naive raises.

    拒收 naive 是因为 naive datetime 在不同进程里是完全不同的时刻 (取决于
    哪个 tz 默认值), 静默 "假装它是 UTC" 会把 bug 带到生产. 上层调用点都
    应拿 ``datetime.now(timezone.utc)``, 命中本函数时已是 aware.
    """
    if now.tzinfo is None:
        raise ValueError(
            "scheduler.windows requires timezone-aware datetime; "
            "caller must pass e.g. datetime.now(timezone.utc)"
        )
    return now.astimezone(_BEIJING_TZ)


def is_weekend(now: datetime) -> bool:
    """周末判定 —— 按北京时间的 weekday. 例: UTC 周日 23:00 = 北京周一 07:00,
    本函数返回 False (已是周一工作日早晨)."""
    return _as_beijing(now).weekday() >= 5


def is_active_hour(
    now: datetime,
    *,
    weekday_start: int,
    weekday_end: int,
    weekend_start: int,
    weekend_end: int,
) -> bool:
    local = _as_beijing(now)
    hour = local.hour
    if local.weekday() >= 5:
        return weekend_start <= hour < weekend_end
    return weekday_start <= hour < weekday_end


def is_in_windows(
    now: datetime,
    *,
    weekday_windows: Iterable[tuple[int, int]],
    weekend_windows: Iterable[tuple[int, int]] = (),
) -> bool:
    """按北京时间判断当前小时是否落入给定窗口集合.

    用于任务级 patrol 窗口:有的任务适合工作日全天,有的只适合早/下午高峰。
    空窗口集合表示该日类型整天静默。
    """
    local = _as_beijing(now)
    windows = weekend_windows if local.weekday() >= 5 else weekday_windows
    hour = local.hour
    return any(start <= hour < end for start, end in windows)


def is_peak_hour(
    now: datetime,
    *,
    peak_windows: Iterable[tuple[int, int]],
    weekend_peak: bool = False,
) -> bool:
    local = _as_beijing(now)
    if local.weekday() >= 5 and not weekend_peak:
        return False
    hour = local.hour
    return any(start <= hour < end for start, end in peak_windows)
