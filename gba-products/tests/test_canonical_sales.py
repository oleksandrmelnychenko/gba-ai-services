from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import pytest

from app.core.history import kyiv_midnight_utc
from app.data import canonical_sales as canonical
from app.data import signals_repository as sig
from app.services import product_analytics


def test_generated_native_artifacts_are_unchanged_and_include_complete_currency_evidence():
    manifest = json.loads((canonical.ARTIFACTS / "manifest.json").read_text())
    for name, expected in manifest["artifacts"].items():
        assert hashlib.sha256((canonical.ARTIFACTS / name).read_bytes()).hexdigest() == expected
    proof = canonical.fragment("return_currency.sql")
    for required in ("SourcePayloadSha256", "HASHBYTES", "returnExpectedFinancial", "returnCurrentFinancial",
                     "SourceDocumentVersion", "EXCEPT", "COUNT_BIG(*)", "CurrencyValuation.ContractVersion"):
        assert required in proof
    assert "Qty *" not in canonical.fragment("return_value.sql")
    assert "sri.Amount" in canonical.fragment("return_value.sql")


def test_artifact_tampering_fails_closed(monkeypatch, tmp_path):
    for path in canonical.ARTIFACTS.iterdir():
        if path.is_file():
            (tmp_path / path.name).write_bytes(path.read_bytes())
    (tmp_path / "return_currency.sql").write_text("SELECT 1")
    monkeypatch.setattr(canonical, "ARTIFACTS", tmp_path)
    canonical.fragment.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="artifact_mismatch"):
            canonical.fragment("return_currency.sql")
    finally:
        canonical.fragment.cache_clear()


def test_all_monetary_queries_use_confirmed_native_population_and_never_guess_return_prices(monkeypatch):
    queries = []
    monkeypatch.setattr(sig, "query", lambda sql, params=None: queries.append(sql) or [])
    sig.sales_velocity("2026-10-24", 23)
    sig.avg_sale_price_eur("2026-10-24", 23)
    sig.monthly_units("2026-10-24", 12)
    sig.monthly_product_sales(42, "2026-10-01", "2026-10-24")
    sig.regional_demand_summary("2026-10-24", 23)
    sig.regional_product_sales("2026-10-24", 23)
    sig.returns_for_products("2026-10-24", 23)
    for sql in queries:
        for required in ("s.Deleted = 0", "s.ChangedToInvoice IS NOT NULL", "oi.Deleted = 0",
                         "ca.ID = s.ClientAgreementID", "s.EffectiveDate >= :history_start",
                         "accountingTarget.OwnershipType = 1", "TRY_CONVERT(decimal(16,8)"):
            assert required in sql
        for forbidden in ("o.Created", "IsValidForCurrentSale", "PricePerItem > 0", "<> :synth"):
            assert forbidden not in sql
    for sql in queries[-4:]:
        assert "sourceSale.MatchCount" in sql
        assert "sr.FromDate >= :history_start" in sql
        assert "returnCurrencyConfirmed" in sql
        assert "CAST(sri.Amount AS decimal(28,14))" in sql
        assert "SUM(returned_qty *" not in sql


def test_period_boundaries_use_kyiv_midnight_separately_across_dst():
    assert kyiv_midnight_utc("2026-07-01") == datetime(2026, 6, 30, 21)
    assert kyiv_midnight_utc("2026-01-01") == datetime(2025, 12, 31, 22)
    assert kyiv_midnight_utc("2026-03-30") - kyiv_midnight_utc("2026-03-29") == timedelta(hours=23)
    assert kyiv_midnight_utc("2026-10-26") - kyiv_midnight_utc("2026-10-25") == timedelta(hours=25)


def financial_row(**changes):
    return dict(revenue_eur=Decimal("10.005"), known_returned_value_eur=Decimal("20.001"),
                unresolved_amount_count=0, arithmetic_failures=0, ambiguous_lines=0) | changes


def test_net_can_be_negative_and_unknown_currency_preserves_partial_known_subtotal():
    complete = canonical.financial_rows([financial_row()])[0]
    assert complete["net_revenue_eur"] == Decimal("-9.996")
    unknown = canonical.financial_rows([financial_row(unresolved_amount_count=2)])[0]
    assert unknown["net_revenue_eur"] is None
    assert unknown["returned_value_eur"] is None
    assert unknown["known_net_revenue_eur"] == Decimal("-9.996")
    summary = canonical.money_summary([unknown])
    assert summary["total_net_revenue_eur"] is None
    assert summary["known_total_net_revenue_eur"] == -10.0
    assert summary["unresolved_amount_count"] == 2


@pytest.mark.parametrize("failure", ["arithmetic_failures", "ambiguous_lines"])
def test_ambiguous_return_or_inexact_source_never_becomes_a_zero_total(failure):
    with pytest.raises(ValueError):
        canonical.financial_rows([financial_row(**{failure: 1})])


def test_grand_total_is_rounded_once_from_exact_native_basis():
    summary = canonical.money_summary([
        financial_row(revenue_eur=Decimal("1.005"), known_returned_value_eur=Decimal(0)),
        financial_row(revenue_eur=Decimal("1.005"), known_returned_value_eur=Decimal(0)),
    ])
    assert summary["total_revenue_eur"] == 2.01


def test_return_only_month_preserves_negative_net_without_negative_demand():
    response = product_analytics.build_product_analytics(product_id=42, as_of="2026-10-24", months=1,
        model_version="fixture", snapshot={"product_id": 42}, monthly_rows=[{
            "ym": "2026-10", "units": 0, "order_count": 0, "revenue_eur": 0,
            "returned_units": 1, "known_returned_value_eur": Decimal("7.5"), "unresolved_amount_count": 0,
        }])
    point = response.sales_series[0]
    assert point.revenue_eur == 0
    assert point.units == 0
    assert point.net_revenue_eur == -7.5
    assert point.avg_price_eur is None
    assert response.snapshot["net_revenue_eur"] == -7.5


def test_monthly_unknown_return_currency_does_not_make_net_zero():
    response = product_analytics.build_product_analytics(product_id=42, as_of="2026-10-24", months=1,
        model_version="fixture", snapshot={"product_id": 42}, monthly_rows=[{
            "ym": "2026-10", "units": 1, "order_count": 1, "revenue_eur": 10,
            "returned_units": 1, "known_returned_value_eur": 3, "unresolved_amount_count": 1,
        }])
    assert response.sales_series[0].net_revenue_eur is None
    assert response.snapshot["net_revenue_eur"] is None
    assert response.snapshot["known_net_revenue_eur"] == 7


def test_region_normalization_retains_unknown_and_crimea_alias_in_one_bucket():
    sql = canonical.facts_cte(returns=True)
    assert "MIN(ID) OVER(PARTITION BY Name)" in sql
    assert "THEN N'KR'" in sql
    assert "c.RegionID IS NOT NULL" not in sql
    assert "region.CanonicalID AS region_id" in sql


def test_signed_gross_month_is_preserved_with_net_amount():
    result = product_analytics.build_product_analytics(
        product_id=42, as_of="2026-10-24", months=1, model_version="fixture", snapshot={"product_id": 42, "found": True},
        monthly_rows=[{"ym": "2026-10", "units": -2, "order_count": 1,
                       "revenue_eur": Decimal("-20.005"), "returned_units": 0,
                       "known_returned_value_eur": Decimal(0), "unresolved_amount_count": 0}])
    point = result.sales_series[0]
    assert point.revenue_eur == point.net_revenue_eur == -20.01
    assert point.units == -2


def test_tiny_exact_line_does_not_fail_a_rounded_display_identity():
    response = product_analytics.build_product_analytics(product_id=42, as_of="2026-10-24", months=1,
        model_version="fixture", snapshot={"product_id": 42}, monthly_rows=[{
            "ym": "2026-10", "units": Decimal("0.001"), "order_count": 1, "revenue_eur": Decimal("0.0005"),
            "returned_units": 0, "known_returned_value_eur": 0, "unresolved_amount_count": 0}])
    assert response.snapshot["revenue_eur"] == 0
    assert response.snapshot["avg_price_eur"] == 0.5
    assert response.snapshot["financial_basis"]["revenue_eur"] == "0.0005"


def test_region_filter_keeps_global_and_regional_uncertainty_separate():
    from app.api.main import _attach_regional_sales
    global_row = {"product_id": 42, "revenue_eur": 100, "net_revenue_eur": None,
                  "known_net_revenue_eur": 90, "unresolved_amount_count": 1}
    regional = {"product_id": 42, "region_id": None, "regional_units": 1,
                "regional_revenue_eur": 20, "regional_order_count": 1, "regional_client_count": 1,
                "regional_net_revenue_eur": 20, "regional_known_net_revenue_eur": 20,
                "unresolved_amount_count": 0}
    row = _attach_regional_sales([global_row], [regional])[0]
    assert row["unresolved_amount_count"] == 1
    assert row["net_revenue_eur"] is None
    assert row["regional_unresolved_amount_count"] == 0
    assert row["regional_net_revenue_eur"] == 20
