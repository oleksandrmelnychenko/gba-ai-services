"""Canonical factual-history boundary for pricing behavioral signals."""

from __future__ import annotations

import hashlib
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class HistoryWindow:
    """One trailing pricing interval resolved against the source-history floor."""

    as_of: date
    requested_start: date
    source_history_start: date
    effective_start: date
    history_complete: bool


def as_date(value: date | datetime | str) -> date:
    """Normalize supported point-in-time values without changing their calendar date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def subtract_calendar_months(value: date | datetime | str, months: int) -> date:
    """Match SQL Server ``DATEADD(month, -months, value)`` calendar semantics."""
    if months < 1:
        raise ValueError("history months must be greater than 0")
    value_date = as_date(value)
    month_index = value_date.year * 12 + value_date.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value_date.day, monthrange(year, month)[1])
    return date(year, month, day)


def trailing_month_history_window(
    as_of: date | datetime | str,
    months: int,
    source_history_start: date | datetime | str,
) -> HistoryWindow:
    """Clamp an exact trailing-month window to the first factual source date."""
    as_of_date = as_date(as_of)
    source_start = as_date(source_history_start)
    if as_of_date < source_start:
        raise ValueError("as_of_date_before_source_history_start")
    requested_start = subtract_calendar_months(as_of_date, months)
    return HistoryWindow(
        as_of=as_of_date,
        requested_start=requested_start,
        source_history_start=source_start,
        effective_start=max(requested_start, source_start),
        history_complete=requested_start >= source_start,
    )


def history_contract_fingerprint(source_history_start: date | datetime | str) -> str:
    """Stable cache/API epoch for the pricing source-floor contract."""
    source_start = as_date(source_history_start).isoformat()
    return hashlib.sha256(f"pricing-history-v1|{source_start}".encode()).hexdigest()[:16]


def model_contract_fingerprint(
    model_version: str,
    source_history_start: date | datetime | str,
    trailing_window_months: int,
) -> str:
    """Stable serving-model namespace including its factual-history contract."""
    history_fingerprint = history_contract_fingerprint(source_history_start)
    payload = f"pricing-model-v1|{model_version}|{history_fingerprint}|{trailing_window_months}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return f"{model_version}-{digest}"


def history_metadata(
    window: HistoryWindow,
    *,
    model_version: str,
    trailing_window_months: int,
) -> dict[str, Any]:
    """Serialize the factual coverage contract used for one pricing result."""
    return {
        "source_history_start": window.source_history_start.isoformat(),
        "requested_start": window.requested_start.isoformat(),
        "effective_start": window.effective_start.isoformat(),
        "history_complete": window.history_complete,
        "history_fingerprint": history_contract_fingerprint(window.source_history_start),
        "model_fingerprint": model_contract_fingerprint(
            model_version,
            window.source_history_start,
            trailing_window_months,
        ),
    }
