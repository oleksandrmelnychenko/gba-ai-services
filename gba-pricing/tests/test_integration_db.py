"""DB-BACKED integration smoke (pytest.mark.integration).

SKIPPED when the dev-DB env is absent so unit CI stays green WITHOUT a DB; runnable via
`make integration` / `pytest -m integration` against the dev DB. These exercise the live A+B
engine for real entities and assert sane magnitudes/sources — the live-only failures that mocked
unit tests never caught.

Env (set before app.core.config import): DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD REDIS_DB.

Fixtures resolve current live identities and required data scenarios dynamically.
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

@pytest.fixture(scope="module")
def normal():
    from app.data.db import query
    from app.data import pricing_repository as repo
    rows = query("""
        SELECT TOP 100 p.ID AS pid, CONVERT(varchar(36), ca.NetUID) AS uid
        FROM dbo.Product p
        JOIN dbo.ProductProductGroup ppg ON ppg.ProductID=p.ID AND ppg.Deleted=0
        JOIN dbo.ProductGroupDiscount d ON d.ProductGroupID = ppg.ProductGroupID
        JOIN dbo.ClientAgreement ca ON ca.ID = d.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE p.Deleted = 0 AND p.NetUID IS NOT NULL AND ca.Deleted = 0
          AND a.Deleted = 0 AND a.IsActive = 1 AND d.IsActive = 1
          AND d.DiscountRate > 0 AND d.DiscountRate < 100
          AND EXISTS (SELECT 1 FROM dbo.ConsignmentItem ci WHERE ci.ProductID=p.ID
                      AND ci.Deleted=0 AND ci.RemainingQty>0 AND ci.AccountingPrice>0)
        ORDER BY p.ID, ca.ID
    """)
    for row in rows:
        product = repo.resolve_product(row["pid"], None)
        agreement = repo.resolve_client_agreement(row["uid"])
        if not product or not agreement or repo.is_promotional(row["pid"], agreement["agreement_id"]):
            continue
        baseline = repo.baseline_price(product["net_uid"], row["uid"], "uk", True)
        cost = repo.unit_cost_eur(row["pid"])["unit_cost_eur"]
        if baseline and cost and baseline > cost * Decimal("1.12"):
            return row
    pytest.fail("No active discounted agreement/product with a positive priced supply lot")


@pytest.fixture(scope="module")
def fallback():
    from app.data.db import query
    from app.data import pricing_repository as repo
    from app.core.config import get_settings
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()
    rows = query("""
        SELECT TOP 100 p.ID AS pid, CONVERT(varchar(36), ca.NetUID) AS uid
        FROM dbo.ClientAgreement ca JOIN dbo.Agreement a ON a.ID=ca.AgreementID
        JOIN dbo.[Order] o ON o.ClientAgreementID=ca.ID
        JOIN dbo.Sale s ON s.OrderID=o.ID JOIN dbo.OrderItem oi ON oi.OrderID=o.ID
        JOIN dbo.Product p ON p.ID=oi.ProductID
        WHERE (a.IsActive=0 OR a.Deleted=1) AND p.Deleted=0 AND p.NetUID IS NOT NULL
          AND oi.IsValidForCurrentSale=1 AND oi.PricePerItem>0
          AND s.Created>=DATEADD(month,-6,:today) AND s.Created<:today
        GROUP BY p.ID,ca.NetUID ORDER BY p.ID
    """, {"today": today})
    for row in rows:
        product = repo.resolve_product(row["pid"], None)
        agreement = repo.resolve_client_agreement(row["uid"])
        if product and agreement and repo.baseline_price(product["net_uid"], row["uid"], "uk", True) is None:
            if repo.client_world_fallback_baseline(row["pid"], agreement["client_agreement_id"], today,
                                                   get_settings().trailing_window_months):
                return row
    pytest.skip("Live DB has no current product with a NULL engine price and same-world realized fallback")


@pytest.fixture(scope="module")
def contaminated():
    from app.data.db import query
    from app.core.config import get_settings
    rows = query("""
        SELECT TOP 1 ci.ProductID AS pid
        FROM dbo.ConsignmentItem ci JOIN dbo.Product p ON p.ID=ci.ProductID
        JOIN dbo.Consignment c ON c.ID=ci.ConsignmentID
        LEFT JOIN dbo.ProductIncome pi ON pi.ID=c.ProductIncomeID
        WHERE ci.Deleted=0 AND ci.AccountingPrice>0 AND ci.RemainingQty>0 AND p.Deleted=0
        GROUP BY ci.ProductID
        HAVING SUM(CASE WHEN pi.SourceDocumentType=:debt THEN 1 ELSE 0 END)>0
           AND SUM(CASE WHEN pi.SourceDocumentType IS NULL OR pi.SourceDocumentType<>:debt THEN 1 ELSE 0 END)>0
        ORDER BY ci.ProductID
    """, {"debt": get_settings().debt_import_source_document_type})
    if not rows:
        pytest.skip("Live DB has no live product with both debt-import and real on-hand lots; repository regression covers exclusion")
    return rows[0]["pid"]


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
            client_agreement_net_uid="00000000-0000-0000-0000-000000000001",
            use_cache=False,
        )


def test_live_product_id_and_uid_must_describe_one_identity(normal):
    from app.data import pricing_repository as repo

    from app.data.db import query
    product = repo.resolve_product(normal["pid"], None)
    other = query("SELECT TOP 1 NetUID AS uid FROM dbo.Product WHERE Deleted=0 AND NetUID IS NOT NULL AND ID<>:pid", {"pid": normal["pid"]})[0]
    assert repo.resolve_product(normal["pid"], product["net_uid"]) == product
    assert repo.resolve_product(normal["pid"], str(other["uid"])) is None


def test_null_engine_baseline_uses_same_world_fallback(fallback):
    """A live product with NULL engine baseline must use the positive, world-safe fallback."""
    from app.data import pricing_repository as repo
    from app.services.pricing import service

    product = repo.resolve_product(fallback["pid"], None)
    assert product is not None
    assert (
        repo.baseline_price(
            product["net_uid"],
            fallback["uid"],
            "uk",
            True,
        )
        is None
    )

    out = service.recommend_price(
        product_id=fallback["pid"],
        product_net_uid=None,
        client_agreement_net_uid=fallback["uid"],
        use_cache=False,
    )
    assert out.baseline_source == "client_world_fallback"
    assert out.baseline_price is not None and out.baseline_price > 0
    assert out.recommended_price is not None and out.recommended_price > 0


def test_normal_product_marked_up_reproduces_engine_baseline(normal):
    """A NORMAL product: marked_up*(1-applied_disc/100) reproduces the engine baseline, with a
    genuinely non-zero applied group discount. Guards the marked_up-from-baseline derivation and
    the active-discount lookup against the live engine."""
    from app.data import pricing_repository as repo
    from app.services.pricing.service import _marked_up_from_baseline

    product = repo.resolve_product(normal["pid"], None)
    assert product is not None
    agreement = repo.resolve_client_agreement(normal["uid"])
    assert agreement is not None

    baseline = repo.baseline_price(product["net_uid"], normal["uid"], "uk", True)
    assert baseline is not None and baseline > 0

    pg_id = repo.product_group_id(normal["pid"])
    assert pg_id is not None
    group_disc = repo.active_group_discount(agreement["client_agreement_id"], pg_id) or 0.0
    promo = repo.is_promotional(normal["pid"], agreement["agreement_id"])
    applied = 0.0 if promo else group_disc

    assert 0 < applied < 100

    marked_up = _marked_up_from_baseline(baseline, applied)
    assert marked_up is not None and marked_up > baseline
    applied_decimal = Decimal(str(applied))
    reproduced = marked_up * (Decimal(1) - applied_decimal / Decimal(100))
    assert abs(reproduced - baseline) < Decimal("0.000000001")


def test_normal_product_full_recommendation_is_sane(normal):
    """End-to-end NORMAL recommendation: a positive baseline, recommended in (0, baseline], with a
    discount that solves back to the recommended price through marked_up."""
    from app.services.pricing import service

    out = service.recommend_price(
        product_id=normal["pid"],
        product_net_uid=None,
        client_agreement_net_uid=normal["uid"],
        use_cache=False,
    )
    assert out.baseline_price is not None and out.baseline_price > 0
    if out.rationale == "constraints-conflict":
        assert out.recommended_price is None and out.suggested_discount_pct is None
        assert out.discount_band is None and out.confidence == "low"
    else:
        assert 0 < out.recommended_price <= out.baseline_price
        assert out.suggested_discount_pct == out.discount_band.target_pct
        from app.domain.money import round_cent
        expected = round_cent(Decimal(str(out.discount_base_price)) * (1 - Decimal(str(out.suggested_discount_pct)) / 100))
        assert abs(expected - Decimal(str(out.recommended_price))) <= Decimal("0.01")
    assert out.currency == "EUR"


def test_contaminated_cost_floor_not_inflated_by_debt_lots(contaminated):
    """A CONTAMINATED-cost product: the unit cost (and thus the margin floor) must come from real
    supply lots, NOT the 1С balance-import debt lots. Asserts the debt-excluded cost is sane
    and strictly below the naive median that includes the debt lot."""
    from app.data import pricing_repository as repo
    from app.data.db import query

    cost = repo.unit_cost_eur(contaminated)
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
        {"pid": contaminated},
    )
    naive_median = Decimal(str(naive[0]["naive_median"]))
    assert cost["unit_cost_eur"] < naive_median


def test_decimal_cost_floor_propagates_to_recommendation(normal):
    """End-to-end floor uses the unrounded Decimal aggregate, then rounds HALF_UP at the API."""
    from app.core.config import get_settings
    from app.data import pricing_repository as repo
    from app.domain.money import HUNDRED, as_decimal, round_cent
    from app.services.pricing import service

    raw_cost = repo.unit_cost_eur(normal["pid"])["unit_cost_eur"]
    assert raw_cost is not None
    out = service.recommend_price(
        product_id=normal["pid"],
        product_net_uid=None,
        client_agreement_net_uid=normal["uid"],
        use_cache=False,
    )
    assert out.unit_cost_eur is not None
    assert out.price_floor is not None
    margin = as_decimal(get_settings().target_margin_pct)
    expected_floor = round_cent(raw_cost * (Decimal(1) + margin / HUNDRED))
    assert Decimal(str(out.unit_cost_eur)) == round_cent(raw_cost)
    assert Decimal(str(out.price_floor)) == expected_floor
    assert out.baseline_price is not None and out.price_floor < out.baseline_price
