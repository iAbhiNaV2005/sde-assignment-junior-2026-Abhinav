"""Unit tests for window calculations, safety margins, and regulatory clipping."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dispatcher.config import WindowConfig
from dispatcher.windows import is_window_open, next_dialable_time

IST = timezone(timedelta(hours=5, minutes=30))


def test_window_boundaries():
    camp_win = WindowConfig("10:00", "19:00", ("mon", "tue", "wed", "thu", "fri", "sat"))
    reg_win = WindowConfig("09:00", "21:00", None)

    # Thursday 12 Nov 2026
    # 09:59:59 - Before start
    t_before = datetime(2026, 11, 12, 9, 59, 59, tzinfo=IST)
    assert not is_window_open(t_before, camp_win, reg_win, margin_seconds=30)

    # 10:00:00 - Exactly at start (inclusive)
    t_start = datetime(2026, 11, 12, 10, 0, 0, tzinfo=IST)
    assert is_window_open(t_start, camp_win, reg_win, margin_seconds=30)

    # 18:59:29 - Just inside 30s safety margin (end is 19:00:00)
    t_inside = datetime(2026, 11, 12, 18, 59, 29, tzinfo=IST)
    assert is_window_open(t_inside, camp_win, reg_win, margin_seconds=30)

    # 18:59:30 - Hit the safety margin (19:00 - 30s)
    t_margin = datetime(2026, 11, 12, 18, 59, 30, tzinfo=IST)
    assert not is_window_open(t_margin, camp_win, reg_win, margin_seconds=30)

    # 19:00:00 - Exactly at end
    t_end = datetime(2026, 11, 12, 19, 0, 0, tzinfo=IST)
    assert not is_window_open(t_end, camp_win, reg_win, margin_seconds=30)


def test_allowed_days():
    # Zen: Mon-Fri
    zen_win = WindowConfig("09:30", "18:00", ("mon", "tue", "wed", "thu", "fri"))
    reg_win = WindowConfig("09:00", "21:00", None)

    # Friday 13 Nov 2026 12:00 - allowed
    fri = datetime(2026, 11, 13, 12, 0, 0, tzinfo=IST)
    assert is_window_open(fri, zen_win, reg_win)

    # Saturday 14 Nov 2026 12:00 - disallowed
    sat = datetime(2026, 11, 14, 12, 0, 0, tzinfo=IST)
    assert not is_window_open(sat, zen_win, reg_win)

    # Sunday 15 Nov 2026 12:00 - disallowed
    sun = datetime(2026, 11, 15, 12, 0, 0, tzinfo=IST)
    assert not is_window_open(sun, zen_win, reg_win)


def test_next_dialable_weekend_skip():
    zen_win = WindowConfig("09:30", "18:00", ("mon", "tue", "wed", "thu", "fri"))
    reg_win = WindowConfig("09:00", "21:00", None)

    # Friday 13 Nov 2026 18:05 IST (window closed for Zen)
    fri_late = datetime(2026, 11, 13, 18, 5, 0, tzinfo=IST)
    next_time = next_dialable_time(fri_late, zen_win, reg_win, margin_seconds=30)

    # Should skip Sat & Sun, landing on Monday 16 Nov 2026 at 09:30 IST
    expected = datetime(2026, 11, 16, 9, 30, 0, tzinfo=IST)
    assert next_time == expected


def test_next_dialable_same_day():
    zen_win = WindowConfig("09:30", "18:00", ("mon", "tue", "wed", "thu", "fri"))
    reg_win = WindowConfig("09:00", "21:00", None)

    # Friday 13 Nov 2026 08:00 IST (before start)
    early = datetime(2026, 11, 13, 8, 0, 0, tzinfo=IST)
    next_time = next_dialable_time(early, zen_win, reg_win, margin_seconds=30)
    expected = datetime(2026, 11, 13, 9, 30, 0, tzinfo=IST)
    assert next_time == expected

    # Friday 13 Nov 2026 11:00 IST (already open)
    mid = datetime(2026, 11, 13, 11, 0, 0, tzinfo=IST)
    assert next_dialable_time(mid, zen_win, reg_win, margin_seconds=30) == mid


def test_regulatory_window_clipping():
    # Campaign wants 07:00 to 22:00, but regulatory is 09:00 to 21:00
    broad_camp = WindowConfig("07:00", "22:00", ("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
    reg_win = WindowConfig("09:00", "21:00", None)

    t_0830 = datetime(2026, 11, 12, 8, 30, 0, tzinfo=IST)
    assert not is_window_open(t_0830, broad_camp, reg_win)

    t_0900 = datetime(2026, 11, 12, 9, 0, 0, tzinfo=IST)
    assert is_window_open(t_0900, broad_camp, reg_win)

    t_2059 = datetime(2026, 11, 12, 20, 59, 20, tzinfo=IST)
    assert is_window_open(t_2059, broad_camp, reg_win, margin_seconds=30)

    t_2100 = datetime(2026, 11, 12, 21, 0, 0, tzinfo=IST)
    assert not is_window_open(t_2100, broad_camp, reg_win)

