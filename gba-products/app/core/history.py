"""Canonical factual-history boundaries shared by product-intelligence signals."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class HistoryWindow:
    """One requested historical interval resolved against the factual source floor."""

    as_of: date
    requested_start: date
    source_history_start: date
    effective_start: date
    history_complete: bool

    @property
    def effective_days(self) -> int:
        """Number of factual days in the half-open interval ``[effective_start, as_of)``."""
        return max(0, (self.as_of - self.effective_start).days)


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def resolve_history_window(
    as_of: date | datetime | str,
    requested_start: date | datetime | str,
    source_history_start: date | datetime | str,
) -> HistoryWindow:
    """Resolve an explicit half-open interval without ever crossing the source floor."""
    as_of_date = as_date(as_of)
    requested = as_date(requested_start)
    source_start = as_date(source_history_start)
    if as_of_date < source_start:
        raise ValueError("as_of_date_before_source_history_start")
    if requested > as_of_date:
        raise ValueError("requested history start must not be after as_of")
    return HistoryWindow(
        as_of=as_of_date,
        requested_start=requested,
        source_history_start=source_start,
        effective_start=max(requested, source_start),
        history_complete=requested >= source_start,
    )


def day_history_window(
    as_of: date | datetime | str,
    days: int,
    source_history_start: date | datetime | str,
) -> HistoryWindow:
    if days < 1:
        raise ValueError("history days must be greater than 0")
    as_of_date = as_date(as_of)
    return resolve_history_window(
        as_of_date,
        as_of_date - timedelta(days=days),
        source_history_start,
    )


def month_history_window(
    as_of: date | datetime | str,
    months: int,
    source_history_start: date | datetime | str,
) -> HistoryWindow:
    if months < 1:
        raise ValueError("history months must be greater than 0")
    as_of_date = as_date(as_of)
    month_index = as_of_date.year * 12 + as_of_date.month - 1 - (months - 1)
    year, zero_based_month = divmod(month_index, 12)
    requested_start = date(year, zero_based_month + 1, 1)
    return resolve_history_window(as_of_date, requested_start, source_history_start)


def history_contract_fingerprint(source_history_start: date | datetime | str) -> str:
    """Stable cache/API epoch for the source-floor contract."""
    source_start = as_date(source_history_start).isoformat()
    return hashlib.sha256(f"products-history-v1|{source_start}".encode()).hexdigest()[:16]


def window_metadata(window: HistoryWindow) -> dict[str, Any]:
    return {
        "source_history_start": window.source_history_start.isoformat(),
        "requested_start": window.requested_start.isoformat(),
        "effective_start": window.effective_start.isoformat(),
        "history_complete": window.history_complete,
        "effective_days": window.effective_days,
    }


def combined_history_metadata(windows: Mapping[str, HistoryWindow]) -> dict[str, Any]:
    """Summarize several signal windows without hiding their individual boundaries."""
    if not windows:
        raise ValueError("at least one history window is required")
    values = list(windows.values())
    as_of = values[0].as_of
    source_start = values[0].source_history_start
    if any(window.as_of != as_of for window in values):
        raise ValueError("history windows must share as_of")
    if any(window.source_history_start != source_start for window in values):
        raise ValueError("history windows must share source_history_start")
    return {
        "source_history_start": source_start.isoformat(),
        "requested_start": min(window.requested_start for window in values).isoformat(),
        "effective_start": min(window.effective_start for window in values).isoformat(),
        "history_complete": all(window.history_complete for window in values),
        "history_fingerprint": history_contract_fingerprint(source_start),
        "history_windows": {
            name: window_metadata(window)
            for name, window in windows.items()
        },
    }
