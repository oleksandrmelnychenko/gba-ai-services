"""Shared transactional-history boundary for solvency features and API metadata."""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from app.core.config import get_settings


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
