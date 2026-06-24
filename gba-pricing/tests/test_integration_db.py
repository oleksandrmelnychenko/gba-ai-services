"""DB-BACKED integration smoke (pytest.mark.integration).

SKIPPED when the dev-DB env is absent so unit CI stays green WITHOUT a DB; runnable via
`make integration` / `pytest -m integration` against the dev DB. These exercise the live A+B
engine for real entities and assert sane magnitudes/sources — the live-only failures that mocked
unit tests never caught.

Env (set before app.core.config import): DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD REDIS_DB.

Pinned dev-DB entities (ConcordDb_V5, discovered live; parameterized, read-only):
  UNPRICED    product 26177445 × active agreement 642d648f-... -> engine baseline 0.
  NORMAL      product 26166832 × active agreement 642d648f-... -> applied 10% group discount;
              marked_up*(1-applied/100) reproduces the engine baseline (38.493).
  CONTAMINATED product 25948814 × active agreement 642d648f-... -> on-hand lots mix 1С debt lots
              (~805 EUR) with real lots (~23 EUR); debt-excluded cost stays sane.
"""
from __future__ import annotations

import os

import pytest

REQUIRED_DB_ENV = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not all(os.environ.get(k) for k in REQUIRED_DB_ENV),
        reason="dev-DB env not set (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD); "
        "run via `make integration`",
    ),
]

UNPRICED_PRODUCT_ID = 26177445
UNPRICED_CA_NETUID = "642d648f-8dca-460b-b9ab-e92d05e53dea"

NORMAL_PRODUCT_ID = 26166832
NORMAL_CA_NETUID = "642d648f-8dca-460b-b9ab-e92d05e53dea"
NORMAL_APPLIED_DISCOUNT_PCT = 10.0

CONTAMINATED_PRODUCT_ID = 25948814
CONTAMINATED_CA_NETUID = "642d648f-8dca-460b-b9ab-e92d05e53dea"


def test_unpriced_product_recommends_none_not_zero():
    """An UNPRICED product×agreement (engine baseline 0) -> recommended_price is None (NOT 0.0),
    rationale no-baseline, LOW confidence. Guards the >=0 short-circuit on the live engine."""
    from app.services.pricing import recommend as engine
    from app.services.pricing import service

    out = service.recommend_price(
        product_id=UNPRICED_PRODUCT_ID,
        product_net_uid=None,
        client_agreement_net_uid=UNPRICED_CA_NETUID,
        use_cache=False,
    )
    assert out.baseline_price is None
    assert out.recommended_price is None
    assert out.recommended_price != 0.0
    assert out.price_floor is None
    assert out.rationale == engine.R_NO_BASELINE
    assert out.confidence.value == "low"


def test_normal_product_marked_up_reproduces_engine_baseline():
    """A NORMAL product: marked_up*(1-applied_disc/100) reproduces the engine baseline, with a
    genuinely non-zero applied group discount. Guards the marked_up-from-baseline derivation and
    the active-discount lookup against the live engine."""
    from app.data import pricing_repository as repo
    from app.services.pricing.service import _marked_up_from_baseline

    product = repo.resolve_product(NORMAL_PRODUCT_ID, None)
    assert product is not None
    agreement = repo.resolve_client_agreement(NORMAL_CA_NETUID)
    assert agreement is not None

    baseline = repo.baseline_price(product["net_uid"], NORMAL_CA_NETUID, "uk", True)
    assert baseline is not None and baseline > 0

    pg_id = repo.product_group_id(NORMAL_PRODUCT_ID)
    assert pg_id is not None
    group_disc = repo.active_group_discount(agreement["client_agreement_id"], pg_id) or 0.0
    promo = repo.is_promotional(NORMAL_PRODUCT_ID, agreement["agreement_id"])
    applied = 0.0 if promo else group_disc

    assert applied == pytest.approx(NORMAL_APPLIED_DISCOUNT_PCT)

    marked_up = _marked_up_from_baseline(baseline, applied)
    assert marked_up is not None and marked_up > baseline
    assert marked_up * (1.0 - applied / 100.0) == pytest.approx(baseline, rel=1e-9)


def test_normal_product_full_recommendation_is_sane():
    """End-to-end NORMAL recommendation: a positive baseline and, when actionable, a discount that
    solves back to the recommended price through the marked-up engine denominator."""
    from app.services.pricing import service

    out = service.recommend_price(
        product_id=NORMAL_PRODUCT_ID,
        product_net_uid=None,
        client_agreement_net_uid=NORMAL_CA_NETUID,
        use_cache=False,
    )
    assert out.baseline_price is not None and out.baseline_price > 0
    assert out.recommended_price is not None
    assert out.recommended_price > 0
    assert out.currency == "EUR"


def test_live_discount_caps_use_actual_pricing_and_live_agreements():
    """Live guard for the ЦО2 bug: ЦО2 and ЦО1 share the same base ProductPricing family, but
    their active DiscountRate norms are materially different. Segment caps must stay keyed by the
    actual Agreement.PricingID and live agreement chain, not by dbo.GetBasePricingId."""
    from app.data import pricing_repository as repo

    product_group_id = 1
    co2 = repo.segment_discount_distribution(product_group_id, 849, "uk")
    co1 = repo.segment_discount_distribution(product_group_id, 852, "uk")

    assert co2["n"] > 100
    assert co1["n"] > 100
    assert co2["p75"] == pytest.approx(0.0)
    assert co2["p90"] == pytest.approx(0.0)
    assert co1["p75"] > co2["p75"] + 5.0
    assert co1["p90"] > co2["p90"] + 5.0


def test_contaminated_cost_floor_not_inflated_by_debt_lots():
    """A CONTAMINATED-cost product: the unit cost (and thus the margin floor) must come from real
    supply lots, NOT the 1С balance-import debt lots. Asserts the debt-excluded cost is sane
    (< 200 EUR) AND strictly far below the naive median that INCLUDES debt lots (~587 EUR), so a
    reverted exclusion would explode the floor."""
    from app.data import pricing_repository as repo
    from app.data.db import query

    cost = repo.unit_cost_eur(CONTAMINATED_PRODUCT_ID)
    assert cost["unit_cost_eur"] is not None
    assert cost["unit_cost_eur"] < 200.0
    assert cost["cost_source"] in ("median_onhand", "latest_lot")

    naive = query(
        """
        SELECT TOP 1 PERCENTILE_CONT(0.5)
                     WITHIN GROUP (ORDER BY ci.AccountingPrice) OVER () AS naive_median
        FROM dbo.ConsignmentItem ci
        WHERE ci.ProductID = :pid
              AND ci.Deleted = 0
              AND ci.AccountingPrice > 0
              AND ci.RemainingQty > 0
        """,
        {"pid": CONTAMINATED_PRODUCT_ID},
    )
    naive_median = float(naive[0]["naive_median"])
    assert naive_median > 400.0
    assert cost["unit_cost_eur"] < naive_median / 5.0


def test_contaminated_cost_floor_propagates_to_recommendation():
    """End-to-end on the CONTAMINATED product: the live recommendation's price_floor reflects the
    debt-EXCLUDED cost (cost*(1+target_margin)), never the debt-inflated naive cost — so the floor
    does not become a loss flag above the baseline."""
    from app.core.config import get_settings
    from app.services.pricing import service

    out = service.recommend_price(
        product_id=CONTAMINATED_PRODUCT_ID,
        product_net_uid=None,
        client_agreement_net_uid=CONTAMINATED_CA_NETUID,
        use_cache=False,
    )
    assert out.unit_cost_eur is not None
    assert out.unit_cost_eur < 200.0
    assert out.price_floor is not None
    margin = get_settings().target_margin_pct
    expected_floor = round(out.unit_cost_eur * (1.0 + margin / 100.0), 2)
    assert out.price_floor == pytest.approx(expected_floor, rel=1e-6)
    assert out.baseline_price is not None and out.price_floor < out.baseline_price
