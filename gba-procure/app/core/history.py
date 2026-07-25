"""Shared source-history boundary and effective-window calculations.

The 1C source contract starts on 2025-01-01. Rolling models may request a
larger lookback, but they must divide and zero-fill only by the days that
actually exist on or after that boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.core.config import get_settings


@dataclass(frozen=True)
class HistoryCoverage:
    source_history_start: date
    requested_start: date
    effective_start: date
    requested_history_days: int
    effective_history_days: int
    history_complete: bool

    def as_metadata(self) -> dict[str, str | int | bool]:
        return {
            "source_history_start": self.source_history_start.isoformat(),
            "effective_start": self.effective_start.isoformat(),
            "effective_history_days": self.effective_history_days,
            "history_complete": self.history_complete,
        }


def as_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def source_history_start() -> date:
    return get_settings().source_history_start_date


def require_supported_as_of(value: str | date | datetime) -> date:
    resolved = as_date(value)
    floor = source_history_start()
    if resolved < floor:
        raise ValueError(
            f"as_of_date must be on or after source history start {floor.isoformat()}"
        )
    return resolved


def rolling_coverage(
    value: str | date | datetime,
    history_days: int,
) -> HistoryCoverage:
    if history_days < 0:
        raise ValueError("history_days must be non-negative")
    resolved = require_supported_as_of(value)
    floor = source_history_start()
    requested_start = resolved - timedelta(days=history_days)
    effective_start = max(requested_start, floor)
    return HistoryCoverage(
        source_history_start=floor,
        requested_start=requested_start,
        effective_start=effective_start,
        requested_history_days=history_days,
        effective_history_days=max((resolved - effective_start).days, 0),
        history_complete=requested_start >= floor,
    )


def history_start_iso() -> str:
    return source_history_start().isoformat()
