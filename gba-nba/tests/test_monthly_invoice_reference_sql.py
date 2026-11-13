"""Confirmed invoice gross: an order draft date and later returns cannot shift its month."""

from datetime import datetime
from decimal import Decimal

from app.data import signals_repository as sig
from app.data.purchase_history import retained_purchase_cte
from tests.test_purchase_history_sql import sql_fixture as _sql_fixture

sql_fixture = _sql_fixture


def test_monthly_invoice_reference_uses_confirmed_events_and_does_not_subtract_later_returns(
    sql_fixture, monkeypatch
):
    db, query = sql_fixture
    monkeypatch.setattr(sig, "query", query)
    monkeypatch.setattr(sig, "_excluded", lambda day: [])
    degraded = sig.monthly_shipped(1, "2025-01-01", "2026-10-02")
    assert degraded == {"2026-10": Decimal(700)}
    assert degraded.unverified_orders == 1
    assert degraded.unverified_amount == Decimal(100)
    db.exec_driver_sql("DELETE #Lineage WHERE LocalSaleID=3 AND SourceSaleID=0x04")
    result = sig.monthly_shipped(1, "2025-01-01", "2026-10-02")
    assert result == {"2026-10": Decimal(800)}
    assert result.unverified_orders == 0
    db.exec_driver_sql("UPDATE #OrderItem SET ReturnedQty=Qty WHERE ID IN (1,2,3,6,8)")
    assert sig.monthly_shipped(1, "2025-01-01", "2026-10-02") == result


def test_imported_sale_created_before_invoicing_is_verified_by_its_invoice_clock(
    sql_fixture, monkeypatch
):
    db, query = sql_fixture
    monkeypatch.setattr(sig, "query", query)
    monkeypatch.setattr(sig, "_excluded", lambda day: [])
    db.exec_driver_sql("""
        DELETE #Lineage WHERE LocalSaleID=3;
        UPDATE #Sale SET Created='2026-09-29T08:00:00', ChangedToInvoice='2026-10-01T09:30:00' WHERE ID=3;
        INSERT #Lineage VALUES(3,'2026-10-01T09:30:00',1,0x03,0);
    """)
    rows = query(
        retained_purchase_cte() + " SELECT * FROM retained_purchases WHERE order_id = 3",
        {"mid": 1, "source_start": "2025-01-01", "asof": "2026-10-02"},
    )
    assert rows[0]["timestamp_basis"] == "imported_lineage_utc"
    assert rows[0]["purchased_at_utc"] == datetime(2026, 10, 1, 9, 30)
    series = sig.monthly_shipped(1, "2025-01-01", "2026-10-02")
    assert series.unverified_orders == 0
    assert series == {"2026-10": Decimal(800)}
