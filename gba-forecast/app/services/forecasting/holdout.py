"""Rolling-origin holdout scored by WAPE and bias per demand class.

`lead` is the number of months between the last training month and the scored month.
The live service trains on closed months only and its first point is the month after the
open one, so the refresh gate scores lead 2.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from app.services.forecasting import classify, demand, selection

REFUSED = "refused"
OVERALL = "overall"
CLASSES = (classify.SMOOTH, classify.ERRATIC, classify.INTERMITTENT, classify.LUMPY)

Forecaster = Callable[[list[float], str], float]


tsb_rate = demand.tsb_rate


def _mean(window: int) -> Forecaster:
    return lambda series, _segment: sum(series[-window:]) / min(window, len(series))


def _ewma(alpha: float) -> Forecaster:
    return lambda series, _segment: demand.ewma_rate(series, alpha)


def _tsb(alpha: float, beta: float) -> Forecaster:
    return lambda series, _segment: tsb_rate(series, alpha, beta)


def service_forecast(
    series: list[float],
    segment: str,
    croston_alpha: float = 0.1,
    scope: str = selection.SCOPE_CLIENT,
) -> float:
    return selection.auto_rate(series, segment, scope)[0]


def legacy_forecast(series: list[float], segment: str, croston_alpha: float = 0.1) -> float:
    method = selection.method_for_segment(segment, selection.LEGACY_SEGMENT_METHOD)
    return demand.forecast_path(series, 1, method, croston_alpha)[0]


CANDIDATES: dict[str, Forecaster] = {
    "service": service_forecast,
    "legacy_v2": legacy_forecast,
    "ewma_0.2": _ewma(0.2),
    "ewma_0.3": _ewma(0.3),
    "ewma_0.4": _ewma(0.4),
    "ewma_0.5": _ewma(0.5),
    "ewma_0.7": _ewma(0.7),
    "mean_3": _mean(3),
    "mean_6": _mean(6),
    "mean_12": _mean(12),
    "tsb_0.2_0.1": _tsb(0.2, 0.1),
    "tsb_0.3_0.2": _tsb(0.3, 0.2),
    "damped_trend": lambda series, _segment: demand.damped_trend_path(series, 1)[0],
    "naive_last": lambda series, _segment: series[-1],
}

GATE_METHODS = ("service", "legacy_v2", "mean_3")


def evaluate(
    series_list: Iterable[list[float]],
    *,
    origins: int = 6,
    min_non_zero: int = 3,
    methods: Iterable[str] = GATE_METHODS,
    scope: str = selection.SCOPE_CLIENT,
    lead: int = 1,
) -> dict:
    if lead < 1:
        raise ValueError("lead must be at least one month")
    names = list(methods)
    unknown = [name for name in names if name not in CANDIDATES]
    if unknown:
        raise ValueError(f"unknown holdout methods: {unknown}")
    cells: dict[str, dict[str, list[float]]] = {}
    total_actual = 0.0
    refused_actual = 0.0
    scored_points = 0

    def cell(group: str, name: str) -> list[float]:
        return cells.setdefault(group, {}).setdefault(name, [0.0, 0.0, 0.0, 0])

    for series in series_list:
        length = len(series)
        for target in range(max(1, length - origins), length):
            train = series[: target - lead + 1]
            if not train:
                continue
            actual = float(series[target])
            total_actual += actual
            if sum(1 for value in train if value > 0) < min_non_zero:
                refused_actual += actual
                continue
            segment = classify.classify(train)
            scored_points += 1
            for name in names:
                if name == "service":
                    value = service_forecast(train, segment, scope=scope)
                else:
                    value = CANDIDATES[name](train, segment)
                forecast = max(0.0, float(value))
                for group in (segment, OVERALL):
                    acc = cell(group, name)
                    acc[0] += abs(forecast - actual)
                    acc[1] += forecast - actual
                    acc[2] += actual
                    acc[3] += 1

    by_class: dict[str, dict] = {}
    for group in (*CLASSES, OVERALL):
        if group not in cells:
            continue
        first = next(iter(cells[group].values()))
        by_class[group] = {
            "points": int(first[3]),
            "actual_eur": round(first[2], 2),
            "methods": {
                name: {
                    "wape_pct": _pct(acc[0], acc[2]),
                    "bias_pct": _pct(acc[1], acc[2]),
                }
                for name, acc in cells[group].items()
            },
        }
    return {
        "origins": origins,
        "lead_months": lead,
        "scope": scope,
        "min_non_zero_months": min_non_zero,
        "scored_points": scored_points,
        "total_actual_eur": round(total_actual, 2),
        "refused_actual_eur": round(refused_actual, 2),
        "refused_actual_share_pct": _pct(refused_actual, total_actual),
        "by_class": by_class,
    }


def _pct(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 2)
