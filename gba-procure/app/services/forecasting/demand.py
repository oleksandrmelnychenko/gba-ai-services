"""Demand forecasting — pluggable methods behind a stable interface.

A simple moving-average / mean baseline plus Croston's method and the SBA
(Syntetos-Boylan Approximation) bias-corrected variant for intermittent demand.
All three sit behind `forecast_from_rows`/`forecast_product` and return the same
DemandForecast shape (mean_daily/std_daily/forecast_units) so the replenishment
policy is unchanged regardless of which method produced the numbers.

The method is config-selectable (`forecast_method` = 'moving_avg'|'croston'|'sba'),
with 'moving_avg' as the default until another method is proven on real data.

B2B spare-parts demand is intermittent (sporadic, many zero-days). Classic smoothing
spreads units across the window; Croston/SBA instead smooth the demand SIZE and the
INTER-DEMAND INTERVAL separately and forecast the per-period rate as size/interval —
the textbook fix for intermittent series.
"""
from __future__ import annotations

import math
from datetime import date

from app.core.config import get_settings
from app.data import supply_repository as repo
from app.domain.models import DemandForecast

_METHOD_MOVING_AVG = "moving_average_v0"
_METHOD_CROSTON = "croston_v1"
_METHOD_SBA = "sba_v1"
_METHOD_EWMA = "ewma_v1"


def seasonal_index_for(rows: list[dict], target_month: int, k: float = 4.0,
                       lo: float = 0.6, hi: float = 1.6, min_months: int = 12) -> float:
    """Shrinkage-weighted month-of-year demand index for target_month (1-12); 1.0 if thin.

    raw = avg(month-of-year demand) / avg(monthly demand); shrunk toward 1.0 by k pseudo-obs
    (history depth is shallow). Returns 1.0 when fewer than min_months of history exist.
    """
    monthly: dict[tuple[int, int], float] = {}
    for r in rows:
        d = r["d"]
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        monthly[(d.year, d.month)] = monthly.get((d.year, d.month), 0.0) + float(r["units"] or 0)
    if len(monthly) < min_months:
        return 1.0
    vals = list(monthly.values())
    overall = sum(vals) / len(vals)
    if overall <= 0:
        return 1.0
    obs = [v for (_, m), v in monthly.items() if m == target_month]
    if not obs:
        return 1.0
    raw = (sum(obs) / len(obs)) / overall
    shrunk = (len(obs) * raw + k) / (len(obs) + k)
    return min(max(shrunk, lo), hi)


def method_for_xyz(xyz: str | None) -> str:
    """Per-quadrant forecaster: intermittent->SBA, variable->EWMA, regular->moving_avg."""
    if xyz == "Z":
        return "sba"
    if xyz == "Y":
        return "ewma"
    return "moving_avg"


def _empirical_std_daily(rows: list[dict], history_days: int) -> float:
    """Std of the daily-demand series over the FULL window (implicit zero-days included).

    Shared by every method so safety-stock sizing (z*sqrt(LT)*std_daily) is held
    constant across forecasters — isolating the mean/rate change under A/B.
    """
    nonzero = [float(r["units"] or 0) for r in rows]
    zeros = history_days - len(nonzero)
    series = nonzero + [0.0] * max(zeros, 0)
    n = len(series) or 1
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / n
    return math.sqrt(var)


def _moving_avg_rate(rows: list[dict], history_days: int) -> float:
    """Mean daily demand: total observed units spread across the full window."""
    total_units = sum(float(r["units"] or 0) for r in rows)
    return total_units / history_days if history_days > 0 else 0.0


def _ewma_monthly_rate(rows: list[dict], alpha: float) -> float:
    """Recency-weighted daily rate: EWMA over the monthly demand series / 30."""
    monthly: dict[str, float] = {}
    for r in rows:
        d = r["d"]
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        key = f"{d.year:04d}-{d.month:02d}"
        monthly[key] = monthly.get(key, 0.0) + float(r["units"] or 0)
    if not monthly:
        return 0.0
    series = [monthly[k] for k in sorted(monthly)]
    level = series[0]
    for m in series[1:]:
        level = alpha * m + (1.0 - alpha) * level
    return level / 30.0


def _demand_events(rows: list[dict]) -> list[tuple[date, float]]:
    """Ordered (day, units) for days with positive demand. Tolerates str/date keys."""
    events: list[tuple[date, float]] = []
    for r in rows:
        units = float(r["units"] or 0)
        if units <= 0:
            continue
        d = r["d"]
        if isinstance(d, str):
            d = date.fromisoformat(d[:10])
        events.append((d, units))
    events.sort(key=lambda e: e[0])
    return events


def _croston_rate(rows: list[dict], history_days: int, alpha: float, sba: bool) -> float:
    """Per-day demand rate via Croston's method (SBA-corrected when sba=True).

    Smooths demand SIZE z on demand-occurrence periods and the INTER-DEMAND INTERVAL p
    (gaps in days between consecutive demand days, the first interval measured from the
    window start). Rate = z / p. SBA multiplies by (1 - alpha/2) to remove Croston's
    well-documented positive bias. Falls back to the moving-average spread when there are
    too few events to form an interval.
    """
    events = _demand_events(rows)
    if not events:
        return 0.0
    if len(events) < 2:
        return _moving_avg_rate(rows, history_days)

    days = [e[0] for e in events]
    sizes = [e[1] for e in events]

    window_start = days[0] - _timedelta_days(_window_lookback(days, history_days))
    intervals: list[float] = []
    prev = window_start
    for d in days:
        gap = (d - prev).days
        intervals.append(float(gap) if gap > 0 else 1.0)
        prev = d

    z = sizes[0]
    p = intervals[0] if intervals[0] > 0 else 1.0
    for i in range(1, len(sizes)):
        z = z + alpha * (sizes[i] - z)
        p = p + alpha * (intervals[i] - p)

    if p <= 0:
        return 0.0
    rate = z / p
    if sba:
        rate *= 1.0 - alpha / 2.0
    return max(0.0, rate)


def _timedelta_days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


def _window_lookback(days: list[date], history_days: int) -> int:
    """Days from the window start to the first demand day, bounded by the window.

    Anchors the first inter-demand interval so a product whose only demand sits at the
    very start of the window isn't credited with an artificially short interval.
    """
    span_used = (days[-1] - days[0]).days
    remaining = max(history_days - span_used, 0)
    return min(remaining, history_days)


def forecast_from_rows(
    product_id: int, rows: list[dict], horizon_days: int | None = None,
    method: str | None = None,
) -> DemandForecast:
    """Forecast from an already-fetched daily-demand series (no DB I/O).

    Identical I/O contract to forecast_product — extracted so callers can batch the
    demand fetch (one bulk query) and forecast each product in-memory. The method is the
    explicit override (per-quadrant dispatch) or Settings.forecast_method; std_daily is
    computed identically for every method so the policy's safety-stock math is unchanged.
    """
    s = get_settings()
    horizon = horizon_days or s.forecast_horizon_days
    method = (method or s.forecast_method or "moving_avg").lower()

    if not rows:
        method_id = {
            "croston": _METHOD_CROSTON, "sba": _METHOD_SBA, "ewma": _METHOD_EWMA,
        }.get(method, _METHOD_MOVING_AVG)
        return DemandForecast(product_id=product_id, mean_daily=0.0, std_daily=0.0,
                              method=method_id, horizon_days=horizon, forecast_units=0.0)

    if method == "croston":
        mean_daily = _croston_rate(rows, s.history_days, s.croston_alpha, sba=False)
        method_id = _METHOD_CROSTON
    elif method == "sba":
        mean_daily = _croston_rate(rows, s.history_days, s.croston_alpha, sba=True)
        method_id = _METHOD_SBA
    elif method == "ewma":
        mean_daily = _ewma_monthly_rate(rows, s.ewma_alpha)
        method_id = _METHOD_EWMA
    else:
        mean_daily = _moving_avg_rate(rows, s.history_days)
        method_id = _METHOD_MOVING_AVG

    std_daily = _empirical_std_daily(rows, s.history_days)

    return DemandForecast(
        product_id=product_id,
        mean_daily=mean_daily,
        std_daily=std_daily,
        method=method_id,
        horizon_days=horizon,
        forecast_units=mean_daily * horizon,
    )


def forecast_product(product_id: int, as_of: str, horizon_days: int | None = None) -> DemandForecast:
    s = get_settings()
    rows = repo.product_daily_demand(product_id, as_of, s.history_days)
    return forecast_from_rows(product_id, rows, horizon_days)
