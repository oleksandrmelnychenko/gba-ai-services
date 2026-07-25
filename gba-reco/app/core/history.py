"""Shared transactional-history boundary for recommendation features and API metadata."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.core.config import get_settings


@dataclass(frozen=True)
class HistoryCoverage:
    source_history_start: date
    effective_start: date
    history_complete: bool


def source_history_start() -> date:
    return get_settings().source_history_start_date


def source_history_start_iso() -> str:
    return source_history_start().isoformat()


def parse_as_of(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return datetime.fromisoformat(normalized).date()


def require_supported_as_of(value: str | date | datetime) -> date:
    resolved = parse_as_of(value)
    floor = source_history_start()
    if resolved < floor:
        raise ValueError(
            f"as_of_date must be on or after source history start {floor.isoformat()}"
        )
    return resolved


def full_history_coverage(value: str | date | datetime) -> HistoryCoverage:
    require_supported_as_of(value)
    floor = source_history_start()
    return HistoryCoverage(
        source_history_start=floor,
        effective_start=floor,
        history_complete=True,
    )
