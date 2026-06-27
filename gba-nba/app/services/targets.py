"""Sales-target engine — «скільки МІНІМУМ продати».

Sits ABOVE the task engine: computes each manager's monthly minimum from their own history
(run-rate = average of the recent COMPLETED months), spreads it across the month's working days
(Mon–Sat), and reports pace vs actual. Tracks TWO metrics: shipped (Order revenue, EUR) and paid
(IncomePaymentOrder cash, EUR). The pace gap is what later boosts task urgency.

All money in EUR. Pure date math is module-level (unit-testable without a DB).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.data import signals_repository as sig

_SUNDAY = 6


def working_days_in_month(year: int, month: int) -> int:
    """Mon–Sat days in the month (Sunday excluded)."""
    days = calendar.monthrange(year, month)[1]
    return sum(1 for d in range(1, days + 1) if date(year, month, d).weekday() != _SUNDAY)


def working_days_elapsed(as_of: date) -> int:
    """Mon–Sat days from the 1st of as_of's month through as_of inclusive."""
    return sum(1 for d in range(1, as_of.day + 1) if date(as_of.year, as_of.month, d).weekday() != _SUNDAY)


def run_rate(series: dict[str, float], current_month: str, n: int = 3) -> float:
    """Average of the n most-recent COMPLETED months (current partial month excluded).

    This is the conservative 'minimum' floor: you've recently been selling this much, at minimum
    keep it up. Returns 0.0 if there's no completed history yet.
    """
    completed = sorted(k for k in series if k < current_month)
    recent = completed[-n:]
    return sum(series[k] for k in recent) / len(recent) if recent else 0.0


def _pace_status(actual: float, expected: float) -> str:
    if expected <= 0:
        return "on"
    ratio = actual / expected
    if ratio >= 1.05:
        return "ahead"
    if ratio < 0.95:
        return "behind"
    return "on"


def _metric(series: dict[str, float], current_month: str, mtd: float,
            wd: int, wd_elapsed: int, n: int) -> dict:
    target = run_rate(series, current_month, n)
    daily_pace = target / wd if wd else 0.0
    expected_to_date = daily_pace * wd_elapsed
    remaining_target = max(target - mtd, 0.0)
    remaining_wd = max(wd - wd_elapsed, 0)
    today_needed = remaining_target / remaining_wd if remaining_wd else remaining_target
    return {
        "target": round(target, 2),
        "mtd": round(mtd, 2),
        "daily_pace": round(daily_pace, 2),
        "expected_to_date": round(expected_to_date, 2),
        "gap": round(expected_to_date - mtd, 2),     # positive = behind pace
        "today_needed": round(today_needed, 2),
        "attainment_pct": round(100.0 * mtd / target, 1) if target else 0.0,
        # new/<trailing-window managers have no run-rate yet -> "no_target", not a misleading "on"
        "pace_status": _pace_status(mtd, expected_to_date) if target > 0 else "no_target",
    }


def compute_target(manager_id: int, as_of: str | None = None,
                   trailing_months: int | None = None) -> dict:
    """Current-month minimum target + pace for a manager, for both shipped and paid."""
    s = get_settings()
    trailing_months = trailing_months if trailing_months is not None else s.target_trailing_months
    today = (datetime.strptime(as_of, "%Y-%m-%d").date() if as_of
             else datetime.now(ZoneInfo(s.timezone)).date())
    current_month = today.strftime("%Y-%m")

    first_of_month = today.replace(day=1)
    since = (first_of_month - timedelta(days=1)).replace(day=1)
    for _ in range(trailing_months):  # step back trailing_months whole months
        since = (since - timedelta(days=1)).replace(day=1)
    since_str = since.isoformat()
    asof_excl = (today + timedelta(days=1)).isoformat()  # include today in MTD (queries use < :asof)

    shipped = sig.monthly_shipped(manager_id, since_str, asof_excl)
    paid = sig.monthly_paid(manager_id, since_str, asof_excl)

    wd = working_days_in_month(today.year, today.month)
    wd_elapsed = working_days_elapsed(today)

    return {
        "manager_id": manager_id,
        "month": current_month,
        "as_of": today.isoformat(),
        "working_days": wd,
        "working_days_elapsed": wd_elapsed,
        "shipped": _metric(shipped, current_month, shipped.get(current_month, 0.0),
                           wd, wd_elapsed, trailing_months),
        "paid": _metric(paid, current_month, paid.get(current_month, 0.0),
                        wd, wd_elapsed, trailing_months),
    }
