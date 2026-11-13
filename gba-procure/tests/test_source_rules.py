from __future__ import annotations

import inspect
from datetime import date, datetime

from app.data import cache, cost_repository, supply_repository
from app.data import source_rules as rules
from app.domain.models import MODEL_VERSION
from app.services import reconciliation
from app.services.eval import backtest


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def test_confirmed_sale_is_live_invoiced_and_not_an_accounting_carrier():
    predicate = rules.confirmed_sale("x")

    assert "x.Deleted = 0" in predicate
    assert "x.ChangedToInvoice IS NOT NULL" in predicate
    assert "NOT EXISTS" in predicate
    assert "carrier.TargetEntityType = N'Sale'" in predicate
    assert "carrier.TargetEntityID = x.ID" in predicate
    assert "carrier.IsCurrent = 1 AND carrier.OwnershipType = 1" in predicate


def test_confirmed_line_is_live_valid_and_positive():
    assert rules.confirmed_line("l") == (
        "l.Deleted = 0 AND l.IsValidForCurrentSale = 1 AND l.Qty > 0"
    )


def test_demand_is_dated_by_invoice_on_the_kyiv_day():
    lines = _normalized(rules.demand_lines())

    assert "s.ChangedToInvoice AS sold_at" in lines
    assert (
        "CAST(s.ChangedToInvoice AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS date) "
        "AS sold_day"
    ) in lines
    assert "s.ChangedToInvoice >= :demand_from" in lines
    assert "s.ChangedToInvoice < :demand_to" in lines
    assert rules.confirmed_sale("s") in lines
    assert rules.confirmed_line("oi") in lines
    assert "o.Created" not in lines


def test_demand_nets_returns_to_sellable_stock_known_before_the_cut_off():
    lines = _normalized(rules.demand_lines())

    assert "sri.OrderItemID = oi.ID" in lines
    assert rules.confirmed_return("sri", "sr") in lines
    assert "sr.FromDate < :demand_to" in lines
    assert rules.sellable_storage("rst") in lines
    assert "rst.ForDefective = 0" in lines
    assert "ELSE 0 END AS units" in lines


def test_import_placeholder_is_the_line_the_sync_writes():
    assert rules.import_placeholder_line("l") == (
        "l.Qty = 0 AND l.PricePerItem = 0 AND l.PricePerItemWithoutVat = 0 "
        "AND l.ExchangeRateAmount = 0 AND LTRIM(RTRIM(l.Comment)) = N'1С'"
    )
    assert rules.IMPORT_PLACEHOLDER_CAPTION == "1\u0421"
    assert rules.placeholder_return("i", "d", "l") == (
        "i.Deleted = 0 AND d.Deleted = 0 AND d.IsCanceled = 0 AND i.Qty > 0 "
        "AND d.SourceDocumentID IS NOT NULL AND " + rules.import_placeholder_line("l")
    )


def test_ordinary_returns_are_still_netted_on_the_returned_line_only():
    lines = _normalized(rules.sale_lines())

    assert "sri.OrderItemID = oi.ID" in lines
    assert rules.confirmed_line("oi") in lines
    assert "sr.FromDate < :demand_to" in lines
    assert "sr.FromDate >=" not in lines
    assert "SourceDocumentID" not in lines
    assert "Comment" not in lines
    assert "OVER (" not in lines
    assert rules.sale_lines() in rules.demand_lines()


def test_a_placeholder_return_is_booked_on_its_own_kyiv_day_when_it_went_to_sellable_stock():
    returns = _normalized(rules.placeholder_returns())

    assert (
        "CAST(sr.FromDate AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS date) "
        "AS returned_day"
    ) in returns
    assert "JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID" in returns
    assert "oi.ProductID AS product_id" in returns
    assert rules.placeholder_return("sri", "sr", "oi") in returns
    assert rules.sellable_storage("rst") in returns
    assert "sr.FromDate >= :demand_from" in returns
    assert "sr.FromDate < :demand_to" in returns
    assert "GROUP BY oi.ProductID, local.returned_day" in returns
    assert "dbo.Sale " not in returns


def test_a_product_day_absorbs_the_return_and_never_goes_below_zero():
    absorbed = _normalized(rules.absorbed_placeholder_returns())
    lines = _normalized(rules.demand_lines())

    assert "PARTITION BY g.product_id, g.returned_day ORDER BY l.sold_at, l.line_id" in absorbed
    assert (
        "CASE WHEN x.returned - x.prior >= x.units THEN x.units "
        "WHEN x.returned > x.prior THEN x.returned - x.prior ELSE 0 END AS units"
    ) in absorbed
    assert "l.product_id = g.product_id AND l.sold_at >= g.day_from AND l.sold_at < g.day_to" in absorbed
    assert "l.units - ISNULL(absorbed.units, 0) AS units" in lines
    assert ") absorbed ON absorbed.line_id = l.line_id ) d" in lines
    assert "UNION" not in lines


def test_demand_lines_support_the_batch_parameter_marker():
    lines = rules.demand_lines("@")

    assert "@demand_from" in lines
    assert "@demand_to" in lines
    assert ":demand" not in lines


def test_trailing_window_is_whole_kyiv_days_clamped_to_the_source_floor():
    assert rules.trailing_window("2026-07-25", 120) == {
        "demand_from": datetime(2026, 3, 26, 22, 0),
        "demand_to": datetime(2026, 7, 24, 21, 0),
    }
    assert rules.trailing_window(date(2025, 2, 1), 120) == {
        "demand_from": datetime(2024, 12, 31, 22, 0),
        "demand_to": datetime(2025, 1, 31, 22, 0),
    }


def test_forward_window_starts_at_the_as_of_kyiv_midnight():
    assert rules.forward_window("2026-10-20", 30) == {
        "demand_from": datetime(2026, 10, 19, 21, 0),
        "demand_to": datetime(2026, 11, 18, 22, 0),
    }


def test_sellable_storage_rule_is_shared_with_the_inventory_position():
    assert rules.sellable_storage("st") == supply_repository._SELLABLE_STORAGE
    assert rules.sellable_storage("st") == (
        "st.Deleted = 0 AND st.ForDefective = 0 "
        "AND (st.AvailableForReSale = 1 OR st.IsResale = 1)"
    )


def test_every_demand_reader_uses_the_shared_rule():
    readers = (
        supply_repository.product_daily_demand,
        supply_repository.product_daily_demand_bulk,
        supply_repository.procurement_source_readiness,
        supply_repository.all_products_revenue_eur,
        cost_repository.sale_prices_eur,
        reconciliation.collect_source_epoch,
        backtest._actual_demand,
    )
    for reader in readers:
        source = inspect.getsource(reader)
        assert "rules.demand_lines(" in source, reader.__name__
        assert "IsValidForCurrentSale" not in source, reader.__name__
        assert "o.Created" not in source, reader.__name__


def test_daily_demand_passes_window_instants(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        supply_repository, "query", lambda sql, params=None: captured.append((sql, params)) or []
    )
    monkeypatch.setattr(supply_repository, "synthetic_product_id", lambda: 999)

    supply_repository.product_daily_demand(7, "2026-07-25", 120)
    supply_repository.product_daily_demand_bulk([7, 8, 999], "2026-07-25", 120)

    assert captured[0][1] == {
        "pid": 7,
        "syn": 999,
        "demand_from": datetime(2026, 3, 26, 22, 0),
        "demand_to": datetime(2026, 7, 24, 21, 0),
    }
    assert captured[1][1] == {
        "syn": 999,
        "demand_from": datetime(2026, 3, 26, 22, 0),
        "demand_to": datetime(2026, 7, 24, 21, 0),
        "p0": 7,
        "p1": 8,
    }
    for sql, _ in captured:
        assert rules.demand_lines() in sql


def test_cache_and_model_version_were_bumped_with_the_definition():
    assert "v17-placeholder-returns" in cache.make_key("cart", "all", "2026-06-15")
    assert MODEL_VERSION.endswith("-v6-placeholder-returns")
