"""Sales-forecast service — turns a monthly EUR history into the console response contract.

Output points are { "SaleAmount": <float EUR>, "MonthNameUK": "<Укр short month + year>" }
(PascalCase, exactly what the «Прогноз продажів» reader expects after the gba-server proxy).
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite

from app.core.config import Settings
from app.core.history import requested_history_start, resolve_history_window
from app.services.forecasting import demand, selection

# month (1-12) -> Ukrainian short month label, per the spec mapping.
MONTHS_UK = ["Січ", "Лют", "Бер", "Кві", "Тра", "Чер", "Лип", "Сер", "Вер", "Жов", "Лис", "Гру"]
_EUR_CENT = Decimal("0.01")


def month_name_uk(year: int, month: int) -> str:
    """'<Укр short month> <year>', e.g. (2026, 7) -> 'Лип 2026'. month is 1-12."""
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range: {month}")
    return f"{MONTHS_UK[month - 1]} {year}"


def history_labels(
    as_of: date,
    months: int,
    source_history_start: date | None = None,
) -> list[str]:
    """Trailing calendar labels, clamped to a factual source boundary when supplied."""
    if source_history_start is None:
        start = requested_history_start(as_of, months)
    else:
        start = resolve_history_window(as_of, months, source_history_start).effective_start

    labels: list[str] = []
    y, m = start.year, start.month
    end_index = as_of.year * 12 + as_of.month - 1
    current_index = y * 12 + m - 1
    while current_index <= end_index:
        labels.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y += 1
            m = 1
        current_index += 1
    return labels


def series_from(history: dict[str, float], labels: list[str]) -> list[float]:
    """Dense value series aligned to `labels` (oldest first); missing months -> 0.0."""
    return [float(history.get(lbl, 0.0)) for lbl in labels]


def future_months(as_of: date, horizon: int) -> list[tuple[int, int]]:
    """The next `horizon` (year, month) pairs after as_of's month, in order."""
    out: list[tuple[int, int]] = []
    y, m = as_of.year, as_of.month
    for _ in range(horizon):
        m += 1
        if m == 13:
            y += 1
            m = 1
        out.append((y, m))
    return out


def _non_zero_months(series: list[float]) -> int:
    return sum(1 for v in series if v > 0)


def eur_cents(value: object) -> float:
    """Accounting round a finite EUR value to non-negative cents.

    Python's built-in ``round`` uses bankers' rounding and binary floats, which makes exact
    half-cent outcomes disagree with accounting/UI totals. Decimal(str(value)) plus
    ROUND_HALF_UP is the fleet-wide money boundary.
    """
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("forecast amount is not a finite number") from exc
    if not amount.is_finite():
        raise ValueError("forecast amount is not a finite number")
    if amount < 0:
        amount = Decimal("0")
    try:
        rounded = amount.quantize(_EUR_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("forecast amount is outside the supported EUR range") from exc
    result = float(rounded)
    if not isfinite(result):
        raise ValueError("forecast amount is outside the supported EUR range")
    return result


def forecast_points(
    history: dict[str, float], as_of: date, cfg: Settings, horizon: int | None = None
) -> list[dict]:
    """Forecast the next N months from a {yyyy-MM: eur} history; shape into contract points.

    Returns [] when the series is too thin (fewer than cfg.min_history_months non-zero months),
    so the console renders «немає даних» instead of a meaningless flat line. Never raises on
    sparse data.
    """
    horizon = horizon if horizon is not None else cfg.forecast_horizon_months
    if horizon <= 0:
        return []

    labels = history_labels(as_of, cfg.history_months, cfg.source_history_start_date)
    series = series_from(history, labels)
    if _non_zero_months(series) < cfg.min_history_months:
        return []

    # Resolve the concrete method per series: in "auto" mode the Syntetos-Boylan segment of
    # this series picks the forecaster (smooth -> EWMA, erratic -> moving_avg, intermittent/
    # lumpy -> SBA); a forced FORECAST_METHOD keeps the single-method behaviour. Contract is
    # unchanged — EWMA is a flat level, damped_trend a per-step path; forecast_series handles both.
    method, _segment = selection.select_method(series, cfg.forecast_method)
    projected = demand.forecast_series(series, horizon, method, cfg.croston_alpha)
    points: list[dict] = []
    for (y, m), amount in zip(future_months(as_of, horizon), projected, strict=True):
        points.append({"SaleAmount": eur_cents(amount), "MonthNameUK": month_name_uk(y, m)})
    return points
