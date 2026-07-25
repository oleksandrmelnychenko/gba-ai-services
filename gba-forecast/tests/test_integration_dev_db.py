"""DB-backed smoke against the dev ConcordDb_V5. Marked integration; skipped if the DB is unreachable.

The live AVANTAZH (АВАНТАЖ) client check that proves the end-to-end contract lives here.
"""

from __future__ import annotations

from decimal import Decimal
from math import isfinite

import pytest

pytestmark = pytest.mark.integration

def _db_ok() -> bool:
    try:
        from app.data.db import query

        query("SELECT 1 AS ok")
        return True
    except Exception:
        return False


def _active_avantazh() -> tuple[int, str]:
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 1 ID AS client_id, CONVERT(varchar(36), NetUID) AS client_net_id
        FROM dbo.Client
        WHERE Deleted = 0
              AND NetUID IS NOT NULL
              AND (Name LIKE N'%АВАНТАЖ%' OR FullName LIKE N'%АВАНТАЖ%')
        ORDER BY ID DESC
        """
    )
    assert rows, "active АВАНТАЖ fixture is missing"
    return int(rows[0]["client_id"]), str(rows[0]["client_net_id"])


@pytest.fixture(scope="module", autouse=True)
def _require_db():
    if not _db_ok():
        pytest.skip("dev DB not reachable")


def test_resolve_avantazh_netuid():
    from app.data import signals_repository as sig

    expected_id, net_uid = _active_avantazh()

    assert sig.client_id_for_netuid(net_uid) == expected_id


def test_client_monthly_history_shape():
    from app.api import main
    from app.data import signals_repository as sig

    expected_id, net_uid = _active_avantazh()
    cid = sig.client_id_for_netuid(net_uid)
    assert cid == expected_id
    as_of = main._today()
    rows = sig.monthly_sales_by_client(cid, as_of, 24)
    series = sig.to_series(rows)
    assert isinstance(series, dict)
    for v in series.values():
        assert v >= 0


def test_forecast_endpoint_for_avantazh_client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import main
    from app.core.config import get_settings

    client = TestClient(main.app)
    client.headers["X-Internal-Api-Key"] = get_settings().internal_api_key
    expected_id, net_uid = _active_avantazh()
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["business_ready"] is True
    assert health.json()["data"]["source_ready"] is True

    resp = client.get("/forecast/sales", params={"client_net_id": net_uid, "months": 6})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ByClient", "ByProduct", "ByClientAndProduct", "meta"}
    # product not requested -> empty
    assert body["ByProduct"] == [] and body["ByClientAndProduct"] == []
    assert body["meta"]["requested"]["client_net_id"] == net_uid.lower()
    assert body["meta"]["resolved"]["client_id"] == expected_id
    assert body["meta"]["identity"] == {"client": "resolved", "product": "not_requested"}
    for p in body["ByClient"]:
        assert set(p) == {"SaleAmount", "MonthNameUK"}
        assert isinstance(p["SaleAmount"], (int, float)) and p["SaleAmount"] >= 0
        assert isinstance(p["MonthNameUK"], str) and p["MonthNameUK"]


def test_unknown_netuid_returns_empty_not_error(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import main
    from app.core.config import get_settings

    client = TestClient(main.app)
    client.headers["X-Internal-Api-Key"] = get_settings().internal_api_key
    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)
    resp = client.get("/forecast/sales", params={"client_net_id": "00000000-0000-0000-0000-000000000000"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ByClient"] == body["ByProduct"] == body["ByClientAndProduct"] == []
    assert body["meta"]["status"] == "unknown_identity"
    assert body["meta"]["identity"]["client"] == "unknown"
    assert body["meta"]["resolved"]["client_id"] is None


def test_live_identity_and_history_money_reconcile_exactly(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import main
    from app.core.config import get_settings
    from app.data import signals_repository as sig
    from app.data.db import query
    from app.services import forecast as fc

    as_of = main._today()
    synth = sig.synthetic_product_id()
    seeds = query(
        """
        SELECT TOP 1
               ca.ClientID AS client_id,
               CONVERT(varchar(36), c.NetUID) AS client_net_id,
               oi.ProductID AS product_id,
               CONVERT(varchar(36), p.NetUID) AS product_net_id,
               COUNT(DISTINCT CONVERT(char(7), o.Created, 120)) AS active_months
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.Client c ON c.ID = ca.ClientID
        JOIN dbo.Product p ON p.ID = oi.ProductID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> :synth
              AND c.Deleted = 0
              AND p.Deleted = 0
              AND c.NetUID IS NOT NULL
              AND p.NetUID IS NOT NULL
              AND o.Created >= DATEADD(
                  month, 1 - :months, DATEFROMPARTS(YEAR(:asof), MONTH(:asof), 1)
              )
              AND o.Created < :asof
        GROUP BY ca.ClientID, c.NetUID, oi.ProductID, p.NetUID
        ORDER BY active_months DESC, ca.ClientID, oi.ProductID
        """,
        {"synth": synth, "months": get_settings().history_months, "asof": as_of},
    )
    assert seeds
    seed = seeds[0]

    monkeypatch.setattr(main.cache, "get", lambda key: None)
    monkeypatch.setattr(main.cache, "set", lambda key, value: None)
    response = TestClient(main.app).get(
        "/forecast/sales",
        params={
            "client_net_id": seed["client_net_id"],
            "product_net_id": seed["product_net_id"],
            "months": 6,
        },
        headers={"X-Internal-Api-Key": get_settings().internal_api_key},
    )
    assert response.status_code == 200
    body = response.json()
    meta = body["meta"]
    assert meta["as_of"] == as_of
    assert meta["requested"] == {
        "client_net_id": str(seed["client_net_id"]).lower(),
        "product_net_id": str(seed["product_net_id"]).lower(),
    }
    assert meta["resolved"] == {
        "client_id": seed["client_id"],
        "client_net_id": str(seed["client_net_id"]).lower(),
        "product_id": seed["product_id"],
        "product_net_id": str(seed["product_net_id"]).lower(),
    }

    scope_sql = {
        "ByClient": """
            SELECT CONVERT(char(7), o.Created, 120) AS ym,
                   SUM(
                       CAST(oi.Qty AS decimal(18, 8))
                       * CAST(oi.PricePerItem AS decimal(28, 14))
                   ) AS eur
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE ca.ClientID = :client_id
                  AND oi.IsValidForCurrentSale = 1
                  AND oi.ProductID <> :synth
                  AND o.Created >= DATEADD(
                      month, 1 - :months, DATEFROMPARTS(YEAR(:asof), MONTH(:asof), 1)
                  )
                  AND o.Created < :asof
            GROUP BY CONVERT(char(7), o.Created, 120)
        """,
        "ByProduct": """
            SELECT CONVERT(char(7), o.Created, 120) AS ym,
                   SUM(
                       CAST(oi.Qty AS decimal(18, 8))
                       * CAST(oi.PricePerItem AS decimal(28, 14))
                   ) AS eur
            FROM dbo.OrderItem oi
            JOIN dbo.[Order] o ON o.ID = oi.OrderID
            WHERE oi.ProductID = :product_id
                  AND oi.IsValidForCurrentSale = 1
                  AND o.Created >= DATEADD(
                      month, 1 - :months, DATEFROMPARTS(YEAR(:asof), MONTH(:asof), 1)
                  )
                  AND o.Created < :asof
            GROUP BY CONVERT(char(7), o.Created, 120)
        """,
        "ByClientAndProduct": """
            SELECT CONVERT(char(7), o.Created, 120) AS ym,
                   SUM(
                       CAST(oi.Qty AS decimal(18, 8))
                       * CAST(oi.PricePerItem AS decimal(28, 14))
                   ) AS eur
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            WHERE ca.ClientID = :client_id
                  AND oi.ProductID = :product_id
                  AND oi.IsValidForCurrentSale = 1
                  AND oi.ProductID <> :synth
                  AND o.Created >= DATEADD(
                      month, 1 - :months, DATEFROMPARTS(YEAR(:asof), MONTH(:asof), 1)
                  )
                  AND o.Created < :asof
            GROUP BY CONVERT(char(7), o.Created, 120)
        """,
    }
    params = {
        "client_id": seed["client_id"],
        "product_id": seed["product_id"],
        "synth": synth,
        "months": get_settings().history_months,
        "asof": as_of,
    }
    for key, sql in scope_sql.items():
        rows = query(sql, params)
        values = [Decimal(str(row["eur"] or 0)) for row in rows if row.get("ym")]
        expected = {
            "month_count": len(values),
            "non_zero_month_count": sum(value > 0 for value in values),
            "total_eur": fc.eur_cents(sum(values, Decimal("0"))),
        }
        actual = meta["history"][key]
        assert actual["month_count"] == expected["month_count"]
        assert actual["non_zero_month_count"] == expected["non_zero_month_count"]
        assert actual["total_eur"] == expected["total_eur"]
        for point in body[key]:
            amount = point["SaleAmount"]
            assert isfinite(amount) and amount >= 0
            assert fc.eur_cents(amount) == amount
