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
    assert ":syn" in src


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


def test_producer_lead_times_uses_factual_invoice_to_receipt_lifecycle():
    src = _src(repo.producer_lead_times)
    assert "SupplyInvoiceOrderItem" in src
    assert "PackingListPackageOrderItem" in src
    assert "ProductIncomeItem" in src
    assert "ProductIncome" in src
    assert "COALESCE(si.DateFrom, so.DateFrom)" in src
    assert "DATEDIFF(day, ordered_at, received_at)" in src
    assert "so.OrderArrivedDate" not in src
    assert "so.IsOrderArrived" not in src


def test_producer_lead_times_keeps_archived_parent_when_factual_rows_are_active():
    """Arrived SupplyOrder parents are archived (Deleted=1), while their invoice/receipt
    facts remain active. The history must gate on those facts, never the parent flag."""
    src = _src(repo.producer_lead_times)
    assert "si.Deleted = 0" in src
    assert "sioi.Deleted = 0" in src
    assert "pinc.Deleted = 0" in src
    assert "so.Deleted" not in src
    assert ":syn" in src


def test_producer_lead_times_unions_ua_receipt_history():
    src = _src(repo.producer_lead_times)
    assert "UNION ALL" in src
    assert "SupplyOrderUkraineItem" in src
    assert "pii.SupplyOrderUkraineItemID = soui.ID" in src
    assert "COALESCE(soui.SupplierID, sou.SupplierID)" in src


def test_candidate_windows_use_factual_document_dates_not_created_sync_stamp():
    """Candidates use SupplyInvoice.DateFrom / SupplyOrderUkraine.FromDate, never the
    SupplyOrder.Created sync stamp that is rewritten to ~now."""
    for fn in (repo.all_producers, repo.products_for_producer):
        src = _src(fn)
        assert "so.Created" not in src, fn.__name__
        assert "si.DateFrom AS source_date" in src, fn.__name__
        assert "sou.FromDate AS source_date" in src, fn.__name__
        assert "source_date >= DATEADD(day, -:days, :asof)" in src, fn.__name__
        assert "source_date < :asof" in src, fn.__name__


def test_candidates_use_active_invoice_facts_even_when_parent_is_archived():
    """After rekey, real products live in active invoice lines under SupplyOrder.Deleted=1."""
    for fn in (repo.all_producers, repo.products_for_producer):
        src = _src(fn)
        assert "dbo.SupplyInvoice si" in src, fn.__name__
        assert "dbo.SupplyInvoiceOrderItem sioi" in src, fn.__name__
        assert "si.Deleted = 0" in src, fn.__name__
        assert "sioi.Deleted = 0" in src, fn.__name__
        assert "so.ClientID" in src, fn.__name__
        assert "so.Deleted" not in src, fn.__name__
        assert "dbo.SupplyOrderItem soi" not in src, fn.__name__


def test_candidates_union_active_ua_real_product_lines():
    for fn in (repo.all_producers, repo.products_for_producer):
        src = _src(fn)
        assert "UNION ALL" in src, fn.__name__
        assert "dbo.SupplyOrderUkraine sou" in src, fn.__name__
        assert "dbo.SupplyOrderUkraineItem soui" in src, fn.__name__
        assert "sou.Deleted = 0" in src, fn.__name__
        assert "soui.Deleted = 0" in src, fn.__name__
        assert "COALESCE(soui.SupplierID, sou.SupplierID)" in src, fn.__name__


def test_open_cockpit_draft_never_self_learns_as_factual_supply():
    """An AI-created draft is replaceable and not yet committed supplier stock.

    It must not feed itself back into candidates, costs, MOQ, lead-time geography,
    readiness or on-order before it becomes a placed document.
    """
    from app.data import cost_repository as cost_repo

    predicate = "NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)"
    for fn in (
        repo.producer_lead_times,
        repo.producer_agreement_currency,
        repo.products_for_producer,
        repo.derive_moq_terms,
        repo.all_producers,
        repo.procurement_source_readiness,
        repo._on_order_chunk,
        cost_repo._fetch_cost_rows,
    ):
        assert predicate in _src(fn), fn.__name__


def test_worker_active_producers_uses_repository_source_path_not_created():
    src = _src(worker.active_producers)
    assert "repo.all_producers" in src
    assert "so.Created" not in src
    assert "SupplyOrder" not in src


def test_synthetic_product_is_excluded_across_candidate_queries():
    for fn in (repo.products_for_producer, repo.all_producers, repo._on_order_chunk):
        assert ":syn" in _src(fn), fn.__name__


def test_agreement_currency_uses_factual_intl_and_ua_documents():
    src = _src(repo.producer_agreement_currency)
    assert "dbo.SupplyInvoice si" in src
    assert "dbo.SupplyInvoiceOrderItem sioi" in src
    assert "si.Deleted = 0" in src
    assert "sioi.Deleted = 0" in src
    assert "so.Deleted" not in src
    assert "dbo.SupplyOrderUkraineItem soui" in src
    assert "sou.Deleted = 0" in src
    assert "soui.Deleted = 0" in src
    assert ":syn" in src


def test_synthetic_product_id_resolved_dynamically_not_hardcoded():
    """The synthetic debt product («Ввід боргів») is periodically re-minted under a NEW ID
    (25422404 died; 29555414 is a later mint) -- repositories must exclude the dynamically
    resolved ID, never a hardcoded literal, or every re-mint silently poisons the plans."""
    import app.data.cost_repository as cost_module
    import app.data.supply_repository as supply_module
    from app.data import synthetic

    for mod in (supply_module, cost_module):
        src = inspect.getsource(mod)
        assert "25422404" not in src, mod.__name__
        assert "29555414" not in src, mod.__name__
        assert "synthetic_product_id" in src, mod.__name__
    resolver = inspect.getsource(synthetic)
    assert "Ввід боргів" in resolver
    assert "Deleted = 0" in resolver
    assert "ORDER BY ID DESC" in resolver
    assert synthetic.FALLBACK_SYNTHETIC_PRODUCT_ID == 29555414


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
    assert "ForEcommerce" not in repo._SELLABLE_STORAGE
    assert "AvailableForReSale" in repo._SELLABLE_STORAGE
    assert "IsResale" in repo._SELLABLE_STORAGE
    assert "st.Deleted = 0" in repo._SELLABLE_STORAGE
    assert "st.ForDefective = 0" in repo._SELLABLE_STORAGE
    for fn in (repo.on_hand, repo.reserved):
        src = _src(fn)
        assert "dbo.Storage" in src, fn.__name__
        assert "_SELLABLE_STORAGE" in src, fn.__name__


def test_on_hand_reconstructs_gross_without_double_subtracting_reservations():
    src = _src(repo.on_hand)
    assert "ReservedByAvailability" in src
    assert "pa.Amount + ISNULL(r.qty, 0)" in src
    assert "ProductReservation" in src


def test_source_readiness_covers_each_required_input_and_source_epoch():
    src = _src(repo.procurement_source_readiness)
    for table in (
        "SupplyInvoiceOrderItem",
        "SupplyOrderUkraineItem",
        "OrderItem",
        "ProductAvailability",
        "Storage",
    ):
        assert table in src
    assert "_SELLABLE_STORAGE" in src
    assert "source_fingerprint" in src
    assert "max_candidate_availability_id" in src
    assert "latest_candidate_availability_update" in src
    assert "supply_checksum" in src
    assert "demand_checksum" in src
    assert "availability_checksum" in src
    assert "reservation_checksum" in src
    assert "flow_checksum" in src
    assert "exchange_rate_history_checksum" in src


def test_source_readiness_reports_lost_storage_roles_before_empty_output():
    reason = repo._source_readiness_reason(
        {
            "global_available_qty": 100,
            "role_marked_storage_count": 0,
            "producer_count": 2,
            "product_count": 10,
            "demand_product_count": 8,
            "inventory_product_count": 0,
            "cost_product_count": 6,
        }
    )
    assert reason == "storage_roles_missing"


def test_derive_moq_terms_uses_min_qty_and_min_orders():
    src = _src(repo.derive_moq_terms)
    assert "DocumentProductQty" in src
    assert "GROUP BY source_kind, document_id, producer_id, product_id" in src
    assert "MIN(dpq.qty)" in src
    assert "HAVING COUNT(*) >= :n" in src
    assert "PackingStandard" in src
    assert ":syn" in src


def test_derive_moq_terms_uses_real_invoice_and_ua_lines_not_placeholder_items():
    src = _src(repo.derive_moq_terms)
    assert "dbo.SupplyInvoice si" in src
    assert "dbo.SupplyInvoiceOrderItem sioi" in src
    assert "si.Deleted = 0" in src
    assert "sioi.Deleted = 0" in src
    assert "so.Deleted" not in src
    assert "UNION ALL" in src
    assert "dbo.SupplyOrderUkraineItem soui" in src
    assert "sou.Deleted = 0" in src
    assert "soui.Deleted = 0" in src
    assert "dbo.SupplyOrderItem soi" not in src


def test_cost_repository_converts_unitprice_to_eur_via_agreement_currency():
    from app.data import cost_repository as cost_repo
    src = _src(cost_repo._fetch_cost_rows)
    assert "GetExchangedToEuroValue" in src
    assert "ClientAgreement" in src
    assert "a.CurrencyID" in src
    assert "sioi.UnitPrice" in src
    assert "soui.UnitPrice" in src
    assert ":syn" in src


def test_cost_repository_windows_trailing_cost_on_datefrom_not_created():
    """Costs use the factual international/UA document dates, never the parent sync stamp."""
    from app.data import cost_repository as cost_repo
    src = _src(cost_repo._fetch_cost_rows)
    assert "si.DateFrom >= DATEADD(day, -:days, :asof)" in src
    assert "si.DateFrom < :asof" in src
    assert "sou.FromDate >= DATEADD(day, -:days, :asof)" in src
    assert "sou.FromDate < :asof" in src
    assert "so.Created" not in src
