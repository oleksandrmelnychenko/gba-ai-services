"""A return anchored to an import placeholder is not a return of the sale that hosts the line."""

import os
from datetime import datetime

import pytest

from app.data import sale_attribution_repository as source
from app.services import sale_attribution

SALE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_placeholder_is_the_line_the_sync_writes_on_an_imported_return():
    assert source.IMPORT_PLACEHOLDER_LINE == (
        "oi.Qty=0 AND oi.PricePerItem=0 AND oi.PricePerItemWithoutVat=0 "
        "AND oi.ExchangeRateAmount=0 AND ISNULL(LTRIM(RTRIM(oi.Comment)),N'')=N'1С'"
    )
    assert source.IMPORT_PLACEHOLDER_RETURN == (
        "r.SourceDocumentID IS NOT NULL AND " + source.IMPORT_PLACEHOLDER_LINE
    )


def test_sale_returns_exclude_placeholder_returns_and_keep_the_rest(monkeypatch):
    captured = []
    monkeypatch.setattr(source, "query", lambda sql, params: captured.append((sql, params)) or [])

    assert source._returns("(:id0)", {"id0": 1}) == []

    sql, params = captured[0]
    assert params == {"id0": 1}
    assert f"WHERE s.ID IN (:id0) AND NOT ({source.IMPORT_PLACEHOLDER_RETURN})" in sql
    assert "JOIN dbo.SaleReturnItem ri ON ri.OrderItemID=oi.ID" in sql
    assert "oi.Qty>0" not in sql


def test_host_sale_evidence_is_complete_without_the_foreign_return(monkeypatch):
    def query(sql, params):
        if "IncomePaymentOrderSale" in sql:
            return []
        if "SaleReturnItem" in sql:
            assert source.IMPORT_PLACEHOLDER_RETURN in sql
            return [dict(sale_id=1, return_id=4, return_item_id=5,
                         document_at=datetime(2026, 5, 5, 12, 0), quantity=1,
                         credit_processed=True, recorded_return_credit_eur=5.25, canceled=False)]
        return [dict(sale_id=1, sale_net_uid=SALE, client_id=7, number="INV-1",
                     is_invoice=True, document_at=datetime(2026, 5, 4),
                     shipped_at=None, order_id=8, offer_net_uid=None, document_currency="EUR",
                     canceled=False, merged=False, retained_lines=1, date_basis="native_invoice_kyiv")]

    monkeypatch.setattr(source, "query", query)
    fact = source.facts([SALE])[SALE]
    evidence = sale_attribution.evidence(fact, 7)

    assert [r["return_item_id"] for r in fact["returns"]] == [5]
    assert evidence["return_credit_coverage_complete"] is True
    assert evidence["recorded_return_credit_eur"] == 5.25


@pytest.fixture
def returns_sql(monkeypatch):
    path = os.environ.get("NBA_SQL_FIXTURE_ENV_FILE")
    if not path:
        pytest.skip("Set NBA_SQL_FIXTURE_ENV_FILE for DEV reader temp-table acceptance")
    from sqlalchemy import create_engine, text

    from app.core.config import Settings

    settings = Settings(_env_file=path)
    assert settings.db_name == "ConcordDb_V5"
    engine = create_engine(settings.sqlalchemy_url, connect_args={"timeout": 30, "login_timeout": 10})
    with engine.connect() as connection:
        try:
            connection.exec_driver_sql("""
            CREATE TABLE #Sale(ID bigint,OrderID bigint);
            CREATE TABLE #OrderItem(ID bigint,OrderID bigint,Qty float,PricePerItem decimal(30,14),
                PricePerItemWithoutVat decimal(30,14),ExchangeRateAmount money,Comment nvarchar(900));
            CREATE TABLE #SaleReturn(ID bigint,FromDate datetime2,Deleted bit,IsCanceled bit,
                SourceDocumentID binary(16));
            CREATE TABLE #SaleReturnItem(ID bigint,SaleReturnID bigint,OrderItemID bigint,Qty float,
                IsMoneyReturned bit,RecordedGrossAmountVersion smallint,Amount decimal(30,14),Deleted bit);
            INSERT #Sale VALUES(1,10),(2,20);
            INSERT #OrderItem VALUES
                (100,10,5,10,8,41,NULL),
                (101,10,0,0,0,0,N'1С'),
                (102,10,0,0,0,0,N' 1С '),
                (103,10,0,0,0,0,NULL),
                (104,10,0,0,0,41,N'1С'),
                (105,10,0,0,0,0,N'1С'),
                (200,20,0,0,0,0,N'1С');
            INSERT #SaleReturn VALUES
                (70,'2026-05-05T10:00:00',0,0,NULL),
                (71,'2026-05-05T11:00:00',0,0,0x01),
                (72,'2026-05-05T12:00:00',0,0,0x02),
                (73,'2026-05-05T13:00:00',0,0,0x03),
                (74,'2026-05-05T14:00:00',0,0,0x04),
                (75,'2026-05-05T15:00:00',0,0,NULL),
                (76,'2026-05-05T16:00:00',0,0,0x06);
            INSERT #SaleReturnItem VALUES
                (80,70,100,1,1,1,9.5,0),
                (81,71,101,1,1,0,25,0),
                (82,72,102,1,1,0,26,0),
                (83,73,103,1,1,0,27,0),
                (84,74,104,1,1,0,28,0),
                (85,75,105,1,1,0,29,0),
                (86,76,200,1,1,0,30,0);
            """)

            def query(sql, params):
                for name in ("SaleReturnItem", "SaleReturn", "OrderItem", "Sale"):
                    sql = sql.replace("dbo." + name, "#" + name)
                return [dict(row) for row in connection.execute(text(sql), params).mappings()]

            monkeypatch.setattr(source, "query", query)
            yield query
        finally:
            connection.rollback()
    engine.dispose()


def test_only_imported_returns_on_the_sync_placeholder_leave_the_host_sale(returns_sql):
    rows = source._returns("(:id0,:id1)", {"id0": 1, "id1": 2})

    assert [(row["sale_id"], row["return_item_id"]) for row in rows] == [
        (1, 80),
        (1, 83),
        (1, 84),
        (1, 85),
    ]
    assert float(rows[0]["recorded_return_credit_eur"]) == 9.5
