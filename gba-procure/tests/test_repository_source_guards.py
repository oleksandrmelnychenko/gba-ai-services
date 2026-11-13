"""Always-on source guards — assert the fixed SQL patterns survive in the repository module.

No DB, no Redis: these inspect the live source text of app.data.supply_repository so that
reintroducing a previously-shipped correctness bug fails CI immediately. Each bug that was
caught only by live smoke (mocked tests stayed green) gets a guard here.
"""

from __future__ import annotations

import inspect

import pytest

from app.data import source_rules as rules
from app.data import supply_repository as repo
from app.services.replenishment import worker


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_product_daily_demand_filters_valid_sales_and_excludes_synthetic():
    src = _src(repo.product_daily_demand)
    assert "rules.demand_lines()" in src
    assert "IsValidForCurrentSale = 1" in rules.demand_lines()
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
    for fn in (repo.producer_lead_times_all, repo.products_by_producer_all, repo.all_producers):
        src = _src(fn)
        assert "ClientID" in src, fn.__name__
        assert "OrganizationID" not in src, fn.__name__


def test_producer_lead_times_uses_datefrom_not_created_for_datediff():
    src = _src(repo.producer_lead_times_all)
    assert "DateFrom" in src
    assert "so.Created" not in src
    assert "SupplyOrder.Created" not in src


def test_producer_lead_times_uses_factual_invoice_to_receipt_lifecycle():
    src = _src(repo.producer_lead_times_all)
    assert "SupplyInvoiceOrderItem" in src
    assert "PackingListPackageOrderItem" in src
    assert "ProductIncomeItem" in src
    assert "ProductIncome" in src
    assert "si.DateFrom AS ordered_at" in src
    assert "COALESCE(si.DateFrom, so.DateFrom)" not in src
    assert "WHEN so.DateFrom > MAX(pinc.FromDate)" in src
    assert "GROUP BY so.ClientID, si.ID, si.DateFrom, so.DateFrom" in src
    assert "DATEDIFF(day, ordered_at, received_at)" in src
    assert "so.OrderArrivedDate" not in src
    assert "so.IsOrderArrived" not in src


def test_producer_lead_times_keeps_archived_parent_when_factual_rows_are_active():
    """Arrived SupplyOrder parents are archived (Deleted=1), while their invoice/receipt
    facts remain active. The history must gate on those facts, never the parent flag."""
    src = _src(repo.producer_lead_times_all)
    assert "si.Deleted = 0" in src
    assert "sioi.Deleted = 0" in src
    assert "pinc.Deleted = 0" in src
    assert "so.Deleted" not in src
    assert ":syn" in src


def test_producer_lead_times_unions_ua_receipt_history():
    src = _src(repo.producer_lead_times_all)
    assert "UNION ALL" in src
    assert "SupplyOrderUkraineItem" in src
    assert "pii.SupplyOrderUkraineItemID = soui.ID" in src
    assert "COALESCE(soui.SupplierID, sou.SupplierID)" in src


def test_candidate_windows_use_factual_document_dates_not_created_sync_stamp():
    """Candidates use SupplyInvoice.DateFrom / SupplyOrderUkraine.FromDate, never the
    SupplyOrder.Created sync stamp that is rewritten to ~now."""
    for fn in (repo.all_producers, repo.products_by_producer_all):
        src = _src(fn)
        assert "so.Created" not in src, fn.__name__
        assert "si.DateFrom AS source_date" in src, fn.__name__
        assert "sou.FromDate AS source_date" in src, fn.__name__
        assert "source_date >= DATEADD(day, -:days, :asof)" in src, fn.__name__
        assert "source_date < :asof" in src, fn.__name__


def test_candidates_use_active_invoice_facts_even_when_parent_is_archived():
    """After rekey, real products live in active invoice lines under SupplyOrder.Deleted=1."""
    for fn in (repo.all_producers, repo.products_by_producer_all):
        src = _src(fn)
        assert "dbo.SupplyInvoice si" in src, fn.__name__
        assert "dbo.SupplyInvoiceOrderItem sioi" in src, fn.__name__
        assert "si.Deleted = 0" in src, fn.__name__
        assert "sioi.Deleted = 0" in src, fn.__name__
        assert "so.ClientID" in src, fn.__name__
        assert "so.Deleted" not in src, fn.__name__
        assert "dbo.SupplyOrderItem soi" not in src, fn.__name__


def test_candidates_union_active_ua_real_product_lines():
    for fn in (repo.all_producers, repo.products_by_producer_all):
        src = _src(fn)
        assert "UNION ALL" in src, fn.__name__
        assert "dbo.SupplyOrderUkraine sou" in src, fn.__name__
        assert "dbo.SupplyOrderUkraineItem soui" in src, fn.__name__
        assert "sou.Deleted = 0" in src, fn.__name__
        assert "soui.Deleted = 0" in src, fn.__name__
        assert "COALESCE(soui.SupplierID, sou.SupplierID)" in src, fn.__name__


def test_candidates_include_real_lines_of_open_console_orders():
    for fn in (repo.all_producers, repo.products_by_producer_all, repo.procurement_source_readiness):
        src = _src(fn)
        assert "dbo.SupplyOrder co" in src, fn.__name__
        assert "dbo.SupplyOrderItem coi" in src, fn.__name__
        assert "coi.Deleted = 0" in src, fn.__name__
        assert "co.Deleted = 0" in src, fn.__name__
        assert "co.IsOrderArrived = 0" in src, fn.__name__
        assert "co.ClientID IS NOT NULL" in src, fn.__name__
        assert "co.DateFrom AS source_date" in src, fn.__name__
        assert "co.Created" not in src, fn.__name__


def test_console_order_candidates_share_the_on_order_predicate():
    on_order = _src(repo._on_order_chunk)
    assert "so.Deleted = 0 AND so.IsOrderArrived = 0" in on_order
    assert "soi.Deleted = 0" in on_order


@pytest.mark.order_units
def test_ukraine_order_price_is_read_as_stored_eur(monkeypatch):
    captured = []

    def fake_query(statement, params):
        captured.append(statement)
        return [
            {"producer_id": 7, "product_id": 1, "cost_eur": 12.5},
            {"producer_id": 7, "product_id": 2, "cost_eur": None},
        ]

    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 99)
    assert repo.last_document_costs_eur("2026-06-15", [1, 2]) == {(7, 1): 12.5}
    statement = " ".join(captured[0].split())
    international, ukraine = statement.split("UNION ALL")
    ukraine, ranked = ukraine.split("Ranked AS")
    assert "ISNULL(a.CurrencyID, 2) AS currency_id, CAST(0 AS bit) AS stored_in_eur" in international
    assert "soui.UnitPrice, CAST(NULL AS bigint), CAST(1 AS bit)," in ukraine
    assert "Agreement" not in ukraine
    assert "CurrencyID" not in ukraine
    assert (
        "CASE WHEN stored_in_eur = 1 THEN unit_price "
        "ELSE dbo.GetExchangedToEuroValue(unit_price, currency_id, source_date) END AS cost_eur"
    ) in ranked


def test_open_cockpit_draft_never_self_learns_as_factual_supply():
    """An AI-created draft is replaceable and not yet committed supplier stock.

    It must not feed itself back into candidates, costs, MOQ, lead-time geography,
    readiness or on-order before it becomes a placed document.
    """
    predicate = "NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)"
    for fn in (
        repo.producer_lead_times_all,
        repo.producer_agreement_currencies_all,
        repo.products_by_producer_all,
        repo.derive_moq_terms,
        repo.all_producers,
        repo.procurement_source_readiness,
        repo._on_order_chunk,
    ):
        assert predicate in _src(fn), fn.__name__


def test_worker_active_producers_uses_repository_source_path_not_created():
    src = _src(worker.active_producers)
    assert "repo.all_producers" in src
    assert "so.Created" not in src
    assert "SupplyOrder" not in src


def test_synthetic_product_is_excluded_across_candidate_queries():
    for fn in (repo.products_by_producer_all, repo.all_producers, repo._on_order_chunk):
        assert ":syn" in _src(fn), fn.__name__


def test_agreement_currency_uses_factual_intl_and_ua_documents():
    src = _src(repo.producer_agreement_currencies_all)
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


def test_on_order_reads_supplyorderitem_only_on_open_console_orders():
    """1C-imported orders carry placeholder SupplyOrderItem rows and arrive with
    IsOrderArrived = 1; only an open console order has real ordered lines."""
    src = _src(repo._on_order_chunk)
    assert "JOIN dbo.SupplyOrder so" in src
    assert "ON so.ID = soi.SupplyOrderID AND so.Deleted = 0 AND so.IsOrderArrived = 0" in src
    assert "soi.ProductID <> :syn" in src
    assert "so.DateFrom >= :history_start" in src
    assert "so.DateFrom < :asof" in src
    assert "so.Created" not in src


def test_on_order_counts_only_the_unpacked_part_of_an_open_order_line():
    """The packed part of a console order line is already on the packing-list spine."""
    src = _src(repo._on_order_chunk)
    assert "WHERE sioi.SupplyOrderItemID = soi.ID" in src
    assert "CASE WHEN soi.Qty > ISNULL(packed.qty, 0)" in src
    assert "THEN soi.Qty - ISNULL(packed.qty, 0) ELSE 0 END" in src


def test_on_order_scopes_every_spine_to_the_requested_products(monkeypatch):
    statements = []

    def fake_query(statement, params):
        statements.append(statement)
        return [{"pid": 5, "qty": 60}]

    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 99)
    assert repo._on_order_chunk([5, 6], "2026-06-15") == {5: 60.0}
    assert statements[0].count("soi.ProductID IN (") == 1
    assert statements[0].count("sioi.ProductID IN (") == 2
    assert repo._on_order_chunk(None, "2026-06-15") == {5: 60.0}
    assert " IN (" not in statements[1]


def test_reconciliation_on_order_mirrors_the_open_console_order_leg():
    from app.services import reconciliation

    src = _src(reconciliation._collect_on_order)
    assert "so.Deleted = 0 AND so.IsOrderArrived = 0" in src
    assert "WHERE sioi.SupplyOrderItemID = soi.ID" in src
    assert "WHERE line.open_qty > 0" in src


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


def test_on_order_reads_one_set_based_pass_without_in_list(monkeypatch):
    calls = []

    def fake_query(statement, params):
        calls.append((statement, params))
        return [{"pid": 5, "qty": 2}, {"pid": 6, "qty": 3}]

    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 99)
    assert repo.on_order([5, 99], "2026-06-15") == {5: 2.0}
    assert repo.on_order_all("2026-06-15") == {5: 2.0, 6: 3.0}
    assert len(calls) == 2
    for statement, params in calls:
        assert " IN (" not in statement
        assert set(params) == {"asof", "history_start", "syn"}


def test_set_based_readers_scope_one_producer_only_when_named(monkeypatch):
    captured = []

    def fake_query(statement, params):
        captured.append((statement, params))
        return []

    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 99)
    repo.producer_lead_times_all("2026-06-15")
    repo.producer_lead_times(7, "2026-06-15")
    repo.producer_agreement_currencies_all("2026-06-15")
    repo.producer_agreement_currency(7, "2026-06-15")
    repo.products_by_producer_all("2026-06-15", 120)
    repo.products_for_producer(7, "2026-06-15", 120)
    for index, (statement, params) in enumerate(captured):
        named = index % 2 == 1
        assert ("producer_id = :pid" in statement) is named
        assert ("producer_id IS NOT NULL" in statement) is not named
        assert (params.get("pid") == 7) is named


def test_set_based_readers_group_rows_like_the_single_producer_readers(monkeypatch):
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 99)
    monkeypatch.setattr(
        repo,
        "query",
        lambda statement, params: [
            {"producer_id": 1, "lead_days": 9},
            {"producer_id": 2, "lead_days": 4},
            {"producer_id": 1, "lead_days": 3},
        ],
    )
    assert repo.producer_lead_times_all("2026-06-15") == {1: [3.0, 9.0], 2: [4.0]}
    monkeypatch.setattr(
        repo,
        "query",
        lambda statement, params: [
            {"producer_id": 1, "ccy": 4, "n": 2},
            {"producer_id": 1, "ccy": 2, "n": 2},
            {"producer_id": 1, "ccy": 3, "n": 1},
            {"producer_id": 2, "ccy": 3, "n": 1},
        ],
    )
    assert repo.producer_agreement_currencies_all("2026-06-15") == {1: 2, 2: 3}
    monkeypatch.setattr(
        repo,
        "query",
        lambda statement, params: [
            {"producer_id": 1, "pid": 8},
            {"producer_id": 1, "pid": 5},
            {"producer_id": 2, "pid": 5},
        ],
    )
    assert repo.products_by_producer_all("2026-06-15", 120) == {1: [5, 8], 2: [5]}
    assert repo.products_for_producer(3, "2026-06-15", 120) == []


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


def test_cost_paths_do_not_execute_mutable_invoice_currency_udf():
    from app.data import cost_repository
    from app.services import reconciliation

    for module in (cost_repository, reconciliation):
        source = inspect.getsource(module)
        assert "GetExchangedToEuroValue" not in source
        if module is cost_repository:
            assert "sioi.UnitPrice" not in source and "soui.UnitPrice" not in source


def test_cost_repository_passes_exact_exclusive_asof_to_verified_cohort(monkeypatch):
    from native_cost_fixture import make_cohort

    from app.data import cost_repository

    cohort = make_cohort([1], as_of="2026-06-15", costs={(7, 1): ["1.0001", "1.0002"]})
    assert cost_repository.producer_unit_costs_eur(7, [1], "2026-06-15", cohort=cohort) == {1: 1.0002}
    import pytest

    from app.data.native_purchase_costs import NativePurchaseCostError

    with pytest.raises(NativePurchaseCostError):
        cost_repository.producer_unit_costs_eur(7, [1], "2026-06-16", cohort=cohort)


def test_on_order_drops_binary_float_noise_from_summed_quantities(monkeypatch):
    monkeypatch.setattr(repo, "query", lambda statement, params: [{"pid": 5, "qty": 400.19999999999993}])
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 99)
    assert repo.on_order_all("2026-06-15") == {5: 400.2}


def test_readiness_fingerprint_follows_open_console_order_lines():
    src = _src(repo.procurement_source_readiness)
    assert "SELECT CAST(4 AS tinyint), soi.ID, soi.ProductID, soi.Qty, soi.Updated" in src
    assert "ON so.ID = soi.SupplyOrderID AND so.Deleted = 0 AND so.IsOrderArrived = 0" in src
