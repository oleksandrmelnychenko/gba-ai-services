"""DB-backed smoke against the dev ConcordDb_V5. Marked integration; skipped if the DB is unreachable.

The live AVANTAZH (АВАНТАЖ) client check that proves the end-to-end contract lives here.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# АВАНТАЖ client NetUID resolved from dbo.Client (FullName LIKE '%АВАНТАЖ%') on the dev DB.
_AVANTAZH_NETUID = "7845841E-0678-4364-A346-2CE21C7378AB"


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


def test_resolve_avantazh_netuid():
    from app.data import signals_repository as sig

    cid = sig.client_id_for_netuid(_AVANTAZH_NETUID)
    assert cid is not None and cid > 0


def test_client_monthly_history_shape():
    from datetime import UTC, datetime

    from app.data import signals_repository as sig

    cid = sig.client_id_for_netuid(_AVANTAZH_NETUID)
    as_of = datetime.now(UTC).strftime("%Y-%m-%d")
    rows = sig.monthly_sales_by_client(cid, as_of, 24)
    series = sig.to_series(rows)
    assert isinstance(series, dict)
    for v in series.values():
        assert v >= 0


def test_forecast_endpoint_for_avantazh_client():
    from fastapi.testclient import TestClient

    from app.api import main

    client = TestClient(main.app)

    assert client.get("/health").status_code == 200

    resp = client.get("/forecast/sales", params={"client_net_id": _AVANTAZH_NETUID, "months": 6})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ByClient", "ByProduct", "ByClientAndProduct"}
    # product not requested -> empty
    assert body["ByProduct"] == [] and body["ByClientAndProduct"] == []
    for p in body["ByClient"]:
        assert set(p) == {"SaleAmount", "MonthNameUK"}
        assert isinstance(p["SaleAmount"], (int, float)) and p["SaleAmount"] >= 0
        assert isinstance(p["MonthNameUK"], str) and p["MonthNameUK"]


def test_unknown_netuid_returns_empty_not_error():
    from fastapi.testclient import TestClient

    from app.api import main

    client = TestClient(main.app)
    resp = client.get("/forecast/sales", params={"client_net_id": "00000000-0000-0000-0000-000000000000"})
    assert resp.status_code == 200
    assert resp.json() == {"ByClient": [], "ByProduct": [], "ByClientAndProduct": []}
