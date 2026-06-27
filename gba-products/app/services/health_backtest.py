"""Pure evaluation helpers for the product health composite.

The service can keep using the 0-100 health score as a triage signal, but this
module makes its calibration measurable: a snapshot at T is compared with
future sales / margin outcomes over T..T+H.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _rank(values: list[float]) -> list[float]:
    """Average ranks for ties, 1-based. Small, dependency-free Spearman helper."""
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    sx = math.sqrt(sum(x * x for x in dx))
    sy = math.sqrt(sum(y * y for y in dy))
    if sx == 0.0 or sy == 0.0:
        return None
    return sum(x * y for x, y in zip(dx, dy, strict=True)) / (sx * sy)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rho without scipy; returns None for too-small or constant samples."""
    return _pearson(_rank(xs), _rank(ys))


def _quantile_buckets(rows: list[dict[str, Any]], buckets: int) -> list[list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda r: (_as_float(r["health"]), int(r["product_id"])))
    n = len(ordered)
    if n == 0:
        return []
    out: list[list[dict[str, Any]]] = []
    for b in range(buckets):
        start = (b * n) // buckets
        end = ((b + 1) * n) // buckets
        out.append(ordered[start:end])
    return out


def _bucket_summary(bucket: list[dict[str, Any]], index: int) -> dict[str, Any]:
    revenues = [_as_float(r.get("future_revenue_eur")) for r in bucket]
    margins = [
        _as_float(r["future_margin_eur"])
        for r in bucket
        if r.get("future_margin_eur") is not None
    ]
    sold_units = [_as_float(r.get("future_units")) for r in bucket]
    returned_units = [_as_float(r.get("future_returned_units")) for r in bucket]
    return {
        "bucket": index,
        "n": len(bucket),
        "avg_health": _round(_mean([_as_float(r.get("health")) for r in bucket])),
        "avg_future_revenue_eur": _round(_mean(revenues), 2),
        "total_future_revenue_eur": _round(sum(revenues), 2),
        "avg_future_margin_eur": _round(_mean(margins), 2),
        "future_units": _round(sum(sold_units), 2),
        "future_return_rate": _round(sum(returned_units) / sum(sold_units), 4)
        if sum(sold_units) > 0 else None,
    }


def _group_summary(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get(key) or "unknown"), []).append(row)
    out = []
    for name, group in sorted(groups.items()):
        revenues = [_as_float(r.get("future_revenue_eur")) for r in group]
        units = [_as_float(r.get("future_units")) for r in group]
        margins = [
            _as_float(r["future_margin_eur"])
            for r in group
            if r.get("future_margin_eur") is not None
        ]
        zero_revenue = sum(1 for r in group if _as_float(r.get("future_revenue_eur")) == 0.0)
        out.append({
            key: name,
            "n": len(group),
            "avg_health": _round(_mean([_as_float(r.get("health")) for r in group])),
            "avg_demand_score": _round(_mean([_as_float(r.get("demand_score")) for r in group])),
            "avg_margin_score": _round(_mean([_as_float(r.get("margin_score")) for r in group])),
            "avg_future_revenue_eur": _round(_mean(revenues), 2),
            "avg_future_units": _round(_mean(units), 2),
            "avg_future_margin_eur": _round(_mean(margins), 2),
            "future_zero_revenue_share": _round(zero_revenue / len(group), 4),
        })
    return out


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _lift(high: list[dict[str, Any]], low: list[dict[str, Any]], key: str) -> float | None:
    hi = _mean([_as_float(r.get(key)) for r in high])
    lo = _mean([_as_float(r.get(key)) for r in low])
    if hi is None or lo is None or lo == 0.0:
        return None
    return hi / lo


def evaluate_health_snapshot(
    snapshot_rows: Iterable[dict[str, Any]],
    outcomes_by_product: dict[int, dict[str, Any]],
    *,
    buckets: int = 10,
    tail_fraction: float = 0.2,
) -> dict[str, Any]:
    """Return calibration diagnostics for product health vs future outcomes.

    `snapshot_rows` are rows from `portfolio.build_portfolio(as_of)["rows"]`.
    `outcomes_by_product` maps product_id to future outcome fields:
    future_units, future_revenue_eur, future_returned_units and optionally
    future_margin_eur.
    """
    joined: list[dict[str, Any]] = []
    for row in snapshot_rows:
        pid = int(row["product_id"])
        outcome = outcomes_by_product.get(pid, {})
        joined.append({
            "product_id": pid,
            "health": _as_float(row.get("health")),
            "demand_score": _as_float(row.get("demand_score")),
            "margin_score": _as_float(row.get("margin_score")),
            "action_label": row.get("action_label"),
            "band": row.get("band"),
            "lifecycle": row.get("lifecycle"),
            "abc": row.get("abc"),
            "future_units": _as_float(outcome.get("future_units")),
            "future_revenue_eur": _as_float(outcome.get("future_revenue_eur")),
            "future_returned_units": _as_float(outcome.get("future_returned_units")),
            "future_margin_eur": outcome.get("future_margin_eur"),
        })

    if not joined:
        return {
            "n": 0,
            "correlations": {},
            "deciles_low_to_high_health": [],
            "top_bottom_lift": {},
            "notes": ["empty snapshot"],
        }

    health = [_as_float(r["health"]) for r in joined]
    revenue = [_as_float(r["future_revenue_eur"]) for r in joined]
    units = [_as_float(r["future_units"]) for r in joined]
    margin_rows = [r for r in joined if r.get("future_margin_eur") is not None]
    margin_health = [_as_float(r["health"]) for r in margin_rows]
    margins = [_as_float(r["future_margin_eur"]) for r in margin_rows]
    demand_score_values = [_as_float(r["demand_score"]) for r in joined]
    margin_score_values = [_as_float(r["margin_score"]) for r in margin_rows]

    bucket_rows = _quantile_buckets(joined, buckets)
    deciles = [_bucket_summary(bucket, i + 1) for i, bucket in enumerate(bucket_rows)]

    ordered = sorted(joined, key=lambda r: (_as_float(r["health"]), int(r["product_id"])))
    tail_n = max(1, int(len(ordered) * tail_fraction))
    low = ordered[:tail_n]
    high = ordered[-tail_n:]

    zero_revenue = sum(1 for r in joined if _as_float(r.get("future_revenue_eur")) == 0.0)
    return {
        "n": len(joined),
        "n_with_future_revenue": len(joined) - zero_revenue,
        "n_with_future_margin": len(margin_rows),
        "health_min": _round(min(health), 2),
        "health_p50": _round(sorted(health)[len(health) // 2], 2),
        "health_max": _round(max(health), 2),
        "future_zero_revenue_share": _round(zero_revenue / len(joined), 4),
        "correlations": {
            "spearman_health_to_future_revenue": _round(spearman(health, revenue), 4),
            "spearman_health_to_future_units": _round(spearman(health, units), 4),
            "spearman_health_to_future_margin": _round(spearman(margin_health, margins), 4),
            "spearman_demand_score_to_future_revenue": _round(spearman(demand_score_values, revenue), 4),
            "spearman_demand_score_to_future_units": _round(spearman(demand_score_values, units), 4),
            "spearman_margin_score_to_future_margin": _round(spearman(margin_score_values, margins), 4),
        },
        "deciles_low_to_high_health": deciles,
        "by_action": _group_summary(joined, "action_label"),
        "top_bottom_lift": {
            "tail_fraction": tail_fraction,
            "future_revenue_avg_top_vs_bottom": _round(_lift(high, low, "future_revenue_eur"), 4),
            "future_units_avg_top_vs_bottom": _round(_lift(high, low, "future_units"), 4),
            "future_margin_avg_top_vs_bottom": _round(_lift(high, low, "future_margin_eur"), 4),
        },
        "notes": [
            "health is treated as a triage composite until these diagnostics show stable lift",
            "future_margin_eur is only available when snapshot unit cost is known",
        ],
    }
