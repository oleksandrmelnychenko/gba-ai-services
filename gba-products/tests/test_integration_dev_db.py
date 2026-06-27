"""DB-backed smoke against the dev ConcordDb_V5. Marked integration; skipped if the DB is unreachable."""
from __future__ import annotations

from datetime import UTC, datetime

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


def test_per_product_signals_for_a_stocked_sku():
    from app.data import signals_repository as sig
    stock = sig.on_hand_stock()
    pid = int(stock[0]["product_id"])
    vel = sig.sales_velocity(_AS_OF, 365, [pid])
    price = sig.avg_sale_price_eur(_AS_OF, 365, [pid])
    rets = sig.returns_for_products(_AS_OF, 365, [pid])
    meta = sig.product_meta([pid])
    assert isinstance(vel, list) and isinstance(price, list) and isinstance(rets, list)
    assert pid in meta and "name" in meta[pid]


def test_snapshot_runs_end_to_end():
    from app.services import stock_health
    snap = stock_health.snapshot(_AS_OF)
    assert snap["total_skus"] > 0
    assert set(snap["bands"]) and snap["total_eur_value"] >= 0
    assert sum(b["count"] for b in snap["bands"].values()) == snap["total_skus"]


def test_portfolio_build_is_consistent():
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


def test_mvp_endpoints_via_testclient():
    from fastapi.testclient import TestClient

    from app.api import main
    from app.services import portfolio
    client = TestClient(main.app)

    assert client.get("/health").status_code == 200
    ov = client.get("/assortment/overview")
    assert ov.status_code == 200 and ov.json()["count"] > 0
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
    client = TestClient(main.app)

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
    tasks = health.json()["tasks"]
    assert tasks and all(t["region_id"] == region_id for t in tasks)
    assert all("regional_revenue_eur" in t and "regional_units" in t for t in tasks)

    product_regions = client.get(f"/product/{tasks[0]['product_id']}/regions", params={"limit": 5})
    assert product_regions.status_code == 200
    assert product_regions.json()["regions"]


def test_phase3_endpoints_via_testclient():
    from fastapi.testclient import TestClient

    from app.api import main
    client = TestClient(main.app)

    margin = client.get("/assortment/margin", params={"limit": 5})
    assert margin.status_code == 200
    assert "summary" in margin.json() and "negative" in margin.json()

    rets = client.get("/assortment/returns")
    assert rets.status_code == 200 and "high_returns" in rets.json()

    subs = client.get("/product/25804318/substitutes", params={"limit": 5})
    assert subs.status_code == 200
    body = subs.json()
    assert "candidates" in body and "in_stock_count" in body
