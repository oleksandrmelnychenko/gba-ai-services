"""DB-BACKED integration smoke (pytest.mark.integration).

SKIPPED when the dev-DB env is absent so unit CI stays green WITHOUT a DB; runnable via
`make integration` / `pytest -m integration` against the dev DB. These exercise the live A+B
engine for real entities and assert sane magnitudes/sources — the live-only failures that mocked
unit tests never caught.

Env (set before app.core.config import): DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD REDIS_DB.

Pinned dev-DB entities (current restored ConcordDb_V5, discovered live; read-only):
  FALLBACK    product 29427720 × agreement 641f1c1f-... -> engine baseline NULL and a positive
              same-world realized-price fallback.
  NORMAL      product 29460936 × agreement 681d8099-... -> applied 19% group discount;
              marked_up*(1-applied/100) reproduces the engine baseline (12.04632).
  CONTAMINATED product 29377180 -> one real on-hand lot (SourceDocumentType=3) and one 1С debt
              lot (SourceDocumentType=1); the robust unit cost must select only the real lot.
"""
from __future__ import annotations

import os
from decimal import Decimal

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

FALLBACK_PRODUCT_ID = 29427720
FALLBACK_CA_NETUID = "641f1c1f-2f15-463f-8cb8-85a5fde36174"

NORMAL_PRODUCT_ID = 29460936
NORMAL_CA_NETUID = "681d8099-a32e-4d3f-b9a1-b7aaac748bd1"
NORMAL_APPLIED_DISCOUNT_PCT = 19.0

CONTAMINATED_PRODUCT_ID = 29377180


def test_live_synthetic_debt_product_is_rejected_before_pricing():
    """The dynamically re-minted «Ввід боргів» row is an identity reject, not 200/no-baseline."""
    from app.data import pricing_repository as repo
    from app.services.pricing import service

    synthetic_id = repo.synthetic_product_id()
    assert repo.resolve_product(synthetic_id, None) is None
    with pytest.raises(LookupError, match="product not found"):
        service.recommend_price(
            product_id=synthetic_id,
            product_net_uid=None,
            client_agreement_net_uid=NORMAL_CA_NETUID,
            use_cache=False,
        )


def test_live_product_id_and_uid_must_describe_one_identity():
    from app.data import pricing_repository as repo

    normal = repo.resolve_product(NORMAL_PRODUCT_ID, None)
    fallback = repo.resolve_product(FALLBACK_PRODUCT_ID, None)
    assert normal is not None and fallback is not None
    assert repo.resolve_product(NORMAL_PRODUCT_ID, normal["net_uid"]) == normal
    assert repo.resolve_product(NORMAL_PRODUCT_ID, fallback["net_uid"]) is None


def test_null_engine_baseline_uses_same_world_fallback():
    """A live product with NULL engine baseline must use the positive, world-safe fallback."""
    from app.data import pricing_repository as repo
    from app.services.pricing import service

    product = repo.resolve_product(FALLBACK_PRODUCT_ID, None)
    assert product is not None
    assert (
        repo.baseline_price(
            product["net_uid"],
            FALLBACK_CA_NETUID,
            "uk",
            True,
        )
        is None
    )

    out = service.recommend_price(
        product_id=FALLBACK_PRODUCT_ID,
        product_net_uid=None,
        client_agreement_net_uid=FALLBACK_CA_NETUID,
        use_cache=False,
    )
    assert out.baseline_source == "client_world_fallback"
    assert out.baseline_price is not None and out.baseline_price > 0
    assert out.recommended_price is not None and out.recommended_price > 0


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
    applied_decimal = Decimal(str(applied))
    reproduced = marked_up * (Decimal(1) - applied_decimal / Decimal(100))
    assert abs(reproduced - baseline) < Decimal("0.000000001")


def test_normal_product_full_recommendation_is_sane():
    """End-to-end NORMAL recommendation: a positive baseline, recommended in (0, baseline], with a
    discount that solves back to the recommended price through marked_up."""
    from app.services.pricing import service

    out = service.recommend_price(
        product_id=NORMAL_PRODUCT_ID,
        product_net_uid=None,
        client_agreement_net_uid=NORMAL_CA_NETUID,
        use_cache=False,
    )
    assert out.baseline_price is not None and out.baseline_price > 0
    assert out.recommended_price is not None
    assert 0 < out.recommended_price <= out.baseline_price
    assert out.currency == "EUR"


def test_contaminated_cost_floor_not_inflated_by_debt_lots():
    """A CONTAMINATED-cost product: the unit cost (and thus the margin floor) must come from real
    supply lots, NOT the 1С balance-import debt lots. Asserts the debt-excluded cost is sane
    and strictly below the naive median that includes the debt lot."""
    from app.data import pricing_repository as repo
    from app.data.db import query

    cost = repo.unit_cost_eur(CONTAMINATED_PRODUCT_ID)
    assert cost["unit_cost_eur"] is not None
    assert isinstance(cost["unit_cost_eur"], Decimal)
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
    naive_median = Decimal(str(naive[0]["naive_median"]))
    assert cost["unit_cost_eur"] < naive_median


def test_decimal_cost_floor_propagates_to_recommendation():
    """End-to-end floor uses the unrounded Decimal aggregate, then rounds HALF_UP at the API."""
    from app.core.config import get_settings
    from app.data import pricing_repository as repo
    from app.domain.money import HUNDRED, as_decimal, round_cent
    from app.services.pricing import service

    raw_cost = repo.unit_cost_eur(NORMAL_PRODUCT_ID)["unit_cost_eur"]
    assert raw_cost is not None
    out = service.recommend_price(
        product_id=NORMAL_PRODUCT_ID,
        product_net_uid=None,
        client_agreement_net_uid=NORMAL_CA_NETUID,
        use_cache=False,
    )
    assert out.unit_cost_eur is not None
    assert out.price_floor is not None
    margin = as_decimal(get_settings().target_margin_pct)
    expected_floor = round_cent(raw_cost * (Decimal(1) + margin / HUNDRED))
    assert Decimal(str(out.unit_cost_eur)) == round_cent(raw_cost)
    assert Decimal(str(out.price_floor)) == expected_floor
    assert out.baseline_price is not None and out.price_floor < out.baseline_price
