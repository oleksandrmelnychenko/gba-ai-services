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
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.money import cents, cents_decimal, decimal_value, tenths
from app.data import signals_repository as sig

_SUNDAY = 6


def working_days_in_month(year: int, month: int) -> int:
    """Mon–Sat days in the month (Sunday excluded)."""
    days = calendar.monthrange(year, month)[1]
    return sum(1 for d in range(1, days + 1) if date(year, month, d).weekday() != _SUNDAY)


def working_days_elapsed(as_of: date) -> int:
    """Mon–Sat days from the 1st of as_of's month through as_of inclusive."""
    return sum(1 for d in range(1, as_of.day + 1) if date(as_of.year, as_of.month, d).weekday() != _SUNDAY)


def run_rate(series: dict[str, object], current_month: str, n: int = 3) -> Decimal:
    """Average of the n most-recent COMPLETED months (current partial month excluded).

    This is the conservative 'minimum' floor: you've recently been selling this much, at minimum
    keep it up. Returns 0.0 if there's no completed history yet.
    """
    completed = sorted(k for k in series if k < current_month)
    recent = completed[-n:]
    if not recent:
        return Decimal("0")
    total = sum((decimal_value(series[key]) for key in recent), start=Decimal("0"))
    return total / Decimal(len(recent))


def _pace_status(actual: Decimal, expected: Decimal) -> str:
    if expected <= 0:
        return "on"
    ratio = actual / expected
    if ratio >= Decimal("1.05"):
        return "ahead"
    if ratio < Decimal("0.95"):
        return "behind"
    return "on"


def _metric(series: dict[str, object], current_month: str, mtd: object,
            wd: int, wd_elapsed: int, n: int) -> dict:
    # Target and MTD are the canonical cent values exposed by the API. Every derived field is
    # calculated from those same values so the C# proxy and console can recompute the contract
    # exactly; hidden sub-cent inputs must not make the displayed fields disagree.
    target = cents_decimal(run_rate(series, current_month, n))
    mtd_value = cents_decimal(decimal_value(mtd))
    daily_pace = target / Decimal(wd) if wd else Decimal("0")
    expected_to_date = daily_pace * Decimal(wd_elapsed)
    remaining_target = max(target - mtd_value, Decimal("0"))
    remaining_wd = max(wd - wd_elapsed, 0)
    today_needed = (
        remaining_target / Decimal(remaining_wd) if remaining_wd else remaining_target
    )
    return {
        "target": cents(target),
        "mtd": cents(mtd_value),
        "daily_pace": cents(daily_pace),
        "expected_to_date": cents(expected_to_date),
        "gap": cents(expected_to_date - mtd_value),     # positive = behind pace
        "today_needed": cents(today_needed),
        "attainment_pct": tenths(Decimal("100") * mtd_value / target) if target else 0.0,
        # new/<trailing-window managers have no run-rate yet -> "no_target", not a misleading "on"
        "pace_status": (
            _pace_status(mtd_value, expected_to_date) if target > 0 else "no_target"
        ),
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
