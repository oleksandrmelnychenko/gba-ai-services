"""Always-on source guards — assert the fixed SQL patterns survive in the repository module.

No DB, no Redis: these inspect the live source text of app.data.supply_repository so that
reintroducing a previously-shipped correctness bug fails CI immediately. Each bug that was
caught only by live smoke (mocked tests stayed green) gets a guard here.
"""
from __future__ import annotations

import inspect
from pathlib import Path

from app.data import supply_repository as repo


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_product_daily_demand_filters_valid_sales_and_excludes_synthetic():
    src = _src(repo.product_daily_demand)
    assert "IsValidForCurrentSale" in src
    assert "25422404" in src


def test_product_daily_demand_does_not_reference_order_deleted():
    src = _src(repo.product_daily_demand)
    assert "o.Deleted" not in src


def test_producer_name_reads_client_suppliername_not_organization():
    src = _src(repo.producer_name)
    assert "dbo.Client" in src
    assert "SupplierName" in src
    assert "dbo.Organization" not in src


def test_producer_dimension_keyed_on_clientid_not_organizationid():
    for fn in (repo.producer_lead_times, repo.products_for_producer, repo.all_producers):
        src = _src(fn)
        assert "ClientID" in src, fn.__name__
        assert "OrganizationID" not in src, fn.__name__


def test_producer_lead_times_uses_datefrom_not_created_for_datediff():
    src = _src(repo.producer_lead_times)
    assert "DateFrom" in src
    assert "so.Created" not in src
    assert "SupplyOrder.Created" not in src


def test_producer_lead_times_datediff_anchored_on_datefrom():
    src = _src(repo.producer_lead_times)
    assert "DATEDIFF(day, so.DateFrom, so.OrderArrivedDate)" in src


def test_synthetic_product_constant_is_excluded_across_candidate_queries():
    for fn in (repo.products_for_producer, repo.all_producers, repo.on_order):
        assert "25422404" in _src(fn), fn.__name__


def test_on_hand_and_reserved_restrict_to_sellable_storages():
    assert "ForEcommerce" in repo._SELLABLE_STORAGE
    assert "AvailableForReSale" in repo._SELLABLE_STORAGE
    assert "IsResale" in repo._SELLABLE_STORAGE
    for fn in (repo.on_hand, repo.reserved):
        src = _src(fn)
        assert "dbo.Storage" in src, fn.__name__
        assert "_SELLABLE_STORAGE" in src, fn.__name__


def test_derive_moq_terms_uses_min_qty_and_min_orders():
    src = _src(repo.derive_moq_terms)
    assert "MIN(soi.Qty)" in src
    assert "HAVING COUNT(*) >= :n" in src
    assert "PackingStandard" in src
    assert "25422404" in src


def test_cost_repository_converts_unitprice_to_eur_via_agreement_currency():
    from app.data import cost_repository as cost_repo
    src = _src(cost_repo._fetch_cost_rows)
    assert "GetExchangedToEuroValue" in src
    assert "ClientAgreement" in src
    assert "a.CurrencyID" in src
    assert "soi.UnitPrice" in src
    assert "25422404" in src


def test_makefile_exposes_backtest_calibration_gate():
    makefile = Path(__file__).resolve().parents[1] / "Makefile"
    src = makefile.read_text(encoding="utf-8")
    assert "backtest:" in src
    assert "procure_backtest_sweep.py" in src
    assert "calibration: backtest" in src
