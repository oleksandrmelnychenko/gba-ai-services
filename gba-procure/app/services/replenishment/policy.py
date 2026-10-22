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
from decimal import Decimal, localcontext
from fractions import Fraction

from app.core.config import get_settings
from app.core.history import rolling_coverage
from app.data import cost_repository as cost_repo
from app.data import feedback, masters
from app.data import supply_repository as repo
from app.data.purchase_cost_cohort import (
    NET_GOODS_BASIS,
    PurchaseCostCohort,
    buyer_provenance,
    exact_line_amount,
    read_purchase_cost_cohort,
)
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
    ChartItemEvidence,
    ChartPopulation,
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
_CENT = Decimal("0.01")
_FOUR_DECIMALS = Decimal("0.0001")


def _decimal(value: float | int | str | Decimal | Fraction) -> Decimal:
    if isinstance(value, Fraction):
        # All callers sum decimal inputs, hence the denominator contains only
        # factors2 and5. Retain every digit before the explicit output rounding.
        with localcontext() as ctx:
            ctx.prec = max(28, len(str(abs(value.numerator))) + value.denominator.bit_length())
            result = Decimal(value.numerator) / Decimal(value.denominator)
        if Fraction(result) != value:
            raise ValueError("quantity or amount has unsupported non-decimal precision")
        return result
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _round_decimal(value: float | int | str | Decimal, quantum: Decimal) -> float:
    exact = Fraction(_decimal(value)) / Fraction(quantum)
    whole, remainder = divmod(abs(exact.numerator), exact.denominator)
    rounded = (whole + (2 * remainder >= exact.denominator)) * (1 if exact >= 0 else -1)
    return _exact_quantity_float(_decimal(Fraction(rounded) * Fraction(quantum)))


def _line_cost(unit_cost: float, qty: float) -> float | None:
    """Round supplier line totals like accounting money, never with binary float ties."""
    return exact_line_amount(_decimal(unit_cost), qty)[1]


def _resolved_unit_cost(
    factual_cost: float | None,
    terms: dict | None,
) -> float | None:
    override = terms.get("unit_cost_override") if terms else None
    if override is not None and float(override) > 0:
        return float(override)
    return factual_cost


def _profit_at_risk(sug: ReorderSuggestion) -> float:
    margin = max(sug.unit_margin_eur or 0.0, 0.0)
    return margin * sug.suggested_qty * _URGENCY_WEIGHT[sug.urgency]


def _value_density(sug: ReorderSuggestion) -> float:
    line_cost = sug.line_cost_eur or 0.0
    if line_cost <= 0:
        return 0.0
    return _profit_at_risk(sug) / line_cost


def _supplier_option_key(sug: ReorderSuggestion) -> tuple:
    """Choose one deterministic, orderable supplier option for each product.

    A cart is a product order, not a list of every historical producer-product
    relationship. Prefer an option with a factual price, then the lowest EUR unit
    cost. Stable operational tie-breakers make repeat builds byte-for-byte consistent.
    """
    return (
        not sug.cost_provenance.budget_eligible,
        sug.unit_cost_eur is None,
        _decimal(sug.unit_cost_eur) if sug.unit_cost_eur is not None else Decimal("Infinity"),
        _URGENCY_ORDER[sug.urgency],
        sug.days_of_cover,
        sug.producer_id,
    )


def _deduplicate_supplier_options(
    items: list[ReorderSuggestion],
) -> tuple[list[ReorderSuggestion], int]:
    by_product: dict[int, ReorderSuggestion] = {}
    for item in items:
        current = by_product.get(item.product_id)
        if current is None or _supplier_option_key(item) < _supplier_option_key(current):
            by_product[item.product_id] = item
    return list(by_product.values()), len(items) - len(by_product)


def _cart_totals(items: list[ReorderSuggestion]) -> dict:
    total_qty = sum((Fraction(_decimal(item.suggested_qty)) for item in items), Fraction())
    priced_cost = sum(
        (Fraction(_decimal(item.line_cost_eur)) for item in items if item.line_cost_eur is not None),
        Fraction(),
    )
    unpriced = sum(1 for item in items if item.line_cost_eur is None)
    priced_cost_eur = _round_decimal(priced_cost, _CENT)
    return {
        "total_suggested_qty": _exact_quantity_float(_decimal(total_qty)),
        "priced_cost_eur": priced_cost_eur,
        "total_cost_eur": None if unpriced else priced_cost_eur,
        "unpriced_item_count": unpriced,
    }


def _cost_total_metadata(items: list[ReorderSuggestion]) -> dict:
    return {
        "cost_total_basis": (
            "includes_buyer_values_with_unverified_tax_basis"
            if any(item.cost_provenance.source == "buyer_supplied" for item in items)
            else NET_GOODS_BASIS
        ),
        "cost_totals_certified": all(item.cost_provenance.budget_eligible for item in items),
    }


_ACKLAM_A = (
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
)
_ACKLAM_B = (
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
)
_ACKLAM_C = (
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
)
_ACKLAM_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00)


def _inv_norm_cdf(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (
            ((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q + _ACKLAM_C[4])
            * q
            + _ACKLAM_C[5]
        ) / ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (
            (
                (
                    (((_ACKLAM_A[0] * r + _ACKLAM_A[1]) * r + _ACKLAM_A[2]) * r + _ACKLAM_A[3]) * r
                    + _ACKLAM_A[4]
                )
                * r
                + _ACKLAM_A[5]
            )
            * q
            / (
                (
                    (((_ACKLAM_B[0] * r + _ACKLAM_B[1]) * r + _ACKLAM_B[2]) * r + _ACKLAM_B[3]) * r
                    + _ACKLAM_B[4]
                )
                * r
                + 1
            )
        )
    q = math.sqrt(-2 * math.log(1 - p))
    return -(
        ((((_ACKLAM_C[0] * q + _ACKLAM_C[1]) * q + _ACKLAM_C[2]) * q + _ACKLAM_C[3]) * q + _ACKLAM_C[4]) * q
        + _ACKLAM_C[5]
    ) / ((((_ACKLAM_D[0] * q + _ACKLAM_D[1]) * q + _ACKLAM_D[2]) * q + _ACKLAM_D[3]) * q + 1)


def _z_for(service_level: float) -> float:
    return _inv_norm_cdf(service_level)


def _urgency(position: float, safety_stock: float, reorder_point: float, order_up_to: float) -> Urgency:
    if position <= max(safety_stock, 0.0):
        return Urgency.CRITICAL
    if position <= reorder_point:
        return Urgency.HIGH
    if position <= order_up_to:
        return Urgency.NORMAL
    return Urgency.NONE


def _suggest_one(
    product_id: int,
    producer_id: int,
    forecast: DemandForecast,
    inv: InventoryPosition,
    lead_time_days: float,
    lead_time_std_days: float = 0.0,
    z: float = 1.6449,
    horizon: int = 30,
) -> ReorderSuggestion:
    lead_demand = forecast.mean_daily * lead_time_days
    lt = max(lead_time_days, 0.0)
    safety_stock = z * math.sqrt(lt * forecast.std_daily**2 + forecast.mean_daily**2 * lead_time_std_days**2)
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
        if needs
        else f"sufficient cover ({days_of_cover:.0f}d)"
    )

    return ReorderSuggestion(
        product_id=product_id,
        producer_id=producer_id,
        suggested_qty=round(suggested_qty, 2),
        reorder_point=round(reorder_point, 2),
        safety_stock=round(safety_stock, 2),
        lead_demand=round(lead_demand, 2),
        order_up_to=round(order_up_to, 2),
        days_of_cover=round(days_of_cover, 1) if days_of_cover != float("inf") else 99999.0,
        urgency=urgency,
        forecast=forecast,
        inventory=inv,
        reason=reason,
    )


def _abc_floor(abc: str | None, s) -> float:
    if abc == "A":
        return s.service_level_floor_a
    if abc == "B":
        return s.service_level_floor_b
    return s.service_level_min


def _economic_service_level(
    sale_eur: float, cost_eur: float, lead_time_days: float, horizon: int, s, abc: str | None = None
) -> float:
    floor = _abc_floor(abc, s)
    margin = sale_eur - cost_eur
    if margin < s.min_margin_eur:
        return min(floor, s.service_level_max)
    holding = (s.holding_rate_annual / 365.0) * cost_eur * horizon
    sl = margin / (margin + holding)
    return min(max(sl, floor), s.service_level_max)


def _exact_quantity_float(quantity: Decimal) -> float:
    result = float(quantity)
    if not math.isfinite(result) or _decimal(result) != quantity:
        raise ValueError("order quantity exceeds the supported exact numeric range")
    return result


def _round_order_qty(qty: float, moq: float | None, multiple: float | None) -> float:
    if qty <= 0:
        return 0.0
    q = max(_decimal(qty), _decimal(moq)) if moq and moq > 0 else _decimal(qty)
    if multiple and multiple > 0:
        pack = _decimal(multiple)
        # Exact rational ceiling: decimal division can round a value just above
        # an integer down before ceil, especially for a small fractional pack.
        q_num, q_den = q.as_integer_ratio()
        p_num, p_den = pack.as_integer_ratio()
        count = -(-(q_num * p_den) // (q_den * p_num))
        with localcontext() as ctx:
            ctx.prec = max(28, len(pack.as_tuple().digits) + len(str(count)))
            q = pack * count
    return _exact_quantity_float(q)


def _attach_cheaper_alt(
    producer_id: int, items: list[ReorderSuggestion], as_of: str, cohort: PurchaseCostCohort
) -> None:
    if not items:
        return
    alts = cost_repo.cheapest_alt_eur([it.product_id for it in items], as_of, cohort=cohort)
    for it in items:
        alt = alts.get(it.product_id)
        if (
            not alt
            or alt["producer_id"] == producer_id
            or it.unit_cost_eur is None
            or it.cost_provenance.source != "receipt_history"
            or it.cost_provenance.canonical_unit_cost_eur is None
        ):
            continue
        base = Fraction(Decimal(it.cost_provenance.canonical_unit_cost_eur))
        alternative = cohort.group(it.product_id, alt["producer_id"])
        if alternative.median_float is None:
            continue
        amount = Fraction(alternative.median_unit_net_cost_eur)
        if amount < base and amount * 50 <= base * 49:
            it.cheaper_alt = CheaperAlt(
                producer_id=alt["producer_id"],
                cost_eur=alt["cost_eur"],
                cost_provenance=cohort.provenance(it.product_id, alt["producer_id"], it.suggested_qty),
            )


_UNSET = object()


def build_plan(
    producer_id: int,
    as_of: str,
    only_needed: bool = True,
    abc_map: dict[int, str] | None = None,
    producer_name=_UNSET,
    *,
    cost_cohort: PurchaseCostCohort | None = None,
    product_ids: list[int] | None = None,
) -> ProducerPurchasePlan:
    s = get_settings()
    coverage = rolling_coverage(as_of, s.history_days)
    lt_mean, lt_std, lt_source = lead_time_svc.producer_lead_time(producer_id, as_of)
    if product_ids is None:
        product_ids = repo.products_for_producer(producer_id, as_of, s.history_days)
    cost_cohort = cost_cohort or read_purchase_cost_cohort(product_ids, as_of)
    cost_cohort.require_scope(product_ids, as_of)

    profile = masters.producer_profile(producer_id, required=True) if s.use_masters else None
    prod_sl_floor: float | None = None
    if profile:
        override = profile.get("lead_time_override_days")
        if override:
            lt_mean = float(override)
            lt_std = lt_mean * s.lead_time_cv
            lt_source = "override"
        if profile.get("service_level_target"):
            prod_sl_floor = float(profile["service_level_target"])
    terms = masters.product_terms_for(producer_id, product_ids, required=True) if s.use_masters else {}
    factors = (
        feedback.learned_factors(
            producer_id, s.feedback_min_samples, s.override_factor_min, s.override_factor_max
        )
        if s.use_feedback
        else {}
    )
    _season_month = _next_month(_as_date(as_of)).month

    on_hand = repo.on_hand(product_ids)
    reserved = repo.reserved(product_ids)
    on_order = repo.on_order(product_ids, as_of)
    # ONE (chunked) demand query for ALL candidate products instead of one per product.
    demand_rows = repo.product_daily_demand_bulk(product_ids, as_of, s.history_days)
    costs = cost_repo.producer_unit_costs_eur(producer_id, product_ids, as_of, cohort=cost_cohort)
    sales = cost_repo.sale_prices_eur(product_ids, as_of, s.history_days)
    if abc_map is None:
        abc_map = classify_svc.get_abc_map(as_of, s.history_days)

    items: list[ReorderSuggestion] = []
    for pid in product_ids:
        oh = on_hand.get(pid, 0.0)
        rs = reserved.get(pid, 0.0)
        oo = on_order.get(pid, 0.0)
        inv = InventoryPosition(
            product_id=pid,
            on_hand=oh,
            reserved=rs,
            on_order=oo,
            available=oh - rs,
            position=oh - rs + oo,
        )
        dr = demand_rows.get(pid, [])
        abc = abc_map.get(pid, "C")
        xyz, _cv, _adi = segmentation.xyz_from_daily(
            dr,
            as_of,
            coverage.effective_history_days,
        )
        method = demand_svc.method_for_xyz(xyz) if s.per_quadrant_forecast else None
        forecast = demand_svc.forecast_from_rows(
            pid,
            dr,
            s.forecast_horizon_days,
            method=method,
            effective_history_days=coverage.effective_history_days,
            effective_start=coverage.effective_start,
        )
        season_factor = 1.0
        if s.seasonality_enabled:
            season_factor = demand_svc.seasonal_index_for(
                dr,
                _season_month,
                s.seasonal_shrinkage_k,
                s.seasonal_min,
                s.seasonal_max,
                s.seasonal_min_months,
            )
            if season_factor != 1.0:
                forecast = forecast.model_copy(
                    update={
                        "mean_daily": forecast.mean_daily * season_factor,
                        "forecast_units": forecast.forecast_units * season_factor,
                    }
                )
        t = terms.get(pid)
        cost = _resolved_unit_cost(costs.get(pid), t)
        sale = sales.get(pid)
        # Recorded sale tax basis is unproved; use the declared existing ABC fallback.
        sl = _abc_floor(abc, s)
        if prod_sl_floor is not None:
            sl = min(max(sl, prod_sl_floor), s.service_level_max)
        z_item = _z_for(sl)
        sug = _suggest_one(pid, producer_id, forecast, inv, lt_mean, lt_std, z_item, s.forecast_horizon_days)
        if only_needed and sug.suggested_qty <= 0:
            continue
        original_qty = sug.suggested_qty
        factor = factors.get(abc)
        if factor is not None and factor != 1.0:
            sug.learned_factor = round(factor, 3)
            sug.suggested_qty = round(sug.suggested_qty * factor, 2)
        if t:
            moq = t.get("moq")
            mult = t.get("order_multiple")
            sug.suggested_qty = _round_order_qty(sug.suggested_qty, moq, mult)
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
            sug.line_cost_eur = _line_cost(cost, sug.suggested_qty)
        override = t.get("unit_cost_override") if t else None
        sug.cost_provenance = (
            buyer_provenance(cost, sug.suggested_qty)
            if override is not None and float(override) > 0
            else cost_cohort.provenance(pid, producer_id, sug.suggested_qty)
        )
        sug.budget_priority_weight = _URGENCY_WEIGHT[sug.urgency] if sug.suggested_qty > 0 else 0.0
        if sale is not None:
            sug.unit_sale_eur = sale
            sug.sale_price_basis = "historical_recorded_tax_basis_unverified"
        sug.unit_margin_eur = None
        sug.value_density = None
        items.append(sug)

    _attach_cheaper_alt(producer_id, items, as_of, cost_cohort)

    # urgency-first ordering
    items.sort(
        key=lambda x: (
            _URGENCY_ORDER[x.urgency],
            -x.suggested_qty,
            x.product_id,
            x.producer_id,
        )
    )

    resolved_name = repo.producer_name(producer_id) if producer_name is _UNSET else producer_name
    # Stamp producer_name onto every line so the warehouse lens (cart plan spanning all
    # producers) can group by producer without a second lookup.
    # Product meta (name / OE number / photo) so the console renders a readable товар.
    meta = repo.product_meta([it.product_id for it in items]) if items else {}
    for it in items:
        it.producer_name = resolved_name
        m = meta.get(it.product_id)
        if m:
            it.product_name = m["name"]
            it.vendor_code = m["vendor_code"]
            it.oe_number = m["oe_number"]
            it.image_url = m["image_url"]
    return ProducerPurchasePlan(
        producer_id=producer_id,
        producer_name=resolved_name,
        lead_time_days=round(lt_mean, 1),
        lead_time_std_days=round(lt_std, 1),
        lead_time_source=lt_source,
        items=items,
        item_count=len(items),
        cost_observation_manifest=cost_cohort.manifest(product_ids),
        **_cost_total_metadata(items),
        as_of_date=as_of,
        **coverage.as_metadata(),
    )


def _cart_producer_plan(
    producer_id: int,
    as_of: str,
    only_needed: bool,
    abc_map: dict[int, str],
    producer_name,
    source_fingerprint: str | None = None,
    *,
    cost_cohort: PurchaseCostCohort,
    product_ids: list[int],
) -> ProducerPurchasePlan:
    # Cached producer outputs are not proof of the current build's financial input.
    return build_plan(
        producer_id,
        as_of,
        only_needed=only_needed,
        abc_map=abc_map,
        producer_name=producer_name,
        cost_cohort=cost_cohort,
        product_ids=product_ids,
    )


def _select_urgency_lines(
    items: list[ReorderSuggestion], budget_eur: float, method: str, time_limit: int
) -> tuple[set[int], str, Fraction, Fraction]:
    exact_budget = Fraction(_decimal(budget_eur))
    eligible = [
        i
        for i, item in enumerate(items)
        if item.cost_provenance.budget_eligible
        and item.suggested_qty > 0
        and item.unit_cost_eur is not None
        and item.line_cost_eur is not None
        and item.line_cost_eur >= 0
    ]
    # Verified factualzero is always eligible; generic optimizer semantics remain untouched.
    chosen = {i for i in eligible if items[i].line_cost_eur == 0}
    positive = [i for i in eligible if items[i].line_cost_eur > 0]
    exact_costs = {i: Fraction(_decimal(items[i].line_cost_eur)) for i in positive}
    weights = {i: Fraction(_decimal(_URGENCY_WEIGHT[items[i].urgency])) for i in eligible}
    method_used = "greedy"
    selected_positive = set()
    if method == "milp" and positive and exact_budget > 0:
        selected, used_milp = milp_opt.select_within_budget(
            [float(weights[i]) for i in positive],
            [items[i].line_cost_eur for i in positive],
            budget_eur,
            time_limit,
        )
        if used_milp:
            selected_positive = {positive[index] for index in selected}
            method_used = "milp"
    if method_used == "greedy":
        used = Fraction()
        for i in sorted(
            positive, key=lambda i: (-weights[i] / exact_costs[i], items[i].product_id, items[i].producer_id)
        ):
            if used + exact_costs[i] <= exact_budget:
                selected_positive.add(i)
                used += exact_costs[i]
    chosen.update(selected_positive)
    used = sum((Fraction(_decimal(items[i].line_cost_eur)) for i in chosen), Fraction())
    if used > exact_budget or any(not items[i].cost_provenance.budget_eligible for i in chosen):
        raise ValueError("budget selection failed exact eligibility or amount verification")
    score = sum((weights[i] for i in chosen), Fraction())
    return chosen, method_used, used, score


def build_cart_plan(
    as_of: str,
    only_needed: bool = True,
    limit: int | None = None,
    budget_eur: float | None = None,
    method: str = "greedy",
    active_days: int | None = None,
    source_fingerprint: str | None = None,
    *,
    cost_cohort: PurchaseCostCohort | None = None,
    products_by_producer: dict[int, list[int]] | None = None,
) -> CartReplenishmentPlan:
    s = get_settings()
    coverage = rolling_coverage(as_of, s.history_days)
    producer_ids = repo.all_producers(as_of, active_days or s.history_days)
    abc_map = classify_svc.get_abc_map(as_of, s.history_days)
    names = repo.producer_names(producer_ids)

    if budget_eur is not None and (not math.isfinite(budget_eur) or budget_eur < 0):
        raise ValueError("budget must be finite and nonnegative")
    products_by_producer = dict(products_by_producer or {})
    for producer in producer_ids:
        if producer not in products_by_producer:
            products_by_producer[producer] = repo.products_for_producer(producer, as_of, s.history_days)
    products = sorted({product for producer in producer_ids for product in products_by_producer[producer]})
    cost_cohort = cost_cohort or read_purchase_cost_cohort(products, as_of)
    cost_cohort.require_scope(products, as_of)

    items: list[ReorderSuggestion] = []
    for producer_id in producer_ids:
        plan = _cart_producer_plan(
            producer_id,
            as_of,
            only_needed,
            abc_map,
            names.get(producer_id),
            source_fingerprint,
            cost_cohort=cost_cohort,
            product_ids=products_by_producer[producer_id],
        )
        if only_needed:
            items.extend(sug for sug in plan.items if sug.suggested_qty > 0)
        else:
            items.extend(plan.items)
    items, duplicate_supplier_options_removed = _deduplicate_supplier_options(items)
    items.sort(key=lambda item: (item.product_id, item.producer_id))
    totals = _cart_totals(items)
    cost_metadata = _cost_total_metadata(items)
    manifest = cost_cohort.manifest(products)
    for item in items:
        item.unit_margin_eur = None
        item.value_density = None
        item.budget_priority_weight = _URGENCY_WEIGHT[item.urgency] if item.suggested_qty > 0 else 0.0
    total_item_count = len(items)

    if budget_eur is not None:
        chosen, method_used, used, score = _select_urgency_lines(items, budget_eur, method, s.milp_time_limit)
        for i, item in enumerate(items):
            item.within_budget = i in chosen
        items.sort(
            key=lambda item: (
                not item.within_budget,
                _URGENCY_ORDER[item.urgency],
                item.product_id,
                item.producer_id,
            )
        )
        return CartReplenishmentPlan(
            items=items,
            item_count=len(items),
            as_of_date=as_of,
            total_item_count=total_item_count,
            duplicate_supplier_options_removed=duplicate_supplier_options_removed,
            **totals,
            budget_eur=budget_eur,
            budget_used_eur=_round_decimal(used, _CENT),
            value_captured_eur=None,
            budget_objective="urgency_weighted_lines",
            budget_score=_exact_quantity_float(_decimal(score)),
            cost_observation_manifest=manifest,
            **cost_metadata,
            selected_count=len(chosen),
            deferred_count=len(items) - len(chosen),
            method_used=method_used,
            **coverage.as_metadata(),
        )

    items.sort(
        key=lambda x: (
            _URGENCY_ORDER[x.urgency],
            x.days_of_cover,
            x.product_id,
            x.producer_id,
        )
    )
    if limit is not None and limit >= 0:
        items = items[:limit]

    return CartReplenishmentPlan(
        items=items,
        item_count=len(items),
        total_item_count=total_item_count,
        is_truncated=len(items) < total_item_count,
        duplicate_supplier_options_removed=duplicate_supplier_options_removed,
        cost_observation_manifest=manifest,
        **cost_metadata,
        **totals,
        as_of_date=as_of,
        **coverage.as_metadata(),
    )


def captured_cart_variant(
    plan: CartReplenishmentPlan, *, budget_eur: float | None, method: str, limit: int | None
) -> CartReplenishmentPlan:
    """Scenario over a complete captured plan; retains its original proof/read window."""
    if plan.is_truncated or plan.total_item_count != plan.item_count:
        raise ValueError("captured cart must be complete before scenario selection")
    result = plan.model_copy(deep=True)
    if budget_eur is not None:
        if not math.isfinite(budget_eur) or budget_eur < 0:
            raise ValueError("budget must be finite and nonnegative")
        chosen, used_method, used, score = _select_urgency_lines(
            result.items,
            budget_eur,
            method,
            get_settings().milp_time_limit,
        )
        for index, item in enumerate(result.items):
            item.within_budget = index in chosen
            item.unit_margin_eur = None
            item.value_density = None
        result.items.sort(
            key=lambda item: (
                not item.within_budget,
                _URGENCY_ORDER[item.urgency],
                item.product_id,
                item.producer_id,
            )
        )
        result.budget_eur = budget_eur
        result.budget_used_eur = _round_decimal(used, _CENT)
        result.budget_objective = "urgency_weighted_lines"
        result.budget_score = _exact_quantity_float(_decimal(score))
        result.value_captured_eur = None
        result.selected_count = len(chosen)
        result.deferred_count = len(result.items) - len(chosen)
        result.method_used = used_method
    elif limit is not None and limit >= 0:
        result.items = result.items[:limit]
        result.item_count = len(result.items)
        result.is_truncated = result.item_count < result.total_item_count
    return result


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
    suggestions: dict[int, ReorderSuggestion],
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
            DemandPoint(period=m, units=round(monthly[m], 2), is_forecast=False) for m in sorted(monthly)
        ]
        fc = forecasts.get(pid)
        forecast_units = float(fc.forecast_units) if fc else 0.0
        points.append(DemandPoint(period=forecast_period, units=round(forecast_units, 2), is_forecast=True))
        suggestion = suggestions.get(pid)
        series.append(
            DemandSeries(
                product_id=pid,
                product_name=suggestion.product_name if suggestion else None,
                vendor_code=suggestion.vendor_code if suggestion else None,
                oe_number=suggestion.oe_number if suggestion else None,
                image_url=suggestion.image_url if suggestion else None,
                producer_id=suggestion.producer_id if suggestion else None,
                producer_name=suggestion.producer_name if suggestion else None,
                points=points,
            )
        )
    return series


def build_charts(
    producer_id: int | None,
    as_of: str,
    top_n: int = 15,
    source_fingerprint: str | None = None,
    cost_cohort: PurchaseCostCohort | None = None,
    products_by_producer: dict[int, list[int]] | None = None,
    plan_input: ProducerPurchasePlan | CartReplenishmentPlan | None = None,
) -> PlanCharts:
    """Procurement-dashboard chart data for one producer (or the whole cart).

    Producer-scoped: build_plan(only_needed=False) so the urgency mix / cover histogram show
    the FULL distribution (including sufficiently-covered items). Cart-wide (producer_id=None):
    reuse build_cart_plan (needed-only across all producers). demand_series covers the top few.
    """
    s = get_settings()
    coverage = rolling_coverage(as_of, s.history_days)
    top_n = max(0, int(top_n))

    if plan_input is not None:
        if plan_input.as_of_date != as_of or (
            producer_id is not None and getattr(plan_input, "producer_id", None) != producer_id
        ):
            raise ValueError("chart input scope differs from requested plan")
        plan = plan_input
        items = plan.items
    elif producer_id is not None:
        plan = build_plan(
            producer_id,
            as_of,
            only_needed=False,
            cost_cohort=cost_cohort,
            product_ids=(products_by_producer or {}).get(producer_id),
        )
        items = plan.items
    else:
        plan = build_cart_plan(
            as_of,
            only_needed=True,
            limit=-1,
            source_fingerprint=source_fingerprint,
            cost_cohort=cost_cohort,
            products_by_producer=products_by_producer,
        )
        items = plan.items

    # urgency_mix — count per urgency, critical-first, every bucket present.
    counts = {u: 0 for u in Urgency}
    for it in items:
        counts[it.urgency] += 1
    urgency_mix = [
        UrgencyMixBucket(urgency=u, count=counts[u]) for u in sorted(counts, key=lambda u: _URGENCY_ORDER[u])
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
            product_name=it.product_name,
            vendor_code=it.vendor_code,
            oe_number=it.oe_number,
            image_url=it.image_url,
            producer_id=it.producer_id,
            producer_name=it.producer_name,
            suggested_qty=it.suggested_qty,
            on_hand=it.inventory.on_hand,
            reorder_point=it.reorder_point,
            urgency=it.urgency,
            evidence=_chart_item_evidence(it),
        )
        for it in ranked[:top_n]
    ]

    # demand_series — history + flagged forecast for the top few products.
    series_n = min(top_n, _DEMAND_SERIES_MAX)
    series_ids = [it.product_id for it in ranked[:series_n]]
    forecasts = {it.product_id: it.forecast for it in ranked[:series_n]}
    series_suggestions = {it.product_id: it for it in ranked[:series_n]}
    demand_series = _demand_series_for(
        series_ids,
        forecasts,
        series_suggestions,
        as_of,
        s.history_days,
    )

    return PlanCharts(
        population=_chart_population(plan, producer_id),
        producer_id=producer_id,
        as_of_date=as_of,
        top_n=top_n,
        urgency_mix=urgency_mix,
        days_of_cover_hist=days_of_cover_hist,
        top_items=top_items,
        demand_series=demand_series,
        cost_observation_manifest=plan.cost_observation_manifest,
        cost_total_basis=plan.cost_total_basis,
        cost_totals_certified=plan.cost_totals_certified,
        **coverage.as_metadata(),
    )


def _chart_item_evidence(item: ReorderSuggestion) -> ChartItemEvidence:
    return ChartItemEvidence(
        inventory=item.inventory, forecast_method=item.forecast.method,
        forecast_units=item.forecast.forecast_units, forecast_horizon_days=item.forecast.horizon_days,
        unit_cost_eur=item.unit_cost_eur, line_cost_eur=item.line_cost_eur,
        cost_coverage=item.cost_provenance.coverage, budget_eligible=item.cost_provenance.budget_eligible,
        budget_exclusion_reasons=item.cost_provenance.budget_exclusion_reasons, reason=item.reason,
    )


def _chart_population(plan, producer_id: int | None) -> ChartPopulation:
    return ChartPopulation(
        scope="supplier_assortment" if producer_id is not None else "needed_cart",
        item_count=len(plan.items), unique_product_count=len({item.product_id for item in plan.items}),
        total_suggested_qty=sum(item.suggested_qty for item in plan.items),
        unpriced_item_count=sum(item.line_cost_eur is None for item in plan.items),
        budget_ineligible_item_count=sum(not item.cost_provenance.budget_eligible for item in plan.items),
        budget_eur=getattr(plan, "budget_eur", None),
    )


def enrich_captured_charts(charts: PlanCharts, plan) -> PlanCharts:
    """Add evidence from the same cached plan; no SQL, generation, ranking or writes."""
    if charts.as_of_date != plan.as_of_date or charts.producer_id != getattr(plan, "producer_id", None):
        return charts
    if getattr(plan, "is_truncated", False) or plan.item_count != len(plan.items):
        return charts
    if charts.cost_observation_manifest != plan.cost_observation_manifest:
        return charts
    if sum(bucket.count for bucket in charts.urgency_mix) != len(plan.items):
        return charts
    items = {(item.product_id, item.producer_id): item for item in plan.items}
    for top in charts.top_items:
        item = items.get((top.product_id, top.producer_id))
        if (item is None or item.suggested_qty != top.suggested_qty or item.urgency != top.urgency
                or item.inventory.on_hand != top.on_hand or item.reorder_point != top.reorder_point):
            return charts
    return charts.model_copy(update={
        "population": _chart_population(plan, charts.producer_id),
        "top_items": [top.model_copy(update={"evidence": _chart_item_evidence(items[(top.product_id, top.producer_id)])}) for top in charts.top_items],
    })
