from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pulse.core.scheduler.windows import (
    is_active_hour,
    is_in_windows,
    is_peak_hour,
    is_weekend,
)

# 所有 windows.py 入口都要求 aware datetime 并强制换算到 Asia/Shanghai.
# 用 UTC 构造输入, 再手算对应的北京时间做断言, 这样测试本身就能证明
# "换算没丢" —— 重构 _BEIJING_TZ 时不会无感破坏语义.


def test_is_weekend_uses_beijing_weekday() -> None:
    # UTC 周日 23:00 = 北京周一 07:00 → 不是周末
    utc_sunday_late = datetime(2026, 3, 29, 23, 0, tzinfo=timezone.utc)
    assert is_weekend(utc_sunday_late) is False

    # UTC 周六 01:00 = 北京周六 09:00 → 是周末
    utc_saturday_am = datetime(2026, 3, 28, 1, 0, tzinfo=timezone.utc)
    assert is_weekend(utc_saturday_am) is True


def test_is_active_hour_weekday_uses_beijing_hour() -> None:
    # UTC 01:00 周五 = 北京 09:00 周五 → 工作日窗口开端, active=True
    utc_weekday_morning = datetime(2026, 3, 27, 1, 0, tzinfo=timezone.utc)
    assert (
        is_active_hour(
            utc_weekday_morning,
            weekday_start=9,
            weekday_end=18,
            weekend_start=0,
            weekend_end=0,
        )
        is True
    )

    # UTC 13:00 周五 = 北京 21:00 周五 → 超出 18 点, inactive
    utc_weekday_night = datetime(2026, 3, 27, 13, 0, tzinfo=timezone.utc)
    assert (
        is_active_hour(
            utc_weekday_night,
            weekday_start=9,
            weekday_end=18,
            weekend_start=0,
            weekend_end=0,
        )
        is False
    )


def test_is_active_hour_weekend_zero_window_means_quiet() -> None:
    # 周末 start=end=0 是 "周末静默" 的硬约定, 任何小时都返回 False.
    utc_saturday_noon = datetime(2026, 3, 28, 4, 0, tzinfo=timezone.utc)  # 北京 12:00
    assert (
        is_active_hour(
            utc_saturday_noon,
            weekday_start=9,
            weekday_end=18,
            weekend_start=0,
            weekend_end=0,
        )
        is False
    )


def test_is_peak_hour_uses_beijing_hour() -> None:
    # UTC 06:00 周五 = 北京 14:00 周五 → 落在 (14,18) 高峰窗
    utc_peak = datetime(2026, 3, 27, 6, 0, tzinfo=timezone.utc)
    assert is_peak_hour(utc_peak, peak_windows=[(9, 12), (14, 18)]) is True

    # UTC 15:00 周五 = 北京 23:00 周五 → 不在高峰
    utc_offpeak = datetime(2026, 3, 27, 15, 0, tzinfo=timezone.utc)
    assert is_peak_hour(utc_offpeak, peak_windows=[(9, 12), (14, 18)]) is False


def test_is_peak_hour_weekend_skipped_unless_opt_in() -> None:
    utc_saturday = datetime(2026, 3, 28, 4, 0, tzinfo=timezone.utc)  # 北京 12:00 周六
    assert is_peak_hour(utc_saturday, peak_windows=[(9, 18)]) is False
    assert (
        is_peak_hour(utc_saturday, peak_windows=[(9, 18)], weekend_peak=True) is True
    )


def test_is_in_windows_supports_task_specific_weekday_windows() -> None:
    # UTC 02:00 周五 = 北京 10:00 周五 → 落入自动投递上午高峰窗
    utc_weekday_morning = datetime(2026, 3, 27, 2, 0, tzinfo=timezone.utc)
    assert (
        is_in_windows(
            utc_weekday_morning,
            weekday_windows=((9, 12), (14, 18)),
        )
        is True
    )

    # UTC 04:30 周五 = 北京 12:30 周五 → 午间静默
    utc_weekday_noon = datetime(2026, 3, 27, 4, 30, tzinfo=timezone.utc)
    assert (
        is_in_windows(
            utc_weekday_noon,
            weekday_windows=((9, 12), (14, 18)),
        )
        is False
    )

    # 周末空窗口 = 静默
    utc_saturday = datetime(2026, 3, 28, 2, 0, tzinfo=timezone.utc)
    assert (
        is_in_windows(
            utc_saturday,
            weekday_windows=((9, 18),),
            weekend_windows=(),
        )
        is False
    )


def test_rejects_naive_datetime() -> None:
    # naive datetime 在不同进程里时刻不唯一, 强制要求 aware 输入避免
    # "静默当 UTC 处理" 的幽灵 bug (ADR-006-v2 + 运行手册约定).
    naive = datetime(2026, 3, 27, 10, 0)
    with pytest.raises(ValueError):
        is_weekend(naive)
    with pytest.raises(ValueError):
        is_active_hour(
            naive,
            weekday_start=9,
            weekday_end=18,
            weekend_start=0,
            weekend_end=0,
        )
    with pytest.raises(ValueError):
        is_peak_hour(naive, peak_windows=[(9, 12)])
    with pytest.raises(ValueError):
        is_in_windows(naive, weekday_windows=((9, 18),))
