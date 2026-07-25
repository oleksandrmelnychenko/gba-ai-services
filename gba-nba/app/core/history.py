"""Canonical source-history boundary and window semantics for every NBA data path."""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from app.core.config import get_settings


class SourceHistoryBoundaryError(ValueError):
    """Raised when a requested business date predates the declared source history."""


@dataclass(frozen=True)
class HistoryWindow:
    """One requested window and the part of it covered by the declared source history."""

    as_of: date
    requested_start: date
    effective_start: date
    source_history_start: date

    @property
    def history_complete(self) -> bool:
        return self.effective_start == self.requested_start

    def metadata(self) -> dict[str, str | bool]:
        return {
            "source_history_start": self.source_history_start.isoformat(),
            "effective_start": self.effective_start.isoformat(),
            "history_complete": self.history_complete,
        }


def source_history_start() -> date:
    return get_settings().source_history_start_date


def parse_date(value: str | date, *, field_name: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def require_as_of(value: str | date) -> date:
    as_of = parse_date(value, field_name="as_of")
    floor = source_history_start()
    if as_of < floor:
        raise SourceHistoryBoundaryError(
            f"as_of {as_of.isoformat()} predates source history "
            f"{floor.isoformat()}"
        )
    return as_of


def explicit_window(requested_start: str | date, as_of: str | date) -> HistoryWindow:
    end = require_as_of(as_of)
    requested = parse_date(requested_start, field_name="requested_start")
    if requested > end:
        raise ValueError("requested_start must not be after as_of")
    floor = source_history_start()
    return HistoryWindow(
        as_of=end,
        requested_start=requested,
        effective_start=max(requested, floor),
        source_history_start=floor,
    )


def rolling_days(as_of: str | date, days: int) -> HistoryWindow:
    if days < 0:
        raise ValueError("days must be non-negative")
    end = require_as_of(as_of)
    return explicit_window(end - timedelta(days=days), end)


def _subtract_months(value: date, months: int) -> date:
    if months < 0:
        raise ValueError("months must be non-negative")
    absolute_month = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def rolling_months(as_of: str | date, months: int) -> HistoryWindow:
    end = require_as_of(as_of)
    return explicit_window(_subtract_months(end, months), end)


def factual_window(as_of: str | date) -> HistoryWindow:
    end = require_as_of(as_of)
    floor = source_history_start()
    return HistoryWindow(
        as_of=end,
        requested_start=floor,
        effective_start=floor,
        source_history_start=floor,
    )


def training_window(as_of: str | date, window_days: int = 365) -> HistoryWindow:
    window = rolling_days(as_of, window_days)
    if not window.history_complete:
        raise SourceHistoryBoundaryError(
            f"training snapshot {window.as_of.isoformat()} has only partial history; "
            f"requires {window_days} days after {window.source_history_start.isoformat()}"
        )
    return window
