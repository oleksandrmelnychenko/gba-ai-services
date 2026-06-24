"""Replenishment policy — reorder-point with lead-time-aware safety stock.

Per product for a producer:
  lead_demand   = mean_daily * lead_time_days
  safety_stock  = z(service_level) * sqrt(lead_time) * std_daily      (demand variability over LT)
  reorder_point = lead_demand + safety_stock
  order_up_to   = reorder_point + horizon_demand                       (cover horizon after arrival)
  position      = on_hand - reserved + on_order
  suggested_qty = max(0, order_up_to - position)  when position <= reorder_point else 0

Urgency from days_of_cover vs lead time. Baseline policy now; the math is standard and
parameters (service_level, horizon) are config-driven for tuning on real data later.
"""
from __future__ import annotations

import math
from datetime import date, datetime

from app.core.config import get_settings
from app.data import cost_repository as cost_repo
from app.data import feedback, masters
from app.data import supply_repository as repo
from app.domain.models import (
    CartReplenishmentPlan,
    CheaperAlt,
    DaysOfCoverBucket,
    DemandForecast,
    DemandPoint,
    DemandSeries,
    InventoryPosition,
    PlanCharts,
    ProducerPurchasePlan,
    ReorderSuggestion,
    TopItem,
    Urgency,
    UrgencyMixBucket,
)
from app.services.classify import segmentation
from app.services.classify import service as classify_svc
from app.services.forecasting import demand as demand_svc
from app.services.forecasting import lead_time as lead_time_svc
from app.services.optimization import milp as milp_opt

# Shared urgency ordering (critical first) for sorting and the mix histogram.
_URGENCY_ORDER = {Urgency.CRITICAL: 0, Urgency.HIGH: 1, Urgency.NORMAL: 2, Urgency.NONE: 3}
_URGENCY_WEIGHT = {Urgency.CRITICAL: 1.0, Urgency.HIGH: 0.7, Urgency.NORMAL: 0.4, Urgency.NONE: 0.1}


def _profit_at_risk(sug: ReorderSuggestion) -> float:
    margin = max(sug.unit_margin_eur or 0.0, 0.0)
    return margin * sug.suggested_qty * _URGENCY_WEIGHT[sug.urgency]


def _value_density(sug: ReorderSuggestion) -> float:
    line_cost = sug.line_cost_eur or 0.0
    if line_cost <= 0:
        return 0.0
    return _profit_at_risk(sug) / line_cost

_ACKLAM_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
             1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_ACKLAM_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
             6.680131188771972e01, -1.328068155288572e01)
_ACKLAM_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
             -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_ACKLAM_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
             3.754408661907416e00)


def _inv_norm_cdf(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q
                 + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q
                 + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((_ACKLAM_A[0] * r + _ACKLAM_A[1]) * r + _ACKLAM_A[2]) * r + _ACKLAM_A[3]) * r
                 + _ACKLAM_A[4]) * r + _ACKLAM_A[5]) * q / (((((_ACKLAM_B[0] * r + _ACKLAM_B[1]) * r
                 + _ACKLAM_B[2]) * r + _ACKLAM_B[3]) * r + _ACKLAM_B[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q
              + _ACKLAM_C[4]) * q + _ACKLAM_C[5]) / ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q
              + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1)


def _z_for(service_level: float) -> float:
    return _inv_norm_cdf(service_level)


def _urgency(position: float, safety_stock: float, reorder_point: float,
             order_up_to: float) -> Urgency:
    if position <= max(safety_stock, 0.0):
        return Urgency.CRITICAL
    if position <= reorder_point:
        return Urgency.HIGH
    if position <= order_up_to:
        return Urgency.NORMAL
    return Urgency.NONE


def _suggest_one(
    product_id: int, producer_id: int, forecast: DemandForecast,
    inv: InventoryPosition, lead_time_days: float, lead_time_std_days: float = 0.0,
    z: float = 1.6449, horizon: int = 30,
) -> ReorderSuggestion:
    lead_demand = forecast.mean_daily * lead_time_days
    lt = max(lead_time_days, 0.0)
    safety_stock = z * math.sqrt(
        lt * forecast.std_daily ** 2 + forecast.mean_daily ** 2 * lead_time_std_days ** 2
    )
    reorder_point = lead_demand + safety_stock
    order_up_to = reorder_point + forecast.mean_daily * horizon

    days_of_cover = (inv.position / forecast.mean_daily) if forecast.mean_daily > 0 else float("inf")
    if days_of_cover != float("inf"):
        days_of_cover = max(0.0, days_of_cover)
    needs = inv.position <= reorder_point
    suggested_qty = max(0.0, order_up_to - inv.position) if needs else 0.0

    urgency = _urgency(inv.position, safety_stock, reorder_point, order_up_to)
    reason = (
        f"position {inv.position:.0f} vs reorder_point {reorder_point:.0f}; "
        f"{days_of_cover:.0f}d cover, lead {lead_time_days:.0f}d"
        if needs else f"sufficient cover ({days_of_cover:.0f}d)"
    )

    return ReorderSuggestion(
        product_id=product_id, producer_id=producer_id,
        suggested_qty=round(suggested_qty, 2),
        reorder_point=round(reorder_point, 2),
        safety_stock=round(safety_stock, 2),
        days_of_cover=round(days_of_cover, 1) if days_of_cover != float("inf") else 99999.0,
        urgency=urgency, forecast=forecast, inventory=inv, reason=reason,
    )


def _abc_floor(abc: str | None, s) -> float:
    if abc == "A":
        return s.service_level_floor_a
    if abc == "B":
        return s.service_level_floor_b
    return s.service_level_min


def _economic_service_level(sale_eur: float, cost_eur: float, lead_time_days: float,
                            horizon: int, s, abc: str | None = None) -> float:
    floor = _abc_floor(abc, s)
    margin = sale_eur - cost_eur
    if margin < s.min_margin_eur:
        return min(floor, s.service_level_max)
    holding = (s.holding_rate_annual / 365.0) * cost_eur * horizon
    sl = margin / (margin + holding)
    return min(max(sl, floor), s.service_level_max)


def _round_order_qty(qty: float, moq: float | None, multiple: float | None) -> float:
    if qty <= 0:
        return 0.0
    q = max(qty, moq) if moq and moq > 0 else qty
    if multiple and multiple > 1:
        q = math.ceil(q / multiple) * multiple
    return float(q)


def _attach_cheaper_alt(producer_id: int, items: list[ReorderSuggestion], as_of: str) -> None:
    if not items:
        return
    alts = cost_repo.cheapest_alt_eur([it.product_id for it in items], as_of)
    for it in items:
        alt = alts.get(it.product_id)
        uc = it.unit_cost_eur
        if alt and alt["producer_id"] != producer_id and (uc is None or alt["cost_eur"] < uc * 0.98):
            it.cheaper_alt = CheaperAlt(producer_id=alt["producer_id"], cost_eur=alt["cost_eur"])


_UNSET = object()


def build_plan(producer_id: int, as_of: str, only_needed: bool = True,
               abc_map: dict[int, str] | None = None,
               producer_name=_UNSET) -> ProducerPurchasePlan:
    s = get_settings()
    lt_mean, lt_std, lt_source = lead_time_svc.producer_lead_time(producer_id, as_of)
    product_ids = repo.products_for_producer(producer_id, as_of, s.history_days)

    profile = masters.producer_profile(producer_id) if s.use_masters else None
    prod_sl_floor: float | None = None
    if profile:
        override = profile.get("lead_time_override_days")
        if override:
            lt_mean = float(override)
            lt_std = lt_mean * s.lead_time_cv
            lt_source = "override"
        if profile.get("service_level_target"):
            prod_sl_floor = float(profile["service_level_target"])
    terms = masters.product_terms_for(producer_id, product_ids) if s.use_masters else {}
    factors = (feedback.learned_factors(
        producer_id, s.feedback_min_samples, s.override_factor_min, s.override_factor_max)
        if s.use_feedback else {})
    _season_month = _next_month(_as_date(as_of)).month

    on_hand = repo.on_hand(product_ids)
    reserved = repo.reserved(product_ids)
    on_order = repo.on_order(product_ids, as_of)
    # ONE (chunked) demand query for ALL candidate products instead of one per product.
    demand_rows = repo.product_daily_demand_bulk(product_ids, as_of, s.history_days)
    costs = cost_repo.producer_unit_costs_eur(producer_id, product_ids, as_of)
    sales = cost_repo.sale_prices_eur(product_ids, as_of, s.history_days)
    if abc_map is None:
        abc_map = classify_svc.get_abc_map(as_of, s.history_days)

    items: list[ReorderSuggestion] = []
    for pid in product_ids:
        oh = on_hand.get(pid, 0.0)
        rs = reserved.get(pid, 0.0)
        oo = on_order.get(pid, 0.0)
        inv = InventoryPosition(
            product_id=pid, on_hand=oh, reserved=rs, on_order=oo,
            available=oh - rs, position=oh - rs + oo,
        )
        dr = demand_rows.get(pid, [])
        abc = abc_map.get(pid, "C")
        xyz, _cv, _adi = segmentation.xyz_from_daily(dr, as_of, s.history_days)
        method = demand_svc.method_for_xyz(xyz) if s.per_quadrant_forecast else None
        forecast = demand_svc.forecast_from_rows(pid, dr, s.forecast_horizon_days, method=method)
        season_factor = 1.0
        if s.seasonality_enabled:
            season_factor = demand_svc.seasonal_index_for(
                dr, _season_month, s.seasonal_shrinkage_k, s.seasonal_min,
                s.seasonal_max, s.seasonal_min_months)
            if season_factor != 1.0:
                forecast = forecast.model_copy(update={
                    "mean_daily": forecast.mean_daily * season_factor,
                    "forecast_units": forecast.forecast_units * season_factor})
        cost = costs.get(pid)
        sale = sales.get(pid)
        if s.economic_service_level and cost is not None and cost > 0 and sale is not None:
            sl = _economic_service_level(sale, cost, lt_mean, s.forecast_horizon_days, s, abc)
        else:
            sl = _abc_floor(abc, s)
        if prod_sl_floor is not None:
            sl = min(max(sl, prod_sl_floor), s.service_level_max)
        z_item = _z_for(sl)
        sug = _suggest_one(
            pid, producer_id, forecast, inv, lt_mean, lt_std, z_item, s.forecast_horizon_days
        )
        if only_needed and sug.suggested_qty <= 0:
            continue
        original_qty = sug.suggested_qty
        factor = factors.get(abc)
        if factor is not None and factor != 1.0:
            sug.learned_factor = round(factor, 3)
            sug.suggested_qty = round(sug.suggested_qty * factor, 2)
        t = terms.get(pid)
        if t:
            moq = t.get("moq")
            mult = t.get("order_multiple")
            sug.suggested_qty = round(_round_order_qty(sug.suggested_qty, moq, mult), 2)
            sug.moq = float(moq) if moq else None
            sug.order_multiple = float(mult) if mult else None
        if sug.suggested_qty != original_qty:
            sug.raw_qty = round(original_qty, 2)
        sug.applied_service_level = round(sl, 4)
        sug.abc = abc
        sug.xyz = xyz
        sug.quadrant = segmentation.quadrant(abc, xyz)
        if season_factor != 1.0:
            sug.seasonal_factor = round(season_factor, 3)
        if cost is not None:
            sug.unit_cost_eur = cost
            sug.line_cost_eur = round(cost * sug.suggested_qty, 2)
        if sale is not None:
            sug.unit_sale_eur = sale
            if cost is not None:
                sug.unit_margin_eur = round(sale - cost, 4)
        items.append(sug)

    _attach_cheaper_alt(producer_id, items, as_of)

    # urgency-first ordering
    items.sort(key=lambda x: (_URGENCY_ORDER[x.urgency], -x.suggested_qty))

    resolved_name = repo.producer_name(producer_id) if producer_name is _UNSET else producer_name
    return ProducerPurchasePlan(
        producer_id=producer_id,
        producer_name=resolved_name,
        lead_time_days=round(lt_mean, 1),
        lead_time_std_days=round(lt_std, 1),
        lead_time_source=lt_source,
        items=items,
        item_count=len(items),
        as_of_date=as_of,
    )


def build_cart_plan(as_of: str, only_needed: bool = True, limit: int = 200,
                    budget_eur: float | None = None,
                    method: str = "greedy",
                    active_days: int | None = None) -> CartReplenishmentPlan:
    s = get_settings()
    producer_ids = repo.all_producers(as_of, active_days or s.history_days)
    abc_map = classify_svc.get_abc_map(as_of, s.history_days)
    names = repo.producer_names(producer_ids)

    items: list[ReorderSuggestion] = []
    for producer_id in producer_ids:
        plan = build_plan(producer_id, as_of, only_needed=only_needed, abc_map=abc_map,
                          producer_name=names.get(producer_id))
        items.extend(sug for sug in plan.items if sug.suggested_qty > 0)

    if budget_eur is not None and budget_eur > 0:
        for it in items:
            it.value_density = round(_value_density(it), 4)
        values = [_profit_at_risk(it) for it in items]
        costs = [it.line_cost_eur or 0.0 for it in items]
        if method == "milp":
            chosen = milp_opt.select_within_budget(values, costs, budget_eur, s.milp_time_limit)
        else:
            chosen = set()
            used_g = 0.0
            for i in sorted(range(len(items)), key=lambda i: -(items[i].value_density or 0.0)):
                if costs[i] > 0 and used_g + costs[i] <= budget_eur:
                    chosen.add(i)
                    used_g += costs[i]
        used = value = 0.0
        for i, it in enumerate(items):
            it.within_budget = i in chosen
            if it.within_budget:
                used += costs[i]
                value += values[i]
        items.sort(key=lambda x: (not x.within_budget, -(x.value_density or 0.0)))
        return CartReplenishmentPlan(
            items=items, item_count=len(items), as_of_date=as_of,
            budget_eur=budget_eur, budget_used_eur=round(used, 2),
            value_captured_eur=round(value, 2),
            selected_count=len(chosen), deferred_count=len(items) - len(chosen),
        )

    items.sort(key=lambda x: (_URGENCY_ORDER[x.urgency], x.days_of_cover))
    if limit is not None and limit >= 0:
        items = items[:limit]

    return CartReplenishmentPlan(
        items=items,
        item_count=len(items),
        as_of_date=as_of,
    )


# --- dashboard chart data ---------------------------------------------------
# All derived from build_plan / build_cart_plan outputs + the SAME batched demand
# fetch (product_daily_demand_bulk). No forecast/policy math is touched here.

# How many of the top items get a demand_series (history + forecast). "Top few."
_DEMAND_SERIES_MAX = 5
# days_of_cover histogram edges, inclusive upper bound per bucket.
_COVER_BUCKETS = ("<0", "0-7", "8-30", "31-90", "90+")


def _cover_bucket(days: float) -> str:
    if days < 0:
        return "<0"
    if days <= 7:
        return "0-7"
    if days <= 30:
        return "8-30"
    if days <= 90:
        return "31-90"
    return "90+"


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _as_date(value: str | date) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _demand_series_for(
    product_ids: list[int],
    forecasts: dict[int, DemandForecast],
    as_of: str,
    history_days: int,
) -> list[DemandSeries]:
    """Monthly historical demand (from the batched bulk fetch) + a flagged forecast point.

    Reuses repo.product_daily_demand_bulk (same source/filters/traps as the plan) for ONLY
    the handful of top products, rolls the daily series up to 'yyyy-MM', then appends the
    forecast horizon as one is_forecast=true point (mean_daily * horizon = forecast_units).
    """
    if not product_ids:
        return []
    bulk = repo.product_daily_demand_bulk(product_ids, as_of, history_days)
    as_of_date = _as_date(as_of)
    forecast_period = _month_key(_next_month(as_of_date))

    series: list[DemandSeries] = []
    for pid in product_ids:
        monthly: dict[str, float] = {}
        for r in bulk.get(pid, []):
            monthly[_month_key(_as_date(r["d"]))] = monthly.get(_month_key(_as_date(r["d"])), 0.0) + float(
                r["units"] or 0
            )
        points = [
            DemandPoint(period=m, units=round(monthly[m], 2), is_forecast=False)
            for m in sorted(monthly)
        ]
        fc = forecasts.get(pid)
        forecast_units = float(fc.forecast_units) if fc else 0.0
        points.append(
            DemandPoint(period=forecast_period, units=round(forecast_units, 2), is_forecast=True)
        )
        series.append(DemandSeries(product_id=pid, points=points))
    return series


def build_charts(
    producer_id: int | None, as_of: str, top_n: int = 15
) -> PlanCharts:
    """Procurement-dashboard chart data for one producer (or the whole cart).

    Producer-scoped: build_plan(only_needed=False) so the urgency mix / cover histogram show
    the FULL distribution (including sufficiently-covered items). Cart-wide (producer_id=None):
    reuse build_cart_plan (needed-only across all producers). demand_series covers the top few.
    """
    s = get_settings()
    top_n = max(0, int(top_n))

    if producer_id is not None:
        plan = build_plan(producer_id, as_of, only_needed=False)
        items = plan.items
    else:
        items = build_cart_plan(as_of, only_needed=True, limit=-1).items

    # urgency_mix — count per urgency, critical-first, every bucket present.
    counts = {u: 0 for u in Urgency}
    for it in items:
        counts[it.urgency] += 1
    urgency_mix = [
        UrgencyMixBucket(urgency=u, count=counts[u])
        for u in sorted(counts, key=lambda u: _URGENCY_ORDER[u])
    ]

    # days_of_cover_hist — fixed buckets, every bucket present (count 0 if empty).
    hist = {b: 0 for b in _COVER_BUCKETS}
    for it in items:
        hist[_cover_bucket(it.days_of_cover)] += 1
    days_of_cover_hist = [DaysOfCoverBucket(bucket=b, count=hist[b]) for b in _COVER_BUCKETS]

    ranked_all = sorted(items, key=lambda x: (_URGENCY_ORDER[x.urgency], -x.suggested_qty))
    ranked: list[ReorderSuggestion] = []
    _seen_pids: set[int] = set()
    for it in ranked_all:
        if it.product_id in _seen_pids:
            continue
        _seen_pids.add(it.product_id)
        ranked.append(it)
    top_items = [
        TopItem(
            product_id=it.product_id,
            suggested_qty=it.suggested_qty,
            on_hand=it.inventory.on_hand,
            reorder_point=it.reorder_point,
            urgency=it.urgency,
        )
        for it in ranked[:top_n]
    ]

    # demand_series — history + flagged forecast for the top few products.
    series_n = min(top_n, _DEMAND_SERIES_MAX)
    series_ids = [it.product_id for it in ranked[:series_n]]
    forecasts = {it.product_id: it.forecast for it in ranked[:series_n]}
    demand_series = _demand_series_for(series_ids, forecasts, as_of, s.history_days)

    return PlanCharts(
        producer_id=producer_id,
        as_of_date=as_of,
        top_n=top_n,
        urgency_mix=urgency_mix,
        days_of_cover_hist=days_of_cover_hist,
        top_items=top_items,
        demand_series=demand_series,
    )
