"""The rule SQL run by the dev SQL Server over synthetic rows held in session temp tables.

Nothing is read from or written to ConcordDb_V5: every dbo table the rule names is replaced by
a #temp table of the same name that lives in tempdb for the one connection of the test.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.data import source_rules as rules

pytestmark = pytest.mark.integration

_SCHEMA = """
CREATE TABLE #ClientAgreement (ID bigint, ClientID bigint);
CREATE TABLE #Order (ID bigint);
CREATE TABLE #Sale (ID bigint, OrderID bigint, ClientAgreementID bigint,
    ChangedToInvoice datetime2 NULL, Deleted bit, Updated datetime2);
CREATE TABLE #OrderItem (ID bigint, OrderID bigint, ProductID bigint NULL, Qty float,
    PricePerItem decimal(30, 14), PricePerItemWithoutVat decimal(30, 14),
    ExchangeRateAmount money, Comment nvarchar(900) NULL, Deleted bit,
    IsValidForCurrentSale bit, Updated datetime2);
CREATE TABLE #SaleReturn (ID bigint, FromDate datetime2, ClientAgreementID bigint,
    Deleted bit, IsCanceled bit, SourceDocumentID binary(16) NULL, Updated datetime2);
CREATE TABLE #SaleReturnItem (ID bigint, SaleReturnID bigint, OrderItemID bigint, Qty float,
    Amount decimal(30, 14), Deleted bit, Updated datetime2);
CREATE TABLE #DataSyncAccountingSourceTarget (TargetEntityType nvarchar(128),
    TargetEntityID bigint, IsCurrent bit, OwnershipType tinyint);
"""

_ROWS = """
INSERT INTO #ClientAgreement VALUES (10, 1), (20, 2);
INSERT INTO #Order VALUES (500), (501), (502);
INSERT INTO #Sale VALUES
    (900, 500, 10, '2026-02-10T09:00:00', 0, '2026-02-10'),
    (901, 501, 10, '2026-03-12T09:00:00', 0, '2026-03-12'),
    (902, 502, 20, '2026-01-31T22:30:00', 0, '2026-01-31');
INSERT INTO #OrderItem VALUES
    (1, 500, 100, 5, 10, 8, 41, NULL, 0, 1, '2026-02-10'),
    (2, 501, 200, 1, 40, 33, 41, NULL, 0, 1, '2026-03-12'),
    (3, 501, 300, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-20'),
    (4, 502, 100, 0, 0, 0, 0, N' 1С ', 0, 0, '2026-01-31'),
    (5, 501, 400, 0, 0, 0, 0, NULL, 0, 0, '2026-03-20'),
    (6, 501, 500, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-20'),
    (7, 501, 600, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-20'),
    (8, 501, 700, 0, 0, 0, 41, N'1С', 0, 0, '2026-03-20');
INSERT INTO #SaleReturn VALUES
    (70, '2026-03-05T10:00:00', 10, 0, 0, NULL, '2026-03-05'),
    (71, '2026-03-20T10:00:00', 10, 0, 0, 0x01, '2026-03-21'),
    (72, '2026-01-31T22:30:00', 20, 0, 0, 0x02, '2026-02-01'),
    (73, '2026-03-20T10:00:00', 10, 0, 0, 0x03, '2026-03-20'),
    (74, '2026-03-20T10:00:00', 10, 0, 0, NULL, '2026-03-20'),
    (75, '2026-03-20T10:00:00', 10, 0, 1, 0x05, '2026-03-20'),
    (76, '2026-03-20T10:00:00', 10, 0, 0, 0x06, '2026-03-20');
INSERT INTO #SaleReturnItem VALUES
    (80, 70, 1, 2, 19.5, 0, '2026-03-05'),
    (81, 71, 3, 1, 25, 0, '2026-03-20'),
    (82, 72, 4, 3, 70, 0, '2026-02-01'),
    (83, 73, 5, 1, 11, 0, '2026-03-20'),
    (84, 74, 6, 1, 12, 0, '2026-03-20'),
    (85, 75, 7, 1, 13, 0, '2026-03-20'),
    (86, 76, 8, 1, 14, 0, '2026-03-20');
"""

_PARAMS = {
    "source_history_start": datetime(2025, 12, 31, 22, 0),
    "history_start": datetime(2025, 12, 31, 22, 0),
    "asof": datetime(2026, 4, 30, 21, 0),
}


def _on_temp_tables(sql: str) -> str:
    return sql.replace("dbo.[Order]", "#Order").replace("dbo.", "#")


@pytest.fixture(scope="module")
def run():
    from app.data.db import get_engine

    try:
        connection = get_engine().connect()
    except Exception:  # noqa: BLE001
        pytest.skip("dev DB not reachable")
    try:
        connection.exec_driver_sql(_SCHEMA + _ROWS)

        def execute(sql: str, extra: dict | None = None) -> list[dict]:
            result = connection.execute(text(_on_temp_tables(sql)), {**_PARAMS, **(extra or {})})
            columns = list(result.keys())
            return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]

        yield execute
    finally:
        connection.rollback()
        connection.close()


def _series(run, key: str) -> dict[tuple[int, str], Decimal]:
    params = rules.zone_params(_PARAMS["source_history_start"], _PARAMS["asof"])
    month = rules.fact_month(_PARAMS["source_history_start"], _PARAMS["asof"])
    rows = run(
        f"""
        SELECT f.{key} AS entity, {month} AS ym, {rules.monthly_net_eur()} AS eur,
               {rules.dropped_placeholder_eur()} AS dropped
        FROM {rules.net_sales_facts()}
        GROUP BY f.{key}, {month}
        """,
        params,
    )
    return {(int(r["entity"]), r["ym"]): (Decimal(r["eur"]), Decimal(r["dropped"])) for r in rows}


def test_only_imported_returns_on_the_sync_placeholder_become_facts(run):
    rows = run(
        f"SELECT f.fact_id, f.client_id, f.product_id, f.fact_at, f.qty, f.eur "
        f"FROM {rules.net_sales_facts()} WHERE f.placeholder_return = 1 ORDER BY f.fact_id"
    )

    assert [
        (int(r["fact_id"]), int(r["client_id"]), int(r["product_id"]), r["fact_at"],
         Decimal(r["qty"]), Decimal(r["eur"]))
        for r in rows
    ] == [
        (81, 1, 300, datetime(2026, 3, 20, 10, 0), Decimal(-1), Decimal(-25)),
        (82, 2, 100, datetime(2026, 1, 31, 22, 30), Decimal(-3), Decimal(-70)),
    ]


def test_ordinary_returns_are_netted_on_the_returned_line_exactly_as_before(run):
    columns = "fact_id, client_id, product_id, fact_at, qty, returned_qty, price, eur"
    before = run(f"SELECT {columns} FROM ({rules.sale_lines()}) l ORDER BY l.fact_id")
    after = run(
        f"SELECT {columns} FROM {rules.net_sales_facts()} "
        f"WHERE f.placeholder_return = 0 ORDER BY f.fact_id"
    )

    assert after == before
    assert [(int(r["fact_id"]), Decimal(r["qty"]), Decimal(r["eur"])) for r in after] == [
        (1, Decimal(3), Decimal(30)),
        (2, Decimal(1), Decimal(40)),
    ]


def test_the_return_reduces_its_own_kyiv_month_for_its_own_client_and_product(run):
    by_client = _series(run, "client_id")
    by_product = _series(run, "product_id")

    assert by_client == {
        (1, "2026-02"): (Decimal(30), Decimal(0)),
        (1, "2026-03"): (Decimal(15), Decimal(0)),
        (2, "2026-02"): (Decimal(0), Decimal(70)),
    }
    assert by_product == {
        (100, "2026-02"): (Decimal(0), Decimal(40)),
        (200, "2026-03"): (Decimal(40), Decimal(0)),
        (300, "2026-03"): (Decimal(0), Decimal(25)),
    }


def test_a_return_at_or_after_the_cut_off_is_not_a_fact_yet(run):
    rows = run(
        f"SELECT COUNT(*) AS n FROM {rules.net_sales_facts()} WHERE f.placeholder_return = 1",
        {"asof": datetime(2026, 3, 20, 10, 0)},
    )

    assert rows[0]["n"] == 1
