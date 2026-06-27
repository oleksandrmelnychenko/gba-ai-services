"""Monthly-series projection — REUSES gba-procure's forecasting math.

The pure rate functions below are lifted from gba-procure
(app/services/forecasting/demand.py: _moving_avg_rate / _croston_rate / SBA correction)
and adapted to operate on a MONTHLY value series (EUR sale amount per month) instead of a
daily-unit series. The intermittent-demand logic is identical: Croston smooths the demand
SIZE and the INTER-DEMAND INTERVAL separately and forecasts rate = size / interval; SBA
multiplies by (1 - alpha/2) to remove Croston's documented positive bias. moving_avg is the
mean over the dense (zero-filled) window. These three return a per-month rate so the forecast
horizon is simply rate repeated N months.

B2B sales are intermittent (many zero months), so Croston/SBA exist as config-selectable
alternatives to the plain mean — same rationale as procure's spare-parts demand.

TREND/RECENCY-AWARE methods (added after the rolling-origin backtest measured a systematic
NEGATIVE bias on smooth/erratic series — these EUR series trend upward and any backward-looking
average lags a growing series):

  ewma          — exponentially-weighted moving average over the dense monthly series. Weights
                  recent months more, so it tracks a level that is drifting up/down faster than
                  the flat window mean. Reuses procure's EWMA recursion (level = alpha*x +
                  (1-alpha)*level), without procure's /30 daily conversion since this series is
                  already monthly. Still a FLAT projection (the smoothed level repeated).
  damped_trend  — Holt linear (level + trend) with a damping factor phi (Gardner-McKenzie). The
                  principled fix for trend-lag: it extrapolates the recent slope but damps each
                  step by phi so it does not over-extrapolate a transient run. This is the only
                  method whose horizon path is NOT flat — step h adds (phi + phi^2 + ... + phi^h)
                  * trend to the level. Falls back to a flat EWMA-style level on degenerate input.

Two projection shapes exist: a flat per-month RATE (moving_avg/croston/sba/ewma) and a
per-step PATH (damped_trend). `forecast_path` is the general contract used by both the live
service and the backtest; `monthly_rate` remains for the flat methods / logging.
"""

from __future__ import annotations

_METHOD_MOVING_AVG = "moving_average_v0"
_METHOD_CROSTON = "croston_v1"
_METHOD_SBA = "sba_v1"
_METHOD_EWMA = "ewma_v1"
_METHOD_DAMPED_TREND = "damped_trend_v1"

# Default smoothing/damping constants for the trend-aware methods. Tuned on the rolling-origin
# backtest (scripts/run_backtest.py); see that report. EWMA alpha is intentionally heavier than
# croston_alpha (0.1) because the goal here is RECENCY (track the drift), not interval smoothing.
EWMA_ALPHA = 0.3
# Holt level/trend smoothing and the damping factor. phi < 1 damps the extrapolated slope so a
# short upswing is not projected linearly forever; phi = 1 would be undamped Holt linear.
HOLT_ALPHA = 0.4
HOLT_BETA = 0.2
HOLT_PHI = 0.85

# Method ids that carry a non-flat horizon path (level + damped trend). Everything else is flat.
_PATH_METHODS = {"damped_trend"}


def moving_avg_rate(series: list[float]) -> float:
    """Mean per-month value: total spread across the full (zero-filled) window."""
    if not series:
        return 0.0
    return sum(series) / len(series)


def ewma_rate(series: list[float], alpha: float) -> float:
    """Recency-weighted per-month level via EWMA over the dense monthly series.

    Mirror of procure's _ewma_monthly_rate (level = alpha*x + (1-alpha)*level) but WITHOUT the
    /30 daily conversion — this series is already a monthly EUR value, so the smoothed level IS
    the per-month rate. Seeds the level at the first month and folds in each subsequent month,
    weighting recent months more heavily (effective memory ~ 1/alpha months). Zero months count
    (they pull the level down), so a fading series decays toward 0 as it should. Returns 0.0 on
    an empty series; never negative for a non-negative series.
    """
    if not series:
        return 0.0
    level = series[0]
    for value in series[1:]:
        level = alpha * value + (1.0 - alpha) * level
    return max(0.0, level)


def croston_rate(series: list[float], alpha: float, sba: bool) -> float:
    """Per-month rate via Croston's method (SBA-corrected when sba=True).

    Mirror of procure's _croston_rate, with index-based inter-demand INTERVALS measured in
    months. Smooths demand SIZE z on demand-occurrence periods and the interval p between
    consecutive non-zero months (the first interval measured from the window start). Rate =
    z / p. Falls back to the moving average when fewer than two demand events exist.
    """
    events = [(i, v) for i, v in enumerate(series) if v > 0]
    if not events:
        return 0.0
    if len(events) < 2:
        return moving_avg_rate(series)

    idxs = [i for i, _ in events]
    sizes = [v for _, v in events]

    intervals: list[float] = []
    prev = -1  # window start anchor (one period before index 0)
    for i in idxs:
        gap = i - prev
        intervals.append(float(gap) if gap > 0 else 1.0)
        prev = i

    z = sizes[0]
    p = intervals[0] if intervals[0] > 0 else 1.0
    for k in range(1, len(sizes)):
        z = z + alpha * (sizes[k] - z)
        p = p + alpha * (intervals[k] - p)

    if p <= 0:
        return 0.0
    rate = z / p
    if sba:
        rate *= 1.0 - alpha / 2.0
    return max(0.0, rate)


def damped_trend_path(
    series: list[float],
    horizon: int,
    *,
    alpha: float = HOLT_ALPHA,
    beta: float = HOLT_BETA,
    phi: float = HOLT_PHI,
) -> list[float]:
    """Holt linear (level + trend) with damping factor phi — a per-step horizon PATH.

    The principled fix for the measured trend-lag: a backward average forecasts the PAST level,
    so on an upward-trending EUR series it always under-shoots. Holt tracks BOTH a level l and a
    local slope b, and projects step h as:

        forecast(h) = l + (phi + phi^2 + ... + phi^h) * b

    The damping factor phi in (0, 1] shrinks the slope's contribution geometrically so a recent
    run-up is extrapolated but not forever (Gardner-McKenzie damped trend). Recursion over the
    dense monthly series:

        l_t = alpha * x_t + (1 - alpha) * (l_{t-1} + phi * b_{t-1})
        b_t = beta  * (l_t - l_{t-1}) + (1 - beta) * phi * b_{t-1}

    Seeds l = series[0], b = series[1] - series[0] (0 if <2 points). Returns a flat EWMA-style
    level (trend collapsed) on a degenerate / single-point series, and clamps every step at >= 0
    so a downward slope can drive the projection to zero but never negative — the EUR-sales
    contract has no negative months.
    """
    h = max(0, horizon)
    if h == 0:
        return []
    if not series:
        return [0.0] * h
    if len(series) == 1:
        return [max(0.0, series[0])] * h

    level = series[0]
    trend = series[1] - series[0]
    for value in series[1:]:
        prev_level = level
        level = alpha * value + (1.0 - alpha) * (level + phi * trend)
        trend = beta * (level - prev_level) + (1.0 - beta) * phi * trend

    out: list[float] = []
    phi_sum = 0.0
    phi_pow = 1.0
    for _ in range(h):
        phi_pow *= phi  # phi^1, phi^2, ... for steps 1..h
        phi_sum += phi_pow
        out.append(max(0.0, level + phi_sum * trend))
    return out


def monthly_rate(series: list[float], method: str, alpha: float) -> tuple[float, str]:
    """Dispatch a FLAT method to its per-month rate; returns (per_month_rate, method_id).

    Only the flat methods (moving_avg/croston/sba/ewma) have a single representative rate. The
    damped_trend method is a per-step path and has no single rate — it resolves here to its
    first-step value (l + phi*b) purely so logging/metrics that want one number stay defined;
    callers that project a horizon must use forecast_path / forecast_series, not this rate.
    """
    method = (method or "moving_avg").lower()
    if method == "croston":
        return croston_rate(series, alpha, sba=False), _METHOD_CROSTON
    if method == "sba":
        return croston_rate(series, alpha, sba=True), _METHOD_SBA
    if method == "ewma":
        return ewma_rate(series, EWMA_ALPHA), _METHOD_EWMA
    if method == "damped_trend":
        path = damped_trend_path(series, 1)
        return (path[0] if path else 0.0), _METHOD_DAMPED_TREND
    return moving_avg_rate(series), _METHOD_MOVING_AVG


def forecast_path(series: list[float], horizon: int, method: str, alpha: float) -> list[float]:
    """Project the next `horizon` months as a per-step path — the general projection contract.

    For FLAT methods (moving_avg/croston/sba/ewma) this is the single per-month rate repeated
    across the horizon (the procure contract, unchanged). For damped_trend it is the Holt
    level+damped-slope sequence, which varies per step. Every value is clamped at >= 0. This is
    the one function both the live service and the backtest call so flat and path methods are
    scored/served identically.
    """
    h = max(0, horizon)
    method = (method or "moving_avg").lower()
    if method == "damped_trend":
        return damped_trend_path(series, h)
    rate, _ = monthly_rate(series, method, alpha)
    rate = max(0.0, rate)
    return [rate for _ in range(h)]


def forecast_series(series: list[float], horizon: int, method: str, alpha: float) -> list[float]:
    """Project the next `horizon` months. Thin alias of forecast_path kept for the existing
    call sites / tests: a flat method yields a repeated rate, damped_trend yields its path.

    `method` is a concrete method ("moving_avg" | "croston" | "sba" | "ewma" | "damped_trend").
    For the per-segment "auto" selection, callers resolve the concrete method first (see
    selection.select_method); this function never classifies — it just projects what it is told.
    """
    return forecast_path(series, horizon, method, alpha)
