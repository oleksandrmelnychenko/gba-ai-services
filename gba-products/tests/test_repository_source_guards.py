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


def test_eur_value_uses_consignment_price_directly_no_fx_divide():
    # ci.Price is ALREADY EUR (== ci.AccountingPrice on every on-hand lot; gba-pricing uses
    # AccountingPrice with no conversion). Dividing by rsa.ExchangeRate mis-scaled the EUR value ~50x.
    # The EUR value must be RemainingQty * ci.Price with NO /ExchangeRate and NOT the UAH PricePerItem.
    assert "rsa.RemainingQty * ci.Price" in _SRC
    assert "ci.Price / NULLIF(rsa.ExchangeRate" not in _SRC
    assert "rsa.PricePerItem" not in _SRC


def test_stock_scope_excludes_defective_and_requires_resale():
    assert "s.ForDefective = 0" in _SRC
    assert "s.AvailableForReSale = 1 OR s.IsResale = 1" in _SRC


def test_stock_excludes_1c_debt_import_lots():
    # 1С debt-import lots (dbo.ProductIncome.SourceDocumentType = 1) carry an inflated balance-import
    # AccountingPrice (== ci.Price, ~55x real cost) on BOTH IsImportedFromOneC and IsVirtual lots, so
    # neither Consignment flag isolates them. They otherwise ~3x the on-hand EUR value (€985k vs €323k
    # real, 67.2% contamination) and overstate the unit_cost the margin layer derives (eur_value/qty)
    # up to 11.5x. The stock FROM block MUST join the lot's ProductIncome via Consignment and exclude
    # SourceDocumentType=1 — mirroring gba-pricing unit_cost_eur. A lot with no ProductIncome (pure
    # transfer) is kept (pi.ID IS NULL). The filter must be parameterized, not a literal.
    assert "dbo.Consignment" in _SRC  # ci -> Consignment FK hop
    assert "ci.ConsignmentID" in _SRC
    assert "dbo.ProductIncome" in _SRC  # Consignment -> ProductIncome FK hop
    assert "c.ProductIncomeID" in _SRC
    assert "pi.ID IS NULL OR pi.SourceDocumentType <> :debt_doc_type" in _SRC
    # mirror gba-pricing's exact intent: the debt document type is 1 and parameter-bound.
    assert "_DEBT_IMPORT_SOURCE_DOCUMENT_TYPE = 1" in _SRC


def test_windows_are_parameterized():
    assert ":asof" in _SRC and ":win" in _SRC


def _returns_fn() -> str:
    """The body of returns_for_products (so guards target the returns query specifically)."""
    start = _SRC.index("def returns_for_products")
    end = _SRC.index("\ndef ", start)
    return _SRC[start:end]


def test_returns_window_on_fromdate_not_sync_created():
    # SaleReturn.Created (and SaleReturnItem.Created) is a 1С-sync MIRROR timestamp; the real
    # return date is SaleReturn.FromDate. Windowing on sr.Created silently mis-dates every return.
    body = _returns_fn()
    assert "sr.FromDate" in body
    assert "sr.Created" not in body


def test_returns_qty_not_read_from_dead_columns():
    # SaleReturnItem.Qty / .Amount and OrderItem.ReturnedQty are all-zero in this sync — reading
    # them makes the returns factor a dead constant. Returned qty is reconstructed from OrderItem.Qty.
    # Check the SQL column references (the alias.Column form), not prose in the docstring.
    parts = _returns_fn().split('"""')  # [code, docstring, code-with-f-string-SQL, sql, tail]
    sql = "".join(parts[2:])  # everything after the docstring (the f-string SQL + trailers)
    assert "sri.Qty" not in sql
    assert "sri.Amount" not in sql
    assert "oi.ReturnedQty" not in sql
    assert "MAX(oi.Qty)" in sql  # returned qty is reconstructed from the sold OrderItem.Qty


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
    # The synthetic "Ввід боргів" product (ID 25422404) is a 1С debt-injection line that ranks #1
    # by revenue (~€7.4M) and would pollute velocity / avg sale price / monthly units. The three
    # AGGREGATING spine queries must exclude it; sold_product_ids is only a membership set (never
    # aggregated) so it is intentionally left without the filter.
    spine = _spine_fns()
    assert spine.count("oi.ProductID <> :synth") == 3
    # sold_product_ids must NOT carry the synthetic filter (it is a set, not an aggregate)
    src = _SRC
    sold = src[src.index("def sold_product_ids"):src.index("def sales_velocity")]
    assert "<> :synth" not in sold


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
    assert "o.Created" in body
    assert "oi.ProductID <> :synth" in body
