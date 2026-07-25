"""DB-backed smoke against the dev ConcordDb_V5. Marked integration; skipped if the DB is unreachable."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

_AS_OF = datetime.now(UTC).strftime("%Y-%m-%d")


def _db_ok() -> bool:
    try:
        from app.data.db import query
        query("SELECT 1 AS ok")
        return True
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_db():
    if not _db_ok():
        pytest.skip("dev DB not reachable")


def test_on_hand_stock_shape_and_plausibility():
    from app.data import signals_repository as sig
    stock = sig.on_hand_stock()
    assert isinstance(stock, list) and stock, "expected on-hand stock rows"
    for r in stock[:200]:
        assert float(r["qty_on_hand"]) > 0
        assert r["eur_value"] is None or float(r["eur_value"]) >= 0
    total_qty = sum(float(r["qty_on_hand"]) for r in stock)
    total_eur = sum(float(r["eur_value"] or 0) for r in stock)
    avg_unit = total_eur / total_qty if total_qty else 0
    # plausibility: a 50x currency bug (raw UAH) would push avg unit cost into the tens/hundreds
    assert 0 <= avg_unit < 1000, f"implausible avg EUR unit cost {avg_unit}"


def test_on_hand_qty_matches_productavailability_without_reservation_replay():
    from app.data import signals_repository as sig
    from app.data.db import query

    expected = query(
        """
        SELECT TOP 1 pa.ProductID AS product_id, SUM(pa.Amount) AS qty_on_hand
        FROM dbo.ProductAvailability pa
        JOIN dbo.Storage s ON s.ID = pa.StorageID
        WHERE pa.Deleted = 0
              AND pa.ProductID <> :synth
              AND s.Deleted = 0
              AND s.ForDefective = 0
              AND (s.AvailableForReSale = 1 OR s.IsResale = 1)
        GROUP BY pa.ProductID
        HAVING SUM(pa.Amount) > 0
        ORDER BY SUM(pa.Amount) DESC, pa.ProductID
        """,
        {"synth": sig.synthetic_product_id()},
    )
    assert expected, "expected ProductAvailability in an operational sellable storage"
    product_id = int(expected[0]["product_id"])
    actual = sig.on_hand_stock([product_id])
    assert len(actual) == 1
    assert float(actual[0]["qty_on_hand"]) == pytest.approx(
        float(expected[0]["qty_on_hand"]), rel=0, abs=1e-9
    )


def test_all_stock_quantity_and_currency_rows_reconcile_exactly_to_direct_sql():
    from app.data import signals_repository as sig
    from app.data.db import query

    expected_rows = query(
        """
        WITH stock AS (
            SELECT pa.ProductID AS product_id,
                   SUM(CAST(pa.Amount AS decimal(18, 8))) AS qty_on_hand
            FROM dbo.ProductAvailability pa
            JOIN dbo.Storage s ON s.ID = pa.StorageID
            WHERE pa.Deleted = 0
                  AND pa.ProductID IS NOT NULL
                  AND pa.ProductID <> :synth
                  AND s.Deleted = 0
                  AND s.ForDefective = 0
                  AND (s.AvailableForReSale = 1 OR s.IsResale = 1)
            GROUP BY pa.ProductID
            HAVING SUM(CAST(pa.Amount AS decimal(18, 8))) > 0
        ),
        costs AS (
            SELECT ci.ProductID AS product_id,
                   SUM(
                       CAST(ci.RemainingQty AS decimal(18, 8))
                       * CAST(ci.AccountingPrice AS decimal(28, 14))
                   ) AS value_eur,
                   SUM(CAST(ci.RemainingQty AS decimal(18, 8))) AS qty
            FROM dbo.ConsignmentItem ci
            JOIN stock ON stock.product_id = ci.ProductID
            LEFT JOIN dbo.Consignment c ON c.ID = ci.ConsignmentID
            LEFT JOIN dbo.ProductIncome pi ON pi.ID = c.ProductIncomeID
            WHERE ci.Deleted = 0
                  AND ci.RemainingQty > 0
                  AND ci.AccountingPrice > 0
                  AND (
                      pi.ID IS NULL
                      OR pi.SourceDocumentType IS NULL
                      OR pi.SourceDocumentType <> 1
                  )
            GROUP BY ci.ProductID
        )
        SELECT stock.product_id,
               stock.qty_on_hand,
               CAST(costs.value_eur / NULLIF(costs.qty, 0) AS decimal(28, 8))
                   AS unit_cost_eur,
               CASE
                   WHEN costs.qty IS NULL THEN NULL
                   ELSE CAST(
                       stock.qty_on_hand
                       * CAST(costs.value_eur / NULLIF(costs.qty, 0) AS decimal(28, 8))
                       AS decimal(38, 8)
                   )
               END AS eur_value
        FROM stock
        LEFT JOIN costs ON costs.product_id = stock.product_id
        """,
        {"synth": sig.synthetic_product_id()},
    )
    actual_rows = sig.on_hand_stock()
    expected = {int(row["product_id"]): row for row in expected_rows}
    actual = {int(row["product_id"]): row for row in actual_rows}

    assert len(expected) == len(expected_rows)
    assert len(actual) == len(actual_rows)
    assert set(actual) == set(expected)
    for product_id, expected_row in expected.items():
        actual_row = actual[product_id]
        assert Decimal(str(actual_row["qty_on_hand"])) == expected_row["qty_on_hand"]
        assert actual_row["unit_cost_eur"] == expected_row["unit_cost_eur"]
        assert actual_row["eur_value"] == expected_row["eur_value"]


def _assert_return_total_matches_canonical_sum(product_id: int, window_days: int = 365):
    from app.core.config import get_settings
    from app.core.history import day_history_window
    from app.data import signals_repository as sig
    from app.data.db import query

    window = day_history_window(
        _AS_OF,
        window_days,
        get_settings().source_history_start_date,
    )
    expected = query(
        """
        SELECT SUM(sri.Qty) AS returned_qty
        FROM dbo.SaleReturnItem sri
        JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
        JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
             AND sr.Deleted = 0 AND sr.IsCanceled = 0
        WHERE sri.Deleted = 0
              AND oi.ProductID = :product_id
              AND oi.ProductID <> :synth
              AND sr.FromDate >= :source_history_start
              AND sr.FromDate >= :history_start
              AND sr.FromDate < :asof
        """,
        {
            "product_id": product_id,
            "synth": sig.synthetic_product_id(),
            "source_history_start": window.source_history_start.isoformat(),
            "history_start": window.effective_start.isoformat(),
            "asof": _AS_OF,
        },
    )
    actual = sig.returns_for_products(_AS_OF, window_days, [product_id])
    assert len(actual) == 1
    assert float(actual[0]["returned_qty"]) == pytest.approx(
        float(expected[0]["returned_qty"]), rel=0, abs=1e-9
    )


def test_partial_return_uses_salereturnitem_qty_not_original_sold_qty():
    from app.core.config import get_settings
    from app.core.history import day_history_window
    from app.data import signals_repository as sig
    from app.data.db import query

    window = day_history_window(
        _AS_OF,
        365,
        get_settings().source_history_start_date,
    )
    rows = query(
        """
        SELECT TOP 1 oi.ProductID AS product_id
        FROM dbo.SaleReturnItem sri
        JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
        JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
             AND sr.Deleted = 0 AND sr.IsCanceled = 0
        WHERE sri.Deleted = 0
              AND oi.ProductID <> :synth
              AND sr.FromDate >= :source_history_start
              AND sr.FromDate >= :history_start
              AND sr.FromDate < :asof
        GROUP BY oi.ProductID, sri.SaleReturnID, sri.OrderItemID
        HAVING SUM(sri.Qty) > 0 AND SUM(sri.Qty) < MAX(oi.Qty)
        ORDER BY oi.ProductID
        """,
        {
            "synth": sig.synthetic_product_id(),
            "source_history_start": window.source_history_start.isoformat(),
            "history_start": window.effective_start.isoformat(),
            "asof": _AS_OF,
        },
    )
    if not rows:
        pytest.skip("no partial active return in the current integration dataset")
    _assert_return_total_matches_canonical_sum(int(rows[0]["product_id"]))


def test_multiple_return_rows_are_summed_not_replaced_by_sold_qty():
    from app.core.config import get_settings
    from app.core.history import day_history_window
    from app.data import signals_repository as sig
    from app.data.db import query

    window = day_history_window(
        _AS_OF,
        365,
        get_settings().source_history_start_date,
    )
    rows = query(
        """
        SELECT TOP 1 oi.ProductID AS product_id
        FROM dbo.SaleReturnItem sri
        JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
        JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
             AND sr.Deleted = 0 AND sr.IsCanceled = 0
        WHERE sri.Deleted = 0
              AND oi.ProductID <> :synth
              AND sr.FromDate >= :source_history_start
              AND sr.FromDate >= :history_start
              AND sr.FromDate < :asof
        GROUP BY oi.ProductID, sri.SaleReturnID, sri.OrderItemID
        HAVING COUNT(*) > 1 AND SUM(sri.Qty) <> MAX(sri.Qty)
        ORDER BY oi.ProductID
        """,
        {
            "synth": sig.synthetic_product_id(),
            "source_history_start": window.source_history_start.isoformat(),
            "history_start": window.effective_start.isoformat(),
            "asof": _AS_OF,
        },
    )
    if not rows:
        pytest.skip("no multi-row active return in the current integration dataset")
    _assert_return_total_matches_canonical_sum(int(rows[0]["product_id"]))


def test_live_sales_currency_half_cent_and_quantity_reconcile_to_direct_sql():
    from app.core import exact_numbers as exact
    from app.core.config import get_settings
    from app.data import signals_repository as sig
    from app.data.db import query
    from app.services import product_analytics

    candidates = query(
        """
        WITH monthly AS (
            SELECT oi.ProductID AS product_id,
                   CONVERT(char(7), o.Created, 126) AS ym,
                   SUM(oi.Qty * oi.PricePerItem) AS binary_float_revenue,
                   SUM(
                       CAST(oi.Qty AS decimal(18, 8))
                       * CAST(oi.PricePerItem AS decimal(28, 14))
                   ) AS exact_revenue_eur,
                   SUM(CAST(oi.Qty AS decimal(18, 8))) AS exact_units,
                   COUNT(DISTINCT o.ID) AS order_count
            FROM dbo.OrderItem oi
            JOIN dbo.[Order] o ON o.ID = oi.OrderID
            WHERE oi.IsValidForCurrentSale = 1
                  AND oi.ProductID <> :synth
                  AND o.Created >= :source_history_start
                  AND o.Created < :asof
            GROUP BY oi.ProductID, CONVERT(char(7), o.Created, 126)
        )
        SELECT TOP 1 *
        FROM monthly
        WHERE CAST(binary_float_revenue AS decimal(38, 2))
              <> CAST(exact_revenue_eur AS decimal(38, 2))
        ORDER BY ym DESC, product_id
        """,
        {
            "synth": sig.synthetic_product_id(),
            "source_history_start": get_settings().source_history_start_date.isoformat(),
            "asof": _AS_OF,
        },
    )
    assert candidates, "expected a live half-cent binary-float drift fixture"
    expected = candidates[0]
    year, month = (int(part) for part in str(expected["ym"]).split("-"))
    window_start = f"{year:04d}-{month:02d}-01"
    end_exclusive = (
        f"{year + 1:04d}-01-01"
        if month == 12
        else f"{year:04d}-{month + 1:02d}-01"
    )

    rows = sig.monthly_product_sales(
        int(expected["product_id"]),
        window_start,
        end_exclusive,
    )

    assert len(rows) == 1
    actual = rows[0]
    assert actual["ym"] == expected["ym"]
    assert actual["units"] == expected["exact_units"]
    assert actual["order_count"] == expected["order_count"]
    assert actual["revenue_eur"] == expected["exact_revenue_eur"]

    response = product_analytics.build_product_analytics(
        product_id=int(expected["product_id"]),
        as_of=end_exclusive,
        months=2,
        model_version="integration",
        snapshot={"product_id": int(expected["product_id"]), "found": True},
        monthly_rows=rows,
    )
    point = next(point for point in response.sales_series if point.month == expected["ym"])
    assert point.units == exact.quantity(expected["exact_units"])
    assert point.revenue_eur == exact.money(expected["exact_revenue_eur"])
    assert point.avg_price_eur == exact.unit_price(
        expected["exact_revenue_eur"] / expected["exact_units"]
    )


def test_per_product_signals_for_a_stocked_sku():
    from app.data import signals_repository as sig
    stock = sig.on_hand_stock()
    pid = int(stock[0]["product_id"])
    vel = sig.sales_velocity(_AS_OF, 365, [pid])
    price = sig.avg_sale_price_eur(_AS_OF, 365, [pid])
    rets = sig.returns_for_products(_AS_OF, 365, [pid])
    meta = sig.product_meta([pid], _AS_OF)
    assert isinstance(vel, list) and isinstance(price, list) and isinstance(rets, list)
    assert pid in meta and "name" in meta[pid]
    assert "primary_producer_id" in meta[pid] and "primary_producer_name" in meta[pid]


def test_latest_factual_producer_reconciles_to_as_of_and_source_floor():
    from app.core.config import get_settings
    from app.data import signals_repository as sig
    from app.data.db import query

    floor = get_settings().source_history_start_date.isoformat()
    rows = query(
        """
        WITH factual_supply AS (
            SELECT sioi.ProductID AS product_id,
                   so.ClientID AS producer_id,
                   si.DateFrom AS source_date,
                   si.ID AS source_document_id,
                   sioi.ID AS source_line_id
            FROM dbo.SupplyInvoice si
            JOIN dbo.SupplyInvoiceOrderItem sioi
                 ON sioi.SupplyInvoiceID = si.ID AND sioi.Deleted = 0
            JOIN dbo.SupplyOrder so ON so.ID = si.SupplyOrderID
            WHERE si.Deleted = 0
                  AND so.ClientID IS NOT NULL
                  AND si.DateFrom >= :source_history_start
                  AND si.DateFrom < :asof

            UNION ALL

            SELECT soui.ProductID,
                   COALESCE(soui.SupplierID, sou.SupplierID),
                   sou.FromDate,
                   sou.ID,
                   soui.ID
            FROM dbo.SupplyOrderUkraine sou
            JOIN dbo.SupplyOrderUkraineItem soui
                 ON soui.SupplyOrderUkraineID = sou.ID AND soui.Deleted = 0
            WHERE sou.Deleted = 0
                  AND NOT (sou.IsFromCockpit = 1 AND sou.IsPlaced = 0)
                  AND COALESCE(soui.SupplierID, sou.SupplierID) IS NOT NULL
                  AND sou.FromDate >= :source_history_start
                  AND sou.FromDate < :asof
        )
        SELECT TOP 1 product_id, producer_id
        FROM factual_supply
        ORDER BY source_date DESC, source_document_id DESC, source_line_id DESC
        """,
        {"source_history_start": floor, "asof": _AS_OF},
    )
    assert rows, "expected a factual supply row inside the source-history interval"
    expected = rows[0]

    actual = sig.product_meta([int(expected["product_id"])], _AS_OF)

    assert actual[int(expected["product_id"])]["primary_producer_id"] == expected["producer_id"]


def test_snapshot_runs_end_to_end():
    from app.core import exact_numbers as exact
    from app.services import stock_health

    snap = stock_health.snapshot(_AS_OF)
    assert snap["total_skus"] > 0
    assert set(snap["bands"]) and snap["total_eur_value"] >= 0
    assert sum(b["count"] for b in snap["bands"].values()) == snap["total_skus"]
    assert snap["total_qty"] == exact.quantity(
        sum((Decimal(str(row["qty_on_hand"])) for row in snap["rows"]), Decimal("0"))
    )
    assert snap["total_eur_value"] == exact.money(
        sum((Decimal(str(row["eur_value"])) for row in snap["rows"]), Decimal("0"))
    )
    assert snap["source_history_start"] == "2025-01-01"
    assert snap["history_fingerprint"]


def test_portfolio_build_is_consistent():
    from app.core import exact_numbers as exact
    from app.services import portfolio

    build = portfolio.build_portfolio(_AS_OF)
    rows = build["rows"]
    assert build["count"] == len(rows) > 0
    assert all("abc" in r and 0.0 <= r["health"] <= 100.0 for r in rows)
    assert all("abc" in r["health_components"] for r in rows)
    assert all("demand_score" in r and "margin_score" in r and "action_label" in r for r in rows)
    assert all("abc" in r["demand_components"] and "margin" in r["margin_components"] for r in rows)
    assert not any(any(k.startswith("_") for k in r) for r in rows)
    ov = build["overview"]
    assert sum(ov["by_band"].values()) == len(rows)
    assert sum(ov["by_action"].values()) == len(rows)
    assert sum(ov["by_abc"].values()) == len(rows)
    assert 0.0 <= ov["avg_health"] <= 100.0
    assert ov["total_eur_value"] == exact.money(
        sum((Decimal(str(row["eur_value"])) for row in rows), Decimal("0"))
    )
    assert ov["total_revenue_eur"] == exact.money(
        sum((Decimal(str(row["revenue_eur"])) for row in rows), Decimal("0"))
    )
    assert build["source_history_start"] == "2025-01-01"
    assert build["history_fingerprint"]


def test_mvp_endpoints_via_testclient():
    from fastapi.testclient import TestClient

    from app.api import main
    from app.core.config import get_settings
    from app.services import portfolio
    client = TestClient(main.app)
    client.headers["X-Internal-Api-Key"] = get_settings().internal_api_key

    assert client.get("/health").status_code == 200
    ov = client.get("/assortment/overview")
    assert ov.status_code == 200 and ov.json()["count"] > 0
    assert ov.json()["source_history_start"] == "2025-01-01"
    health = client.get("/assortment/health", params={"limit": 5, "sort": "frozen_eur"})
    assert health.status_code == 200 and len(health.json()["tasks"]) <= 5
    demand = client.get("/assortment/health", params={"limit": 5, "sort": "demand"})
    assert demand.status_code == 200 and len(demand.json()["tasks"]) <= 5
    demand_alias = client.get("/assortment/health", params={"limit": 5, "sort": "demand_score"})
    assert demand_alias.status_code == 200 and demand_alias.json()["sort"] == "demand"
    bad_sort = client.get("/assortment/health", params={"sort": "not_a_sort"})
    assert bad_sort.status_code == 400
    bad_region_sort = client.get("/assortment/health", params={"sort": "regional_revenue"})
    assert bad_region_sort.status_code == 400

    pid = portfolio.build_portfolio(_AS_OF)["rows"][0]["product_id"]
    prof = client.get(f"/product/{pid}")
    assert prof.status_code == 200 and prof.json()["found"] is True


def test_regional_endpoints_via_testclient():
    from fastapi.testclient import TestClient

    from app.api import main
    from app.core.config import get_settings
    client = TestClient(main.app)
    client.headers["X-Internal-Api-Key"] = get_settings().internal_api_key

    regions = client.get("/assortment/regions", params={"limit": 3})
    assert regions.status_code == 200
    body = regions.json()
    assert body["regions"]
    region_id = body["regions"][0]["region_id"]

    health = client.get(
        "/assortment/health",
        params={"region_id": region_id, "sort": "regional_revenue", "limit": 5, "stocked_only": False},
    )
    assert health.status_code == 200
    assert "regional" in health.json()["history_windows"]
    tasks = health.json()["tasks"]
    assert tasks and all(t["region_id"] == region_id for t in tasks)
    assert all("regional_revenue_eur" in t and "regional_units" in t for t in tasks)

    product_regions = client.get(f"/product/{tasks[0]['product_id']}/regions", params={"limit": 5})
    assert product_regions.status_code == 200
    assert product_regions.json()["regions"]


def test_phase3_endpoints_via_testclient():
    from fastapi.testclient import TestClient

    from app.api import main
    from app.core.config import get_settings
    client = TestClient(main.app)
    client.headers["X-Internal-Api-Key"] = get_settings().internal_api_key

    margin = client.get("/assortment/margin", params={"limit": 5})
    assert margin.status_code == 200
    assert "summary" in margin.json() and "negative" in margin.json()

    rets = client.get("/assortment/returns")
    assert rets.status_code == 200 and "high_returns" in rets.json()

    subs = client.get("/product/25804318/substitutes", params={"limit": 5})
    assert subs.status_code == 200
    body = subs.json()
    assert "candidates" in body and "in_stock_count" in body
