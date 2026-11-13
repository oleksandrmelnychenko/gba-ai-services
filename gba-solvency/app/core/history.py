"""Shared transactional-history boundary for solvency features and API metadata."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import get_settings

BUSINESS_ZONE = ZoneInfo("Europe/Kyiv")
SQL_BUSINESS_ZONE = "FLE Standard Time"


@dataclass(frozen=True)
class HistoryCoverage:
    source_history_start: date
    requested_start: date
    effective_start: date
    history_complete: bool


def source_history_start() -> date:
    return get_settings().source_history_start_date


def require_supported_as_of(value: str | date) -> date:
    resolved = date.fromisoformat(value) if isinstance(value, str) else value
    floor = source_history_start()
    if resolved < floor:
        raise ValueError(
            f"as_of_date must be on or after source history start {floor.isoformat()}"
        )
    return resolved


def subtract_months(value: date, months: int) -> date:
    if months < 0:
        raise ValueError("months must be non-negative")
    ordinal = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(ordinal, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def coverage(value: str | date, months: int) -> HistoryCoverage:
    resolved = require_supported_as_of(value)
    floor = source_history_start()
    requested_start = subtract_months(resolved, months)
    effective_start = max(floor, requested_start)
    return HistoryCoverage(
        source_history_start=floor,
        requested_start=requested_start,
        effective_start=effective_start,
        history_complete=requested_start >= floor,
    )


@dataclass(frozen=True)
class SalesWindow:
    """UTC instants of Europe/Kyiv midnights bounding stored (UTC) sale timestamps.

    The window is half-open: complete Kyiv days from `start` up to, not including, `end`
    (the as-of day). `series_start` is the first Kyiv month start inside the window, so a
    monthly series never opens with a partial month. `periods` are the Kyiv months of that
    series, from the first full month through the month of the last complete day.
    """

    history_start: datetime
    start: datetime
    end: datetime
    series_start: datetime
    periods: tuple[str, ...] = ()

    def params(self) -> dict[str, datetime]:
        return {
            "history_start": self.history_start,
            "window_start": self.start,
            "window_end": self.end,
        }


def local_midnight_utc(day: date) -> datetime:
    local = datetime.combine(day, time.min, tzinfo=BUSINESS_ZONE)
    return local.astimezone(UTC).replace(tzinfo=None)


def month_keys(first: date, last: date) -> tuple[str, ...]:
    keys = []
    ordinal = first.year * 12 + first.month - 1
    while ordinal <= last.year * 12 + last.month - 1:
        year, month_index = divmod(ordinal, 12)
        keys.append(f"{year:04d}-{month_index + 1:02d}")
        ordinal += 1
    return tuple(keys)


def sales_window(as_of: str | date, months: int) -> SalesWindow:
    resolved = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    floor = source_history_start()
    start_day = max(floor, subtract_months(resolved, months))
    series_day = start_day
    if series_day.day != 1:
        year, month = divmod(series_day.year * 12 + series_day.month, 12)
        series_day = date(year, month + 1, 1)
    return SalesWindow(
        history_start=local_midnight_utc(floor),
        start=local_midnight_utc(start_day),
        end=local_midnight_utc(resolved),
        series_start=local_midnight_utc(series_day),
        periods=month_keys(series_day, resolved - timedelta(days=1)),
    )
