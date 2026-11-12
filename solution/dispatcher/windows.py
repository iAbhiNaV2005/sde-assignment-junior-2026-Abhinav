"""Calling window calculations and regulatory compliance."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import WindowConfig

IST = timezone(timedelta(hours=5, minutes=30))
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def _get_effective_window(
    camp_win: WindowConfig,
    reg_win: WindowConfig,
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Compute (effective_start, effective_end) as (hour, minute) tuples."""
    c_sh, c_sm = parse_hm(camp_win.start)
    c_eh, c_em = parse_hm(camp_win.end)
    r_sh, r_sm = parse_hm(reg_win.start)
    r_eh, r_em = parse_hm(reg_win.end)

    start_m = max(c_sh * 60 + c_sm, r_sh * 60 + r_sm)
    end_m = min(c_eh * 60 + c_em, r_eh * 60 + r_em)

    return (divmod(start_m, 60), divmod(end_m, 60))


def is_window_open(
    t: datetime,
    camp_win: WindowConfig,
    reg_win: WindowConfig,
    margin_seconds: float = 30.0,
) -> bool:
    """True if t (in IST) is inside the intersection of campaign & regulatory windows on an allowed day."""
    lt = t.astimezone(IST)
    day_name = DAYS[lt.weekday()]

    if camp_win.days is not None and day_name not in camp_win.days:
        return False

    (sh, sm), (eh, em) = _get_effective_window(camp_win, reg_win)
    start_dt = lt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_dt = lt.replace(hour=eh, minute=em, second=0, microsecond=0) - timedelta(seconds=margin_seconds)

    if start_dt >= end_dt:
        return False

    return start_dt <= lt < end_dt


def next_dialable_time(
    t: datetime,
    camp_win: WindowConfig,
    reg_win: WindowConfig,
    margin_seconds: float = 30.0,
) -> datetime:
    """Earliest instant >= t (in IST) where the window is open with safety margin."""
    lt = t.astimezone(IST)

    if is_window_open(lt, camp_win, reg_win, margin_seconds):
        return lt

    (sh, sm), (eh, em) = _get_effective_window(camp_win, reg_win)

    # Search up to 8 days ahead to handle weekend skips (e.g. Zen Fri night -> Mon morning)
    for d in range(9):
        target_day = (lt + timedelta(days=d)).replace(second=0, microsecond=0)
        day_name = DAYS[target_day.weekday()]

        if camp_win.days is not None and day_name not in camp_win.days:
            continue

        start_dt = target_day.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end_dt = target_day.replace(hour=eh, minute=em, second=0, microsecond=0) - timedelta(seconds=margin_seconds)

        if start_dt >= end_dt:
            continue

        if d == 0:
            if lt < start_dt:
                return start_dt
            if lt < end_dt:
                return lt
            # Closed for today; advance to next days
            continue

        return start_dt

    return lt

