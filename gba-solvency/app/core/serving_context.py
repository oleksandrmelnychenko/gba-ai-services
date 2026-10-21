"""Serving-only date/window policy; historical training helpers remain independent."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.history import require_supported_as_of

BUSINESS_TIMEZONE = "Europe/Kyiv"
STATE_BASIS = "current_observation"
TRAINED_WINDOW_MONTHS = 12
HISTORICAL_SCORE_REASON = (
    "Історію оцінки не збережено. Поточні борги та умови договорів "
    "не відновлюють стан минулих місяців."
)
NON_BUYER_SCORE_REASON = "Оцінка не застосовується: клієнт не має ролі покупця."


class ServingConfigurationError(RuntimeError):
    """The configured FX date cannot support the current-observation response."""


def utc_now() -> datetime:
    """One injectable clock read per request, independent of the host timezone."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ServingContext:
    as_of: str
    fx_date: str


def observation_fields(as_of: str) -> dict[str, str]:
    """Describe a current read, without claiming an atomic or historical snapshot."""
    return {
        "state_basis": STATE_BASIS,
        "business_timezone": BUSINESS_TIMEZONE,
        "fx_date": as_of,
    }


def resolve_current_context(
    as_of_date: str | None,
    window_months: int,
    *,
    scoring: bool,
) -> ServingContext:
    """Reject unsupported requests before client lookup, cache access, or feature SQL."""
    if type(window_months) is not int or (
        window_months != TRAINED_WINDOW_MONTHS if scoring else not 1 <= window_months <= 60
    ):
        raise ValueError(
            "window_months must be 12 for the trained scorecard"
            if scoring else "chart window_months must be between 1 and 60"
        )
    current_day = utc_now().astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date()
    requested_day = require_supported_as_of(
        current_day.isoformat() if as_of_date is None else as_of_date
    )
    if requested_day != current_day:
        raise ValueError(
            "as_of_date must equal the current Europe/Kyiv date; "
            "historical and future solvency states are not recorded"
        )
    as_of = current_day.isoformat()
    fx_date = get_settings().resolve_fx_date(as_of)
    if fx_date != as_of:
        raise ServingConfigurationError(
            "Solvency current observation is unavailable: configured FX date "
            "does not match its Europe/Kyiv date"
        )
    return ServingContext(as_of=as_of, fx_date=fx_date)
