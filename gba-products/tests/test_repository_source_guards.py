"""Source-guard tests — encode the load-bearing data hazards as assertions over the repo source,
so a future edit that reintroduces them fails CI (no DB needed)."""
from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent / "app" / "data" / "signals_repository.py").read_text()


def test_uses_order_created_not_orderitem_created():
    # time windows must key off Order.Created (oi.Created is truncated to ~3 days on this data)
    assert "o.Created" in _SRC
    assert "oi.Created" not in _SRC


def test_never_converts_already_eur_sale_price():
    # OrderItem.PricePerItem is already EUR — must not be wrapped in a currency conversion
    assert "GetExchangedToEuroValue" not in _SRC


def test_all_load_bearing_quantity_and_money_aggregates_cast_before_arithmetic():
    # Qty/Amount are legacy SQL float columns. Leaving any of these expressions raw reintroduces
    # binary-float accumulation and can move an accounting half-cent.
    assert "SUM(oi.Qty)" not in _SRC
    assert "SUM(pa.Amount)" not in _SRC
    assert "SUM(sri.Qty)" not in _SRC
    assert "oi.Qty * oi.PricePerItem" not in _SRC
    assert "ci.RemainingQty * ci.AccountingPrice" not in _SRC
    assert "_SALE_AMOUNT = f" in _SRC


def test_price_and_regional_queries_return_decimal_revenue_aggregates():
    price = _function_source("avg_sale_price_eur")
    assert "SUM({_SALE_AMOUNT}) AS revenue_eur" in price
    assert "SUM({_SALE_QTY}) AS sold_qty" in price
    assert "CAST(revenue_eur / NULLIF(sold_qty, 0) AS decimal(28, 8))" in price

    regional_product = _function_source("regional_product_sales")
    regional_summary = _function_source("regional_demand_summary")
    assert "SUM({_SALE_AMOUNT}) AS regional_revenue_eur" in regional_product
    assert "SUM({_SALE_QTY}) AS regional_units" in regional_product
    assert "SUM({_SALE_AMOUNT}) AS revenue_eur" in regional_summary
    assert "SUM({_SALE_QTY}) AS units" in regional_summary


def _function_source(name: str) -> str:
    start = _SRC.index(f"def {name}")
    end = _SRC.find("\ndef ", start + 1)
    return _SRC[start:] if end == -1 else _SRC[start:end]


def _sql_after_docstring(name: str) -> str:
    return "".join(_function_source(name).split('"""')[2:])


def test_stock_quantity_is_exact_productavailability_amount_without_reservation_replay():
    sql = _sql_after_docstring("on_hand_stock")
    assert "FROM dbo.ProductAvailability pa" in sql
    assert "SUM({_PA_QTY}) AS qty_on_hand" in sql
    assert '_PA_QTY = "CAST(pa.Amount AS decimal(18, 8))"' in _SRC
    assert "dbo.ReSaleAvailability" not in sql
    assert "dbo.ProductReservation" not in sql
    assert "pa.Amount +" not in sql
    assert "pa.Amount -" not in sql


def test_eur_value_uses_accounting_eur_cost_without_fx_conversion():
    sql = _sql_after_docstring("on_hand_stock")
    assert "SUM({_COST_QTY} * {_COST_PRICE}) AS cost_value_eur" in sql
    assert '_COST_QTY = "CAST(ci.RemainingQty AS decimal(18, 8))"' in _SRC
    assert '_COST_PRICE = "CAST(ci.AccountingPrice AS decimal(28, 14))"' in _SRC
    assert "GetExchangedToEuroValue" not in sql
    assert "ExchangeRate" not in sql
    assert "PricePerItem" not in sql


def test_stock_scope_excludes_defective_and_requires_resale():
    sql = _sql_after_docstring("on_hand_stock")
    assert "_SELLABLE_STORAGE" in sql
    assert "s.ForDefective = 0" in _SRC
    assert "s.AvailableForReSale = 1 OR s.IsResale = 1" in _SRC


def test_stock_excludes_1c_debt_import_lots():
    # 1С debt-import lots (dbo.ProductIncome.SourceDocumentType = 1) carry an inflated balance-import
    # AccountingPrice (== ci.Price, ~55x real cost) on BOTH IsImportedFromOneC and IsVirtual lots, so
    # neither Consignment flag isolates them. They otherwise ~3x the on-hand EUR value (€985k vs €323k
    # real, 67.2% contamination) and overstate the unit_cost the margin layer derives (eur_value/qty)
    # up to 11.5x. The cost CTE MUST join the lot's ProductIncome via Consignment and exclude
    # SourceDocumentType=1 — mirroring gba-pricing unit_cost_eur. A lot with no ProductIncome (pure
    # transfer) is kept (pi.ID IS NULL). The filter must be parameterized, not a literal.
    sql = _sql_after_docstring("on_hand_stock")
    assert "dbo.Consignment" in sql  # ci -> Consignment FK hop
    assert "ci.ConsignmentID" in sql
    assert "dbo.ProductIncome" in sql  # Consignment -> ProductIncome FK hop
    assert "c.ProductIncomeID" in sql
    assert "pi.SourceDocumentType <> :debt_doc_type" in sql
    # mirror gba-pricing's exact intent: the debt document type is 1 and parameter-bound.
    assert "_DEBT_IMPORT_SOURCE_DOCUMENT_TYPE = 1" in _SRC


def test_stock_readiness_detects_populated_global_but_empty_sellable_scope():
    ready = _function_source("_stock_readiness_reason")
    assert "global_availability_row_count" in ready
    assert "global_available_qty" in ready
    assert "role_marked_storage_count" in ready
    assert "sellable_availability_row_count" in ready
    assert "sellable_available_qty" in ready


def test_windows_are_parameterized():
    assert ":asof" in _SRC
    assert ":source_history_start" in _SRC
    assert ":history_start" in _SRC
    assert "DATEADD(day, -:win, :asof)" not in _SRC


def _returns_fn() -> str:
    """The body of returns_for_products (so guards target the returns query specifically)."""
    return _function_source("returns_for_products")


def test_returns_window_on_fromdate_not_sync_created():
    # SaleReturn.Created (and SaleReturnItem.Created) is a 1С-sync MIRROR timestamp; the real
    # return date is SaleReturn.FromDate. Windowing on sr.Created silently mis-dates every return.
    body = _returns_fn()
    assert "sr.FromDate" in body
    assert "sr.Created" not in body


def test_returns_sum_canonical_item_qty_preserving_partial_and_multiple_rows():
    # gba-server persists SaleReturnItem.Qty and groups SUM(Qty) by OrderItemID. Grouping the
    # return/order key prevents price/flag multiplication, while SUM preserves partial quantities
    # and multiple active return rows. MAX(OrderItem.Qty) would replace both with the sold quantity.
    parts = _returns_fn().split('"""')  # [code, docstring, code-with-f-string-SQL, sql, tail]
    sql = "".join(parts[2:])  # everything after the docstring (the f-string SQL + trailers)
    assert "SUM({_RETURN_QTY}) AS returned_qty" in sql
    assert "SUM(returned_qty) AS returned_qty" in sql
    assert "GROUP BY oi.ProductID, sri.SaleReturnID, sri.OrderItemID" in sql
    assert '_RETURN_QTY = "CAST(sri.Qty AS decimal(18, 8))"' in _SRC
    assert "MAX(oi.Qty)" not in sql
    assert "sri.Amount" not in sql
    assert "oi.ReturnedQty" not in sql


def test_returns_honor_active_set_and_exclude_synthetic():
    body = _returns_fn()
    assert "sr.Deleted = 0" in body and "sr.IsCanceled = 0" in body
    assert "oi.ProductID <> :synth" in body  # exclude the synthetic debt-entry product


def test_returns_do_not_filter_deleted_order_lines():
    # processing a return marks the original sale line Deleted=1 (~73% of returned lines), so the
    # returns query must NOT join oi.Deleted = 0 (that dropped most real returns).
    assert "oi.Deleted = 0" not in _returns_fn()


def _spine_fns() -> str:
    """The four Sale/Order/OrderItem-spine queries (everything that is NOT returns_for_products)."""
    src = _SRC
    start = src.index("def sold_product_ids")
    end = src.index("def returns_for_products")
    head = src[start:end]
    m_start = src.index("def monthly_units")
    m_end = src.index("\ndef ", m_start)
    return head + src[m_start:m_end]


def test_sales_spine_uses_validity_flag_not_deleted():
    # dbo.[Order]/OrderItem are ~80%/84% Deleted=1 in this 1С-synced DB (the sync flips Deleted on
    # every superseded revision), so o.Deleted=0 AND oi.Deleted=0 keeps only ~16% of real sale lines
    # and undercounts every sales-based signal ~3.5x. Validity = oi.IsValidForCurrentSale = 1.
    spine = _spine_fns()
    assert "o.Deleted = 0" not in spine
    assert "oi.Deleted = 0" not in spine
    # all four spine queries must gate on the validity flag (sold_ids, velocity, price, monthly)
    assert spine.count("oi.IsValidForCurrentSale = 1") == 4


def test_aggregating_spine_excludes_synthetic_debt_entry():
    # The synthetic "Ввід боргів" product (dynamically resolved; the 1С sync re-mints its row so
    # a hardcoded ID goes stale) is a debt-injection line that ranks #1
    # by revenue (~€7.4M) and would pollute velocity / avg sale price / monthly units. The three
    # AGGREGATING spine queries must exclude it; sold_product_ids is only a membership set (never
    # aggregated) so it is intentionally left without the filter.
    spine = _spine_fns()
    assert spine.count("oi.ProductID <> :synth") == 3
    # sold_product_ids must NOT carry the synthetic filter (it is a set, not an aggregate)
    src = _SRC
    sold = src[src.index("def sold_product_ids"):src.index("def sales_velocity")]
    assert "<> :synth" not in sold


def test_synthetic_debt_product_id_resolved_dynamically_not_hardcoded():
    # The 1С sync re-mints the «Ввід боргів» Product row, so any hardcoded ID (e.g. the dead
    # 25422404) silently stops filtering after a re-mint. The ID must be resolved from the live
    # table by name (latest non-deleted row wins).
    assert "25422404" not in _SRC
    assert "N'Ввід боргів'" in _SRC
    assert "ORDER BY ID DESC" in _SRC
    assert 'def synthetic_product_id' in _SRC


def test_product_monthly_analytics_uses_canonical_sales_spine_and_actual_eur_revenue():
    start = _SRC.index("def monthly_product_sales")
    end = _SRC.index("\ndef ", start)
    body = _SRC[start:end]

    assert "oi.IsValidForCurrentSale = 1" in body
    assert "_SALES_HISTORY_WINDOW" in body
    assert "o.Created >= :source_history_start" in _SRC
    assert "o.Created >= :history_start" in _SRC
    assert "o.Created < :asof" in _SRC
    assert "oi.ProductID = :product_id" in body
    assert "oi.ProductID <> :synth" in body
    assert "COUNT(DISTINCT o.ID) AS order_count" in body
    assert "SUM({_SALE_AMOUNT}) AS revenue_eur" in body
    assert "CAST(revenue_eur / NULLIF(units, 0) AS decimal(28, 8))" in body
    assert '_SALE_QTY = "CAST(oi.Qty AS decimal(18, 8))"' in _SRC
    assert '_SALE_PRICE = "CAST(oi.PricePerItem AS decimal(28, 14))"' in _SRC
    assert "GetExchangedToEuroValue" not in body


def test_regional_demand_uses_client_region_id_not_region_code():
    start = _SRC.index("def regional_product_sales")
    end = _SRC.index("def regional_demand_summary")
    body = _SRC[start:end]
    assert "dbo.ClientAgreement" in body
    assert "ca.ID = o.ClientAgreementID" in body
    assert "c.ID = ca.ClientID" in body
    assert "c.RegionID" in body
    sql = "".join(body.split('"""')[2:])
    assert "RegionCodeID" not in sql
    assert "oi.IsValidForCurrentSale = 1" in body
    assert "_SALES_HISTORY_WINDOW" in body
    assert "oi.ProductID <> :synth" in body


def test_latest_producer_uses_factual_invoice_and_ukraine_item_spines():
    body = _function_source("product_meta")
    assert "dbo.SupplyInvoiceOrderItem" in body
    assert "sioi.SupplyInvoiceID = si.ID" in body
    assert "si.DateFrom AS source_date" in body
    assert "dbo.SupplyOrderUkraineItem" in body
    assert "COALESCE(soui.SupplierID, sou.SupplierID)" in body
    assert "sou.FromDate AS source_date" in body
    assert "sou.IsFromCockpit = 1 AND sou.IsPlaced = 0" in body
    assert "si.DateFrom >= :source_history_start" in body
    assert "si.DateFrom < :asof" in body
    assert "sou.FromDate >= :source_history_start" in body
    assert "sou.FromDate < :asof" in body
    assert "JOIN dbo.SupplyOrderItem soi" not in body
