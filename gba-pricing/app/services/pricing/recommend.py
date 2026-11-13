from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

from app.core.config import get_settings
from app.domain.models import (
    Confidence,
    DiscountBand,
    PeerBand,
    PriceRecommendation,
)
from app.domain.money import CENT, HUNDRED, ZERO, MoneyValue, as_decimal, optional_decimal
from app.services.pricing.elasticity import elastic_optimal_price, is_sane_elasticity

HIGH_COST_LOTS = 3
HIGH_PEER_N = 10
LOW_PEER_N = 3

ELAS_SOURCE_PER_SKU = "per-sku"
ELAS_SOURCE_POOLED = "pooled-group"
ELAS_SOURCE_NONE = "none"

BASELINE_SOURCE_AGREEMENT = "agreement"
BASELINE_SOURCE_CLIENT_WORLD = "client_world_fallback"

R_BELOW_MARGIN = "below-margin-loss-flag"
R_MARGIN_FLOOR = "margin-floor"
R_PEER_MEDIAN = "peer-median"
R_AT_BASELINE = "at-baseline"
R_DISCOUNT_CAP = "discount-cap"
R_NO_ANCHOR = "no-anchor"
R_NO_BASELINE = "no-baseline"
R_CONSTRAINTS_CONFLICT = "constraints-conflict"

CONFLICT_DISCOUNT_ABOVE_PEER_CAP = "current-discount-above-peer-cap"
CONFLICT_PRICE_AT_MARGIN_FLOOR = "current-price-at-margin-floor"
CONFLICT_FLOOR_ABOVE_LIST = "margin-floor-above-list-price"


def _round2(value: MoneyValue) -> float:
    """Serialize a monetary/percentage value at two decimals using accounting rounding."""
    return float(as_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


FLOOR_BASIS_MARKUP_ON_COST = "markup_on_cost"


def floor_terms(target_margin_pct: MoneyValue) -> dict:
    """The floor rule stated both ways: a markup on cost and the margin of price it yields.

    The floor is unit_cost*(1+markup/100), so the margin of the floor price is
    markup/(100+markup)*100 — 12 % on cost is 10.71 % of price."""
    markup = as_decimal(target_margin_pct)
    return {
        "floor_markup_pct": _round2(markup),
        "floor_margin_pct": _round2(markup / (HUNDRED + markup) * HUNDRED),
        "floor_basis": FLOOR_BASIS_MARKUP_ON_COST,
    }


def price_floor(
    unit_cost_eur: MoneyValue | None,
    target_margin_pct: MoneyValue,
) -> Decimal | None:
    """Hard price floor: unit_cost_eur*(1+target_margin_pct/100) — target_margin_pct is a
    markup on cost, see floor_terms for the equivalent margin of price. None when no cost
    lot exists (peer-band-only path, floor skipped)."""
    if unit_cost_eur is None:
        return None
    unit_cost = as_decimal(unit_cost_eur)
    margin_pct = as_decimal(target_margin_pct)
    return unit_cost * (Decimal(1) + margin_pct / HUNDRED)


@dataclass
class RecommendedPrice:
    value: Decimal | None
    rationale: str


def recommended_price(
    floor: MoneyValue | None,
    peer_p50: MoneyValue | None,
    baseline: MoneyValue | None,
) -> RecommendedPrice:
    """clamp( max(floor, peer_P50), lower=floor, upper=baseline ).

    Never above the engine baseline; never below the margin floor. floor>baseline is a LOSS
    FLAG -> recommended=floor, rationale='below-margin-loss-flag'. With no floor (no cost) the
    band is peer-only and still capped at the baseline.
    """
    floor_value = optional_decimal(floor)
    peer_value = optional_decimal(peer_p50)
    baseline_value = optional_decimal(baseline)

    if baseline_value is not None and baseline_value <= ZERO:
        return RecommendedPrice(value=None, rationale=R_NO_BASELINE)

    if (
        floor_value is not None
        and baseline_value is not None
        and floor_value > baseline_value
    ):
        return RecommendedPrice(value=floor_value, rationale=R_BELOW_MARGIN)

    target = peer_value
    if floor_value is not None:
        target = floor_value if target is None else max(floor_value, target)

    if target is None:
        if baseline_value is not None:
            return RecommendedPrice(value=baseline_value, rationale=R_AT_BASELINE)
        return RecommendedPrice(value=None, rationale=R_NO_ANCHOR)

    bound = target
    if floor_value is not None:
        bound = max(bound, floor_value)
    if baseline_value is not None:
        bound = min(bound, baseline_value)

    if floor_value is not None and bound <= floor_value:
        rationale = R_MARGIN_FLOOR
    elif baseline_value is not None and bound >= baseline_value:
        rationale = R_AT_BASELINE
    else:
        rationale = R_PEER_MEDIAN
    return RecommendedPrice(value=bound, rationale=rationale)


def discount_from_price(
    rec_price: MoneyValue | None,
    marked_up: MoneyValue | None,
) -> Decimal | None:
    """The DiscountRate that reproduces rec_price through the engine formula:
    (1 - rec_price/marked_up)*100 where marked_up = ROUND(P + P*ExtraCharge/100, 14)."""
    rec_value = optional_decimal(rec_price)
    marked_up_value = optional_decimal(marked_up)
    if rec_value is None or marked_up_value is None or marked_up_value <= ZERO:
        return None
    return (Decimal(1) - rec_value / marked_up_value) * HUNDRED


def cap_discount(
    raw_discount_pct: MoneyValue | None,
    peer_p75: MoneyValue | None,
    peer_p90: MoneyValue | None,
) -> tuple[Decimal | None, bool]:
    """Cap the suggested discount at the peer P75 of ProductGroupDiscount.DiscountRate, hard-
    capped at P90. Returns (capped_pct, was_capped). Negative discounts (a recommended price
    above the marked-up list) are floored at 0."""
    if raw_discount_pct is None:
        return None, False
    capped = max(ZERO, as_decimal(raw_discount_pct))
    peer_p75_value = optional_decimal(peer_p75)
    peer_p90_value = optional_decimal(peer_p90)
    was_capped = False
    cap = peer_p75_value if peer_p75_value is not None else peer_p90_value
    if peer_p90_value is not None:
        cap = peer_p90_value if cap is None else min(cap, peer_p90_value)
    if cap is not None and capped > cap:
        capped = cap
        was_capped = True
    return capped, was_capped


def confidence_for(
    lot_count: int,
    peer_n: int,
    has_cost: bool,
    baseline: MoneyValue | None = None,
) -> Confidence:
    """high if cost lots>=3 AND peer n>=10; low if no cost OR peer n<3; medium otherwise.

    A missing/non-positive baseline (no live engine price for this product × agreement) forces
    LOW so the console suppresses the recommendation instead of surfacing a bogus anchor.
    """
    baseline_value = optional_decimal(baseline)
    if baseline_value is None or baseline_value <= ZERO:
        return Confidence.LOW
    if not has_cost or peer_n < LOW_PEER_N:
        return Confidence.LOW
    if lot_count >= HIGH_COST_LOTS and peer_n >= HIGH_PEER_N:
        return Confidence.HIGH
    return Confidence.MEDIUM


def margin_pct_at(
    rec_price: MoneyValue | None,
    unit_cost_eur: MoneyValue | None,
) -> Decimal | None:
    """(rec_price - unit_cost_eur)/rec_price*100. None if no cost or non-positive price."""
    rec_value = optional_decimal(rec_price)
    unit_cost = optional_decimal(unit_cost_eur)
    if rec_value is None or unit_cost is None or rec_value <= ZERO:
        return None
    return (rec_value - unit_cost) / rec_value * HUNDRED


def rationale_for(price_rationale: str, discount_was_capped: bool) -> str:
    """The binding constraint. A discount-cap that actually bound is surfaced over a peer-median
    anchor; a margin floor / loss flag / baseline anchor always take precedence."""
    if price_rationale in (
        R_BELOW_MARGIN, R_MARGIN_FLOOR, R_AT_BASELINE, R_NO_ANCHOR, R_NO_BASELINE
    ):
        return price_rationale
    if discount_was_capped:
        return R_DISCOUNT_CAP
    return price_rationale


def elasticity_outputs(
    elasticity_estimate: dict | None,
    unit_cost_eur: MoneyValue | None,
) -> tuple[float | None, str | None, float | None]:
    """Gate the SECONDARY elasticity signal. Returns (elasticity, source, elastic_optimal_price).

    The estimate is surfaced ONLY when it is present AND economically sane (e>0, in 0.5..5): the
    backtest showed ~85% of observational fits are wrong-signed (price-volume confounding from
    agreement size + secular drift), so an insane estimate is suppressed to None rather than shown.
    Even when surfaced, the elastic price is advisory -- it never replaces recommended_price.
    """
    if not elasticity_estimate:
        return None, ELAS_SOURCE_NONE, None
    e = elasticity_estimate.get("elasticity")
    src = elasticity_estimate.get("source") or ELAS_SOURCE_NONE
    if not is_sane_elasticity(e):
        return None, ELAS_SOURCE_NONE, None
    return e, src, elastic_optimal_price(e, unit_cost_eur)


def build_recommendation(
    *,
    product_id: int,
    client_agreement_netuid: str,
    baseline: MoneyValue | None,
    marked_up: MoneyValue | None,
    cost: dict,
    peer: dict,
    segment: dict,
    target_margin_pct: MoneyValue,
    as_of_date: str,
    elasticity_estimate: dict | None = None,
    baseline_source: str = BASELINE_SOURCE_AGREEMENT,
) -> PriceRecommendation:
    """Pure assembler — every input is already fetched. Computes floor, recommended price,
    suggested discount (capped), discount band, confidence, margin and rationale. No I/O.

    elasticity_estimate (optional) carries {elasticity, source} for a high-data SKU; it is gated
    to a sane magnitude and exposed as ADDITIONAL secondary fields only -- the PRIMARY
    recommended_price remains the A+B margin-floor+peer-band clamp regardless of elasticity.
    """
    unit_cost = cost.get("unit_cost_eur")
    lot_count = int(cost.get("lot_count") or 0)
    has_cost = unit_cost is not None

    floor = price_floor(unit_cost, target_margin_pct)

    p50 = peer.get("p50")
    peer_n = int(peer.get("n") or 0)
    rec = recommended_price(floor, p50, baseline)

    # A floor-implied discount is an UPPER bound. The agreement's current price
    # supplies the LOWER bound (except the existing explicit below-margin loss flag).
    # Round bounds inward at the actual two-decimal DiscountRate precision.
    marked_up_value = optional_decimal(marked_up)
    baseline_value = optional_decimal(baseline)
    suggested_discount = None
    discount_band = None
    was_capped = False
    conflict = False
    conflict_reason = None
    conflict_explanation = None
    discount_cap = None
    price_at_cap = None
    if rec.value is not None and marked_up_value is not None and marked_up_value > ZERO:
        lo = ZERO
        if baseline_value is not None and rec.rationale != R_BELOW_MARGIN:
            lo = max(ZERO, discount_from_price(baseline_value, marked_up_value))
        hi = HUNDRED
        for limit in (segment.get("p75"), segment.get("p90"), discount_from_price(floor, marked_up_value)):
            if limit is not None:
                hi = min(hi, as_decimal(limit))
        lo = lo.quantize(CENT, rounding=ROUND_CEILING)
        hi = hi.quantize(CENT, rounding=ROUND_FLOOR)
        if lo > hi:
            conflict = True
            segment_caps = [
                as_decimal(limit).quantize(CENT, rounding=ROUND_FLOOR)
                for limit in (segment.get("p75"), segment.get("p90"))
                if limit is not None
            ]
            segment_cap = min(segment_caps) if segment_caps else None
            if hi >= ZERO:
                discount_cap = hi
                price_at_cap = marked_up_value * (Decimal(1) - hi / HUNDRED)
            if rec.rationale == R_BELOW_MARGIN:
                conflict_reason = CONFLICT_FLOOR_ABOVE_LIST
                conflict_explanation = (
                    f"margin floor {_round2(floor):.2f} is above the undiscounted list price "
                    f"{_round2(marked_up_value):.2f}; no discount rate can reach it, the floor "
                    "is returned as an advisory price"
                )
            else:
                hold_discount = max(
                    ZERO, discount_from_price(baseline_value, marked_up_value)
                ).quantize(CENT, rounding=ROUND_HALF_UP)
                hold_price = marked_up_value * (Decimal(1) - hold_discount / HUNDRED)
                if segment_cap is not None and segment_cap < lo:
                    conflict_reason = CONFLICT_DISCOUNT_ABOVE_PEER_CAP
                    conflict_explanation = (
                        f"current agreement discount {_round2(hold_discount):.2f}% exceeds the "
                        f"peer discount cap {_round2(segment_cap):.2f}%; the current agreement "
                        "price is held and no further discount is recommended"
                    )
                else:
                    conflict_reason = CONFLICT_PRICE_AT_MARGIN_FLOOR
                    conflict_explanation = (
                        f"current agreement price leaves no discount room above the margin "
                        f"floor {_round2(floor):.2f}; the current agreement price is held"
                    )
                if floor is None or _round2(hold_price) >= _round2(floor):
                    rec = RecommendedPrice(hold_price, R_CONSTRAINTS_CONFLICT)
                    suggested_discount = hold_discount
                    discount_band = DiscountBand(
                        min_pct=float(hold_discount),
                        target_pct=float(hold_discount),
                        max_pct=float(hold_discount),
                    )
                else:
                    rec = RecommendedPrice(None, R_CONSTRAINTS_CONFLICT)
        else:
            raw_discount = discount_from_price(rec.value, marked_up_value)
            quantized = raw_discount.quantize(CENT, rounding=ROUND_HALF_UP)
            suggested_discount = min(max(quantized, lo), hi)
            was_capped = suggested_discount < quantized
            rec.value = marked_up_value * (Decimal(1) - suggested_discount / HUNDRED)
            discount_band = DiscountBand(
                min_pct=float(lo), target_pct=float(suggested_discount), max_pct=float(hi),
            )

    confidence = confidence_for(lot_count, peer_n, has_cost, baseline)
    if conflict:
        confidence = Confidence.LOW
    margin_pct = margin_pct_at(rec.value, unit_cost)
    rationale = rationale_for(rec.rationale, was_capped)

    elasticity_val, elasticity_src, elastic_price = elasticity_outputs(
        elasticity_estimate, unit_cost
    )

    return PriceRecommendation(
        product_id=product_id,
        client_agreement_netuid=client_agreement_netuid,
        currency="EUR",
        baseline_price=_round2(baseline) if baseline is not None else None,
        baseline_source=baseline_source if baseline is not None else None,
        recommended_price=_round2(rec.value) if rec.value is not None else None,
        price_floor=_round2(floor) if floor is not None else None,
        **floor_terms(target_margin_pct),
        unit_cost_eur=_round2(unit_cost) if unit_cost is not None else None,
        suggested_discount_pct=(
            _round2(suggested_discount) if suggested_discount is not None else None
        ),
        discount_band=discount_band,
        discount_base_price=float(marked_up_value) if marked_up_value is not None else None,
        peer_band=PeerBand(
            p25=_round2(peer["p25"]) if peer.get("p25") is not None else None,
            p50=_round2(p50) if p50 is not None else None,
            p75=_round2(peer["p75"]) if peer.get("p75") is not None else None,
            n=peer_n,
        ),
        confidence=confidence,
        margin_pct_at_recommended=_round2(margin_pct) if margin_pct is not None else None,
        rationale=rationale,
        constraints_conflict=conflict,
        conflict_reason=conflict_reason,
        conflict_explanation=conflict_explanation,
        discount_cap_pct=_round2(discount_cap) if discount_cap is not None else None,
        price_at_discount_cap=_round2(price_at_cap) if price_at_cap is not None else None,
        elasticity=round(elasticity_val, 3) if elasticity_val is not None else None,
        elasticity_source=elasticity_src,
        elastic_optimal_price=_round2(elastic_price) if elastic_price is not None else None,
        as_of_date=as_of_date,
        model_version=get_settings().model_version,
    )
