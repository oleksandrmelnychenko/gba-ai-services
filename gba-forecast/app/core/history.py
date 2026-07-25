"""Canonical source-history boundary and rolling-window calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class HistoryWindow:
    """Resolved calendar window for one point-in-time forecast."""

    as_of: date
    requested_start: date
    source_history_start: date
    effective_start: date
    history_complete: bool


def as_date(value: date | datetime | str) -> date:
    """Normalize supported point-in-time values without changing their timezone/date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def requested_history_start(as_of: date | datetime | str, months: int) -> date:
    """First day of the oldest calendar month in a trailing inclusive month window."""
    if months < 1:
        raise ValueError("history months must be greater than 0")
    as_of_date = as_date(as_of)
    month_index = as_of_date.year * 12 + as_of_date.month - 1 - (months - 1)
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def resolve_history_window(
    as_of: date | datetime | str,
    months: int,
    source_history_start: date | datetime | str,
) -> HistoryWindow:
    """Clamp a rolling calendar window to the first date available from the source."""
    as_of_date = as_date(as_of)
    source_start = as_date(source_history_start)
    if as_of_date < source_start:
        raise ValueError("as_of_date_before_source_history_start")

    requested_start = requested_history_start(as_of_date, months)
    return HistoryWindow(
        as_of=as_of_date,
        requested_start=requested_start,
        source_history_start=source_start,
        effective_start=max(requested_start, source_start),
        history_complete=requested_start >= source_start,
    )
