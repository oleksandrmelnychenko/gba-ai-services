"""Real SQL date projection against disposable session-local tables; no business writes."""

import os
from datetime import datetime

import pytest

from app.data import sale_attribution_repository as source


@pytest.fixture
def dates_sql(monkeypatch):
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
            assert (
                connection.execute(text("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT')")).scalar()
                == 0
            )
            connection.exec_driver_sql("""
            CREATE TABLE #Sale(ID bigint,NetUid uniqueidentifier,OrderID bigint,ClientAgreementID bigint,
                IsImported bit,Deleted bit,IsMerged bit,ChangedToInvoice datetime2,Created datetime2,
                ShipmentDate datetime2,SaleNumberID bigint,SaleInvoiceNumberID bigint);
            CREATE TABLE #Order(ID bigint,Deleted bit,IsMerged bit,ClientShoppingCartID bigint);
            CREATE TABLE #ClientAgreement(ID bigint,ClientID bigint,AgreementID bigint,Deleted bit);
            CREATE TABLE #Agreement(ID bigint,CurrencyID bigint);
            CREATE TABLE #Currency(ID bigint,Code varchar(3));
            CREATE TABLE #SaleNumber(ID bigint,Value varchar(30));
            CREATE TABLE #SaleInvoiceNumber(ID bigint,Number varchar(30));
            CREATE TABLE #ClientShoppingCart(ID bigint,NetUid uniqueidentifier);
            CREATE TABLE #SaleMerged(Deleted bit,InputSaleID bigint,OutputSaleID bigint);
            CREATE TABLE #OrderItem(OrderID bigint,Deleted bit,IsValidForCurrentSale bit,
                Qty float,ReturnedQty float);
            CREATE TABLE #Lineage(LocalSaleID bigint,DestinationDocumentDate datetime2,IsPresent bit,
                SourceSaleID varbinary(16),ImportedForAmg bit);
            INSERT #OrderItem VALUES(1,0,1,10,-1),(1,0,1,0,-1),(1,0,1,10,11),
                (1,0,1,10,10),(1,0,1,10,2),(1,0,1,10,NULL),(1,1,1,10,0),(1,0,0,10,0);
            INSERT #ClientAgreement VALUES(7,7,1,0);
            INSERT #Agreement VALUES(1,1); INSERT #Currency VALUES(1,'UAH');
            """)
            # Same UTC midnight boundary, different provenance; native draft creation is years earlier.
            for ident in range(1, 8):
                connection.execute(text("INSERT #Order VALUES(:id,0,0,NULL)"), {"id": ident})
                connection.execute(
                    text("""INSERT #Sale VALUES(:id,NEWID(),:id,7,:imported,0,0,:invoice,
                    :created,NULL,NULL,NULL)"""),
                    {
                        "id": ident,
                        "imported": ident != 1,
                        "invoice": None if ident == 7 else datetime(2024, 12, 31, 22, 30),
                        "created": datetime(2020, 1, 1) if ident == 1 else datetime(2024, 12, 31, 22, 30),
                    },
                )
            connection.exec_driver_sql("""
                INSERT #Lineage VALUES
                (2,'2024-12-31T22:30:00',1,0x02,0),
                (3,'2024-12-31T22:30:00',1,0x03,0),(3,'2024-12-31T22:30:00',1,0x03,1),
                (4,'2024-12-31T22:30:01',1,0x04,0),
                (5,'2024-12-31T22:30:00',0,0x05,0);
            """)
            # ID7 is a native draft, so its Created has no invoice-event provenance.
            connection.exec_driver_sql("UPDATE #Sale SET IsImported=0 WHERE ID=7")

            def query(sql, params):
                names = [
                    "DataSyncImportedSaleSourceLine",
                    "ClientShoppingCart",
                    "ClientAgreement",
                    "SaleInvoiceNumber",
                    "SaleNumber",
                    "SaleMerged",
                    "OrderItem",
                    "Agreement",
                    "Currency",
                    "[Order]",
                    "Sale",
                ]
                for name in names:
                    sql = sql.replace(
                        "dbo." + name,
                        "#" + ("Lineage" if name == "DataSyncImportedSaleSourceLine" else name.strip("[]")),
                    )
                return [dict(row) for row in connection.execute(text(sql), params).mappings()]

            monkeypatch.setattr(source, "query", query)
            monkeypatch.setattr(source.history, "source_history_start", lambda: datetime(2025, 1, 1).date())
            yield query
        finally:
            connection.rollback()
    engine.dispose()


def test_verified_midnight_dates_and_draft_creation_are_projected_before_candidate_floor(dates_sql):
    rows, total = source.candidates(7, 0, 20)
    assert total == 2
    by_id = {row["sale_id"]: row for row in rows}
    assert set(by_id) == {1, 2}
    assert by_id[1]["date_basis"] == "native_invoice_kyiv"
    assert by_id[1]["retained_lines"] == 2
    assert by_id[2]["date_basis"] == "imported_lineage_kyiv"
    assert all(row["document_date"] == "2025-01-01" for row in rows)
    assert all(row["shipment_date"] is None for row in rows)
    page, total = source.candidates(7, 1, 1)
    assert total == 2 and len(page) == 1


def test_ambiguous_missing_inactive_and_mismatched_lineage_do_not_invent_clock(dates_sql):
    rows = dates_sql(source._SALES + " WHERE s.ID>=3", {})
    for row in rows:
        projected = source._sale(row)
        assert projected["date_basis"] == "stored_sale_date_unverified"
        assert projected["document_date"] == "2024-12-31"
