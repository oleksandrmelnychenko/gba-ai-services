"""Always-on source guards — assert the fixed SQL patterns survive in the repository module.

No DB, no Redis: these inspect the live source text of app.data.supply_repository so that
reintroducing a previously-shipped correctness bug fails CI immediately. Each bug that was
caught only by live smoke (mocked tests stayed green) gets a guard here.
"""
from __future__ import annotations

import inspect

from app.data import supply_repository as repo
from app.services.replenishment import worker


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


def test_candidate_windows_use_datefrom_not_created_sync_stamp():
    """all_producers / products_for_producer must window on so.DateFrom (real placement date),
    NOT so.Created (the 1C-sync stamp rewritten to ~now). Created has only a handful of distinct
    dates all near now, so a Created window is non-discriminating: it returns EVERY producer/
    product regardless of lookback instead of just those that ordered within the window."""
    for fn in (repo.all_producers, repo.products_for_producer):
        src = _src(fn)
        assert "so.Created" not in src, fn.__name__
        assert "so.DateFrom >= DATEADD(day, -:days, :asof)" in src, fn.__name__


def test_worker_active_producers_uses_repository_source_path_not_created():
    src = _src(worker.active_producers)
    assert "repo.all_producers" in src
    assert "so.Created" not in src
    assert "SupplyOrder" not in src


def test_synthetic_product_constant_is_excluded_across_candidate_queries():
    for fn in (repo.products_for_producer, repo.all_producers, repo._on_order_chunk):
        assert "25422404" in _src(fn), fn.__name__


def test_on_order_does_not_source_from_synthetic_supplyorderitem_placeholder():
    """on_order MUST NOT read SupplyOrderItem (synthetic placeholder for not-yet-arrived orders)
    nor filter on SupplyOrder.Created (the 1C-sync stamp ~now) -- those made it always empty."""
    src = _src(repo._on_order_chunk)
    assert "SupplyOrderItem" not in src           # synthetic-placeholder table
    assert "so.Created" not in src                # rewritten sync timestamp
    assert "IsOrderArrived" not in src            # boolean had no per-item received granularity


def test_on_order_reconstructs_open_minus_received_over_real_product_spine():
    """on_order = ordered(SupplyInvoiceOrderItem real ProductID) - received(ProductIncome),
    point-in-time on the REAL historical date columns (DateFrom / FromDate), netted >0."""
    src = _src(repo._on_order_chunk)
    # ordered side: packing-list spine carries the real product
    assert "PackingListPackageOrderItem" in src
    assert "SupplyInvoiceOrderItem" in src
    assert "si.DateFrom < :asof" in src
    # received side: ProductIncome netting on its real receipt date
    assert "ProductIncomeItem" in src
    assert "ProductIncome " in src or "dbo.ProductIncome\n" in src
    assert "pinc.FromDate < :asof" in src
    # ukraine spine also covered
    assert "SupplyOrderUkraineItem" in src
    # open = ordered minus received, clamped positive
    assert "ISNULL(r.qty, 0)" in src
    assert "> 0.001" in src


def test_on_order_chunks_in_list_under_param_cap():
    """The IN list is referenced 4x in one statement; on_order must chunk to stay under
    MSSQL's 2100-param cap rather than passing the whole product set in one shot."""
    src = _src(repo.on_order)
    assert "_ON_ORDER_IN_CHUNK" in src
    assert repo._ON_ORDER_IN_CHUNK * 4 < 2100


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


def test_cost_repository_windows_trailing_cost_on_datefrom_not_created():
    """_fetch_cost_rows must window the trailing-cost lookback on the REAL placement date
    (ISNULL(so.DateFrom, so.Created)), NOT raw so.Created -- the 1C-sync stamp is rewritten to
    ~now, so a Created window is a no-op that admits EVERY supply line-item (incl. orders years
    old) instead of just those within the lookback. Mirrors the FX-date ISNULL on DateFrom."""
    from app.data import cost_repository as cost_repo
    src = _src(cost_repo._fetch_cost_rows)
    assert "ISNULL(so.DateFrom, so.Created) >= DATEADD(day, -:days, :asof)" in src
    assert "ISNULL(so.DateFrom, so.Created) < :asof" in src
    assert "AND so.Created >= DATEADD" not in src
