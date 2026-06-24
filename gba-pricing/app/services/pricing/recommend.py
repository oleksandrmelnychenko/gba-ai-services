from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.domain.models import (
    Confidence,
    DiscountBand,
    PeerBand,
    PriceRecommendation,
)
from app.services.pricing.elasticity import elastic_optimal_price, is_sane_elasticity

HIGH_COST_LOTS = 3
HIGH_PEER_N = 10
LOW_PEER_N = 3

ELAS_SOURCE_PER_SKU = "per-sku"
ELAS_SOURCE_POOLED = "pooled-group"
ELAS_SOURCE_NONE = "none"

R_BELOW_MARGIN = "below-margin-loss-flag"
R_MARGIN_FLOOR = "margin-floor"
R_PEER_MEDIAN = "peer-median"
R_AT_BASELINE = "at-baseline"
R_DISCOUNT_CAP = "discount-cap"
R_NO_ANCHOR = "no-anchor"
R_NO_BASELINE = "no-baseline"


def _round2(x: float) -> float:
    return round(x, 2)


def price_floor(unit_cost_eur: float | None, target_margin_pct: float) -> float | None:
    """Hard margin floor: unit_cost_eur*(1+target_margin_pct/100). None when no cost lot
    exists (peer-band-only path, floor skipped)."""
    if unit_cost_eur is None:
        return None
    return unit_cost_eur * (1.0 + target_margin_pct / 100.0)


@dataclass
class RecommendedPrice:
    value: float | None
    rationale: str


def recommended_price(
    floor: float | None,
    peer_p50: float | None,
    baseline: float | None,
) -> RecommendedPrice:
    """clamp( max(floor, peer_P50), lower=floor, upper=baseline ).

    Never above the engine baseline; never below the margin floor. floor>baseline is a LOSS
    FLAG -> recommended=floor, rationale='below-margin-loss-flag'. With no floor (no cost) the
    band is peer-only and still capped at the baseline.
    """
    if baseline is not None and baseline <= 0:
        return RecommendedPrice(value=None, rationale=R_NO_BASELINE)

    if floor is not None and baseline is not None and floor > baseline:
        return RecommendedPrice(value=floor, rationale=R_BELOW_MARGIN)

    target = peer_p50
    if floor is not None:
        target = floor if target is None else max(floor, target)

    if target is None:
        if baseline is not None:
            return RecommendedPrice(value=baseline, rationale=R_AT_BASELINE)
        return RecommendedPrice(value=None, rationale=R_NO_ANCHOR)

    bound = target
    if floor is not None:
        bound = max(bound, floor)
    if baseline is not None:
        bound = min(bound, baseline)

    if floor is not None and bound <= floor:
        rationale = R_MARGIN_FLOOR
    elif baseline is not None and bound >= baseline:
        rationale = R_AT_BASELINE
    else:
        rationale = R_PEER_MEDIAN
    return RecommendedPrice(value=bound, rationale=rationale)


def discount_from_price(rec_price: float | None, marked_up: float | None) -> float | None:
    """The DiscountRate that reproduces rec_price through the engine formula:
    (1 - rec_price/marked_up)*100 where marked_up = ROUND(P + P*ExtraCharge/100, 14)."""
    if rec_price is None or not marked_up or marked_up <= 0:
        return None
    return (1.0 - rec_price / marked_up) * 100.0


def cap_discount(
    raw_discount_pct: float | None,
    peer_p75: float | None,
    peer_p90: float | None,
) -> tuple[float | None, bool]:
    """Cap the suggested discount at the peer P75 of ProductGroupDiscount.DiscountRate, hard-
    capped at P90. Returns (capped_pct, was_capped). Negative discounts (a recommended price
    above the marked-up list) are floored at 0."""
    if raw_discount_pct is None:
        return None, False
    capped = max(0.0, raw_discount_pct)
    was_capped = False
    cap = peer_p75 if peer_p75 is not None else peer_p90
    if peer_p90 is not None:
        cap = peer_p90 if cap is None else min(cap, peer_p90)
    if cap is not None and capped > cap:
        capped = cap
        was_capped = True
    return capped, was_capped


def confidence_for(
    lot_count: int, peer_n: int, has_cost: bool, baseline: float | None = None
) -> Confidence:
    """high if cost lots>=3 AND peer n>=10; low if no cost OR peer n<3; medium otherwise.

    A missing/non-positive baseline (no live engine price for this product × agreement) forces
    LOW so the console suppresses the recommendation instead of surfacing a bogus anchor.
    """
    if baseline is None or baseline <= 0:
        return Confidence.LOW
    if not has_cost or peer_n < LOW_PEER_N:
        return Confidence.LOW
    if lot_count >= HIGH_COST_LOTS and peer_n >= HIGH_PEER_N:
        return Confidence.HIGH
    return Confidence.MEDIUM


def margin_pct_at(rec_price: float | None, unit_cost_eur: float | None) -> float | None:
    """(rec_price - unit_cost_eur)/rec_price*100. None if no cost or non-positive price."""
    if rec_price is None or unit_cost_eur is None or rec_price <= 0:
        return None
    return (rec_price - unit_cost_eur) / rec_price * 100.0


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
    elasticity_estimate: dict | None, unit_cost_eur: float | None
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
    baseline: float | None,
    marked_up: float | None,
    cost: dict,
    peer: dict,
    segment: dict,
    target_margin_pct: float,
    as_of_date: str,
    elasticity_estimate: dict | None = None,
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

    raw_discount = discount_from_price(rec.value, marked_up)
    seg_p75 = segment.get("p75")
    seg_p90 = segment.get("p90")
    suggested_discount, was_capped = cap_discount(raw_discount, seg_p75, seg_p90)

    floor_discount = discount_from_price(floor, marked_up)
    min_pct = max(0.0, floor_discount) if floor_discount is not None else 0.0
    max_pct = seg_p90 if seg_p90 is not None else (seg_p75 if seg_p75 is not None else 0.0)
    target_pct = suggested_discount if suggested_discount is not None else 0.0
    lo, hi = sorted([min_pct, max_pct])
    target_pct = min(max(target_pct, lo), hi)
    discount_band = DiscountBand(
        min_pct=_round2(lo),
        target_pct=_round2(target_pct),
        max_pct=_round2(hi),
    )
    assert discount_band.min_pct <= discount_band.target_pct <= discount_band.max_pct

    confidence = confidence_for(lot_count, peer_n, has_cost, baseline)
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
        recommended_price=_round2(rec.value) if rec.value is not None else None,
        price_floor=_round2(floor) if floor is not None else None,
        unit_cost_eur=_round2(unit_cost) if unit_cost is not None else None,
        suggested_discount_pct=(
            _round2(suggested_discount) if suggested_discount is not None else None
        ),
        discount_band=discount_band,
        peer_band=PeerBand(
            p25=_round2(peer["p25"]) if peer.get("p25") is not None else None,
            p50=_round2(p50) if p50 is not None else None,
            p75=_round2(peer["p75"]) if peer.get("p75") is not None else None,
            n=peer_n,
        ),
        confidence=confidence,
        margin_pct_at_recommended=_round2(margin_pct) if margin_pct is not None else None,
        rationale=rationale,
        elasticity=round(elasticity_val, 3) if elasticity_val is not None else None,
        elasticity_source=elasticity_src,
        elastic_optimal_price=_round2(elastic_price) if elastic_price is not None else None,
        as_of_date=as_of_date,
        model_version=get_settings().model_version,
    )
