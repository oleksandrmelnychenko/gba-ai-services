from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data import cache
from app.data import pricing_repository as repo
from app.domain.models import (
    Confidence,
    DiscountBand,
    PeerBand,
    PriceRecommendation,
)
from app.domain.money import HUNDRED, ZERO, MoneyValue, as_decimal, optional_decimal
from app.services.pricing import elasticity as elas
from app.services.pricing import recommend as engine

log = get_logger("pricing_service")


def _as_of(as_of_date: str | None) -> str:
    return as_of_date or datetime.now().strftime("%Y-%m-%d")


def _hydrate(
    data: dict,
    *,
    expected_product_id: int,
    expected_product_net_uid: str,
    expected_agreement_net_uid: str,
) -> PriceRecommendation | None:
    """Validate cached identity before hydrating.

    Cache entries written before the canonical product UUID was added are intentionally misses;
    a price computed for one entity is never allowed to cross an identity boundary.
    """
    if (
        data.get("product_id") != expected_product_id
        or str(data.get("product_net_uid") or "").lower()
        != expected_product_net_uid.lower()
        or str(data.get("client_agreement_netuid") or "").lower()
        != expected_agreement_net_uid.lower()
    ):
        return None
    band = data.get("discount_band")
    peer = data.get("peer_band") or {}
    return PriceRecommendation(
        product_id=data["product_id"],
        product_net_uid=data["product_net_uid"],
        client_agreement_netuid=data["client_agreement_netuid"],
        currency=data.get("currency", "EUR"),
        baseline_price=data.get("baseline_price"),
        baseline_source=data.get("baseline_source"),
        recommended_price=data.get("recommended_price"),
        price_floor=data.get("price_floor"),
        unit_cost_eur=data.get("unit_cost_eur"),
        suggested_discount_pct=data.get("suggested_discount_pct"),
        discount_band=DiscountBand(**band) if band else None,
        peer_band=PeerBand(**peer),
        confidence=Confidence(data.get("confidence", "low")),
        margin_pct_at_recommended=data.get("margin_pct_at_recommended"),
        rationale=data.get("rationale", ""),
        elasticity=data.get("elasticity"),
        elasticity_source=data.get("elasticity_source"),
        elastic_optimal_price=data.get("elastic_optimal_price"),
        as_of_date=data.get("as_of_date"),
        model_version=data.get("model_version", get_settings().model_version),
    )


def recommend_price(
    *,
    product_id: int | None,
    product_net_uid: str | None,
    client_agreement_net_uid: str,
    culture: str = "uk",
    with_vat: bool = True,
    target_margin_pct: float | None = None,
    as_of_date: str | None = None,
    use_cache: bool = True,
) -> PriceRecommendation:
    """A+B recommendation for one product × client-agreement.

    Resolves both entities (LookupError -> 404 at the API), pulls baseline/cost/peer/segment from
    the repository, then delegates the pure A+B math to the engine. Redis-cached on
    (product, agreement, as_of, margin, vat, culture); graceful-degrade on cache failure.

    A NULL/non-positive engine baseline (e.g. inactive/deleted Agreement -> the live world
    resolver returns NULL) falls back to the client's recent same-Organization-world realized
    price (repo.client_world_fallback_baseline, baseline_source='client_world_fallback'); only
    when that too is unavailable does the explicit no-baseline result go out.
    """
    started = datetime.now()
    settings = get_settings()
    as_of = _as_of(as_of_date)
    margin = settings.target_margin_pct if target_margin_pct is None else target_margin_pct
    margin_value = as_decimal(margin)
    if margin_value < ZERO or margin_value > HUNDRED:
        raise ValueError("target_margin_pct must be between 0 and 100")
    window = settings.trailing_window_months
    fx_date = settings.resolve_fx_date(as_of_date)

    product = repo.resolve_product(product_id, product_net_uid)
    if product is None:
        raise LookupError(
            f"product not found: id={product_id} net_uid={product_net_uid}"
        )
    agreement = repo.resolve_client_agreement(client_agreement_net_uid)
    if agreement is None:
        raise LookupError(f"client_agreement_net_uid not found: {client_agreement_net_uid}")

    pid = product["id"]
    p_net_uid = product["net_uid"]
    ca_net_uid = agreement["client_agreement_netuid"]
    key = cache.make_key(pid, ca_net_uid, as_of, margin, with_vat, culture)

    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            hydrated = _hydrate(
                cached,
                expected_product_id=pid,
                expected_product_net_uid=p_net_uid,
                expected_agreement_net_uid=ca_net_uid,
            )
            if hydrated is not None:
                return hydrated

    baseline = repo.baseline_price(p_net_uid, ca_net_uid, culture, with_vat)
    baseline_source = engine.BASELINE_SOURCE_AGREEMENT
    if baseline is None or baseline <= 0:
        fallback = repo.client_world_fallback_baseline(
            pid, agreement["client_agreement_id"], as_of, window
        )
        if fallback is not None and fallback["fallback_price"] > 0:
            baseline = fallback["fallback_price"]
            baseline_source = engine.BASELINE_SOURCE_CLIENT_WORLD
            log.info(
                "baseline_client_world_fallback",
                product_id=pid,
                client_agreement_netuid=ca_net_uid,
                fallback_price=str(baseline),
                n_lines=fallback["n"],
            )
    if baseline is None or baseline <= 0:
        result = _no_baseline_recommendation(pid, p_net_uid, ca_net_uid, as_of)
        if use_cache:
            cache.set(key, result.model_dump(mode="json"))
        latency = (datetime.now() - started).total_seconds() * 1000
        log.info(
            "recommend",
            product_id=pid,
            client_agreement_netuid=ca_net_uid,
            baseline_price=result.baseline_price,
            baseline_source=result.baseline_source,
            recommended_price=result.recommended_price,
            price_floor=result.price_floor,
            suggested_discount_pct=result.suggested_discount_pct,
            confidence=result.confidence.value,
            rationale=result.rationale,
            latency_ms=round(latency, 2),
        )
        return result

    list_markup = repo.base_list_price_and_markup(pid, agreement["agreement_id"])
    seg_culture = list_markup.get("culture") if list_markup else culture
    base_pricing_id = list_markup.get("base_pricing_id") if list_markup else None

    pg_id = repo.product_group_id(pid)
    group_disc = (
        (repo.active_group_discount(agreement["client_agreement_id"], pg_id) or 0.0)
        if pg_id is not None else 0.0
    )
    applied_disc = 0.0 if repo.is_promotional(pid, agreement["agreement_id"]) else group_disc
    marked_up = _marked_up_from_baseline(baseline, applied_disc)

    cost = repo.unit_cost_eur(pid)
    peer = repo.peer_band(pid, as_of, window, fx_date)

    segment = (
        repo.segment_discount_distribution(pg_id, base_pricing_id, seg_culture)
        if pg_id is not None and base_pricing_id is not None
        else {"p75": None, "p90": None, "n": 0}
    )

    elasticity_estimate = estimate_elasticity(pid, pg_id, as_of, window)

    result = engine.build_recommendation(
        product_id=pid,
        client_agreement_netuid=ca_net_uid,
        baseline=baseline,
        marked_up=marked_up,
        cost=cost,
        peer=peer,
        segment=segment,
        target_margin_pct=margin,
        as_of_date=as_of,
        elasticity_estimate=elasticity_estimate,
        baseline_source=baseline_source,
    )
    result = result.model_copy(update={"product_net_uid": p_net_uid})

    if use_cache:
        cache.set(key, result.model_dump(mode="json"))

    latency = (datetime.now() - started).total_seconds() * 1000
    log.info(
        "recommend",
        product_id=pid,
        client_agreement_netuid=ca_net_uid,
        baseline_price=result.baseline_price,
        baseline_source=result.baseline_source,
        recommended_price=result.recommended_price,
        price_floor=result.price_floor,
        suggested_discount_pct=result.suggested_discount_pct,
        confidence=result.confidence.value,
        rationale=result.rationale,
        latency_ms=round(latency, 2),
    )
    return result


def estimate_elasticity(pid: int, pg_id: int | None, as_of: str, window: int) -> dict | None:
    """SECONDARY elasticity estimate for a SKU (option C), or None when not estimable / disabled.

    Per-SKU fit on the (agreement x month) panel when the SKU has enough valid lines (bands A/B);
    else borrow the ProductGroup POOLED fit if that group clears the pooled line threshold; else
    NO estimate. The fit is returned RAW (sign/magnitude as estimated) -- the engine assembler
    applies the economic-sanity gate, so an insane (wrong-signed/extreme) fit is dropped to None
    downstream rather than surfaced. Gated behind elasticity_enabled (default OFF): the backtest
    held this lever, so the live serving path computes nothing unless a deployment opts in.
    """
    s = get_settings()
    if not s.elasticity_enabled:
        return None
    lines = repo.product_line_count(pid, as_of, window)
    if lines >= s.elasticity_min_lines_sku:
        cells = [
            elas.PanelCell(int(r["agreement_id"]), str(r["month"]), float(r["qty"]), float(r["price"]))
            for r in repo.product_panel(pid, as_of, window)
            if r["price"] is not None and r["qty"] is not None
        ]
        fit = elas.fit_elasticity(cells, mad_k=s.peer_band_mad_k, mad_min_rows=s.peer_band_mad_min_rows)
        if fit.elasticity is not None:
            return {"elasticity": fit.elasticity, "source": engine.ELAS_SOURCE_PER_SKU,
                    "n_kept": fit.n_kept, "r_squared": fit.r_squared}

    if pg_id is not None:
        pooled_cells = [
            elas.PanelCell(int(r["agreement_id"]), str(r["month"]), float(r["qty"]),
                           float(r["price"]), product_id=int(r["product_id"]))
            for r in repo.group_panel(pg_id, as_of, window, s.elasticity_pooled_max_products)
            if r["price"] is not None and r["qty"] is not None
        ]
        if len(pooled_cells) >= s.elasticity_pooled_min_lines:
            fit = elas.fit_elasticity(
                pooled_cells, pooled=True,
                mad_k=s.peer_band_mad_k, mad_min_rows=s.peer_band_mad_min_rows,
            )
            if fit.elasticity is not None:
                return {"elasticity": fit.elasticity, "source": engine.ELAS_SOURCE_POOLED,
                        "n_kept": fit.n_kept, "r_squared": fit.r_squared}
    return None


def _no_baseline_recommendation(
    product_id: int,
    product_net_uid: str,
    client_agreement_netuid: str,
    as_of: str,
) -> PriceRecommendation:
    """No live engine price (baseline missing or <=0) AND the client-world fallback found no
    same-world realized lines (or the requested world is undeterminable — worlds are never
    mixed). Emit an explicit no-baseline result with all targets None and LOW confidence so the
    console suppresses it — never run the A+B math on a 0 baseline (which would recommend 0.0 /
    a bogus floor)."""
    return PriceRecommendation(
        product_id=product_id,
        product_net_uid=product_net_uid,
        client_agreement_netuid=client_agreement_netuid,
        currency="EUR",
        baseline_price=None,
        recommended_price=None,
        price_floor=None,
        unit_cost_eur=None,
        suggested_discount_pct=None,
        discount_band=None,
        peer_band=PeerBand(),
        confidence=Confidence.LOW,
        margin_pct_at_recommended=None,
        rationale=engine.R_NO_BASELINE,
        as_of_date=as_of,
        model_version=get_settings().model_version,
    )


def _marked_up_from_baseline(
    baseline: MoneyValue | None,
    applied_discount_pct: MoneyValue,
) -> Decimal | None:
    """The engine's pre-discount list price, derived from the authoritative baseline:
    baseline = marked_up * (1 - applied_discount/100)  (OneTimeDiscount=0 with NULL OrderItemId),
    so marked_up = baseline / (1 - applied/100). applied = the active group DiscountRate on the
    normal branch, 0 on the promotional branch (where GetCalculatedProductPrice* forces
    DiscountRate=0). Deriving marked_up from baseline is correct for promo AND non-promo without
    re-resolving the pricing tier, and matches the price-book reconstruction on the normal branch.
    suggested DiscountRate is then solved as (1 - recommended/marked_up)*100."""
    baseline_value = optional_decimal(baseline)
    if baseline_value is None:
        return None
    discount_pct = as_decimal(applied_discount_pct)
    if discount_pct >= HUNDRED:
        return baseline_value
    return baseline_value / (Decimal(1) - discount_pct / HUNDRED)
