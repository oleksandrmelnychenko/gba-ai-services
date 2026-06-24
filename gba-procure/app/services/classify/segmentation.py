"""ABC / XYZ segmentation (pure logic).

ABC by trailing EUR revenue (Pareto: A=top 80% of cumulative revenue, B=next 15%,
C=last 5%). XYZ by demand regularity (coefficient of variation of the monthly demand
series): X=regular, Y=variable, Z=intermittent/erratic. ADI = average demand interval
(days between demand occurrences) flags lumpy intermittent demand.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import mean, pstdev

_ABC_A_CUM = 0.80
_ABC_B_CUM = 0.95
_XYZ_X_CV = 0.5
_XYZ_Y_CV = 1.0


def abc_from_revenue(revenue_by_pid: dict[int, float]) -> dict[int, str]:
    positive = {pid: r for pid, r in revenue_by_pid.items() if r > 0}
    total = sum(positive.values())
    if total <= 0:
        return {pid: "C" for pid in revenue_by_pid}
    out: dict[int, str] = {pid: "C" for pid in revenue_by_pid}
    cum = 0.0
    for pid, rev in sorted(positive.items(), key=lambda kv: kv[1], reverse=True):
        prev_share = cum / total
        if prev_share < _ABC_A_CUM:
            out[pid] = "A"
        elif prev_share < _ABC_B_CUM:
            out[pid] = "B"
        else:
            out[pid] = "C"
        cum += rev
    return out


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _window_months(as_of, history_days: int) -> list[str]:
    end = _as_date(as_of)
    start = end - timedelta(days=history_days)
    months: list[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


def _monthly_units_filled(daily_rows: list[dict], as_of, history_days: int) -> list[float]:
    monthly: dict[str, float] = {k: 0.0 for k in _window_months(as_of, history_days)}
    for r in daily_rows:
        d = _as_date(r["d"])
        key = f"{d.year:04d}-{d.month:02d}"
        monthly[key] = monthly.get(key, 0.0) + float(r["units"] or 0)
    return [monthly[k] for k in sorted(monthly)]


def xyz_from_daily(
    daily_rows: list[dict], as_of, history_days: int
) -> tuple[str, float, float]:
    """Return (xyz_class, cv, adi). cv = std/mean of zero-filled monthly demand."""
    demand_days = sum(1 for r in daily_rows if float(r["units"] or 0) > 0)
    adi = (history_days / demand_days) if demand_days > 0 else float("inf")

    series = _monthly_units_filled(daily_rows, as_of, history_days)
    if len(series) < 2 or mean(series) <= 0:
        return "Z", float("inf"), adi
    cv = pstdev(series) / mean(series)
    if cv <= _XYZ_X_CV:
        xyz = "X"
    elif cv <= _XYZ_Y_CV:
        xyz = "Y"
    else:
        xyz = "Z"
    return xyz, round(cv, 4), round(adi, 2)


def quadrant(abc: str, xyz: str) -> str:
    return f"{abc}{xyz}"
