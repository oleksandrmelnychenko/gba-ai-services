"""The demand rule run by the dev SQL Server over synthetic rows held in session temp tables.

Nothing is read from or written to ConcordDb_V5: every dbo table the rule names is replaced by
a #temp table of the same name that lives in tempdb for the one connection of the test.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.data import source_rules as rules

pytestmark = pytest.mark.integration

_SCHEMA = """
CREATE TABLE #Order (ID bigint);
CREATE TABLE #Sale (ID bigint, OrderID bigint, ChangedToInvoice datetime2 NULL, Deleted bit);
CREATE TABLE #OrderItem (ID bigint, OrderID bigint, ProductID bigint NULL, Qty float,
    PricePerItem decimal(30, 14), PricePerItemWithoutVat decimal(30, 14),
    ExchangeRateAmount money, Comment nvarchar(900) NULL, Deleted bit,
    IsValidForCurrentSale bit, Updated datetime2);
CREATE TABLE #Storage (ID bigint, Deleted bit, ForDefective bit, AvailableForReSale bit,
    IsResale bit);
CREATE TABLE #SaleReturn (ID bigint, FromDate datetime2, Deleted bit, IsCanceled bit,
    SourceDocumentID binary(16) NULL);
CREATE TABLE #SaleReturnItem (ID bigint, SaleReturnID bigint, OrderItemID bigint, Qty float,
    StorageID bigint, Deleted bit, Updated datetime2);
CREATE TABLE #DataSyncAccountingSourceTarget (TargetEntityType nvarchar(128),
    TargetEntityID bigint, IsCurrent bit, OwnershipType tinyint);
"""

_ROWS = """
INSERT INTO #Storage VALUES (1, 0, 0, 1, 0), (2, 0, 1, 0, 0);
INSERT INTO #Order VALUES (500), (501), (502), (503);
INSERT INTO #Sale VALUES
    (900, 500, '2026-03-10T08:00:00', 0),
    (901, 501, '2026-03-10T12:00:00', 0),
    (902, 502, '2026-03-11T08:00:00', 0),
    (903, 503, '2026-03-09T22:30:00', 0);
INSERT INTO #OrderItem VALUES
    (1, 500, 100, 5, 10, 8, 41, NULL, 0, 1, '2026-03-10'),
    (2, 501, 100, 2, 11, 9, 41, NULL, 0, 1, '2026-03-10'),
    (3, 502, 100, 4, 10, 8, 41, NULL, 0, 1, '2026-03-11'),
    (4, 503, 200, 3, 20, 16, 41, NULL, 0, 1, '2026-03-09'),
    (10, 502, 100, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-10'),
    (11, 502, 200, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-10'),
    (12, 502, 100, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-11'),
    (13, 502, 100, 0, 0, 0, 0, NULL, 0, 0, '2026-03-11'),
    (14, 502, 300, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-11'),
    (15, 502, 100, 0, 0, 0, 0, N'1С', 0, 0, '2026-03-11');
INSERT INTO #SaleReturn VALUES
    (70, '2026-03-20T10:00:00', 0, 0, NULL),
    (71, '2026-03-10T15:00:00', 0, 0, 0x01),
    (72, '2026-03-09T23:00:00', 0, 0, 0x02),
    (73, '2026-03-11T10:00:00', 0, 0, 0x03),
    (74, '2026-03-11T10:00:00', 0, 0, 0x04),
    (75, '2026-03-12T10:00:00', 0, 0, 0x05),
    (76, '2026-03-11T10:00:00', 0, 1, 0x06);
INSERT INTO #SaleReturnItem VALUES
    (80, 70, 1, 1, 1, 0, '2026-03-20'),
    (81, 71, 10, 5, 1, 0, '2026-03-21'),
    (82, 72, 11, 9, 1, 0, '2026-03-10'),
    (83, 73, 12, 1, 2, 0, '2026-03-11'),
    (84, 74, 13, 1, 1, 0, '2026-03-11'),
    (85, 75, 14, 2, 1, 0, '2026-03-12'),
    (86, 76, 15, 1, 1, 0, '2026-03-11');
"""

_PARAMS = {
    "demand_from": datetime(2026, 2, 28, 22, 0),
    "demand_to": datetime(2026, 3, 31, 21, 0),
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


def _units(rows: list[dict]) -> dict[int, Decimal]:
    return {int(r["line_id"]): Decimal(r["units"]) for r in rows}


def test_the_return_day_absorbs_the_placeholder_return_and_stops_at_zero(run):
    before = _units(run(f"SELECT l.line_id, l.units FROM ({rules.sale_lines()}) l"))
    after = _units(run(f"SELECT d.line_id, d.units FROM {rules.demand_lines()}"))

    assert before == {1: Decimal(4), 2: Decimal(2), 3: Decimal(4), 4: Decimal(3)}
    assert after == {1: Decimal(0), 2: Decimal(1), 3: Decimal(4), 4: Decimal(0)}


def test_product_days_and_totals_agree_and_other_days_are_untouched(run):
    days = run(
        f"SELECT d.product_id, d.sold_day, SUM(d.units) AS units "
        f"FROM {rules.demand_lines()} GROUP BY d.product_id, d.sold_day"
    )
    totals = run(
        f"SELECT d.product_id, SUM(d.units) AS units FROM {rules.demand_lines()} GROUP BY d.product_id"
    )

    assert {(int(r["product_id"]), r["sold_day"]): Decimal(r["units"]) for r in days} == {
        (100, date(2026, 3, 10)): Decimal(1),
        (100, date(2026, 3, 11)): Decimal(4),
        (200, date(2026, 3, 10)): Decimal(0),
    }
    assert {int(r["product_id"]): Decimal(r["units"]) for r in totals} == {
        100: Decimal(5),
        200: Decimal(0),
    }


def test_the_outcome_counts_what_each_return_day_absorbed_and_dropped(run):
    rows = run(
        f"SELECT r.product_id, r.returned_day, r.returned_units, r.absorbed_units, r.dropped_units "
        f"FROM {rules.placeholder_return_outcome()} ORDER BY r.product_id, r.returned_day"
    )

    assert [
        (int(r["product_id"]), r["returned_day"], Decimal(r["returned_units"]),
         Decimal(r["absorbed_units"]), Decimal(r["dropped_units"]))
        for r in rows
    ] == [
        (100, date(2026, 3, 10), Decimal(5), Decimal(5), Decimal(0)),
        (200, date(2026, 3, 10), Decimal(9), Decimal(3), Decimal(6)),
        (300, date(2026, 3, 12), Decimal(2), Decimal(0), Decimal(2)),
    ]


def test_a_return_outside_the_window_reduces_nothing(run):
    after = _units(
        run(
            f"SELECT d.line_id, d.units FROM {rules.demand_lines()}",
            {"demand_to": datetime(2026, 3, 10, 15, 0)},
        )
    )

    assert after == {1: Decimal(5), 2: Decimal(2), 4: Decimal(0)}
