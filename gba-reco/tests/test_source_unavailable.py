"""A slow or unreachable sales database is a clean 503 with a reason, or the cached list."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.api import main
from app.core.config import get_settings
from app.data import cache
from app.data.db import unavailable_reason
from app.services.recommendations import copurchase, recommender

AS_OF = "2026-03-01"
TIMEOUT = OperationalError(
    "SELECT 1", {},
    Exception("(20003, b'DB-Lib error message 20003, Adaptive Server connection timed out')"),
)
DOWN = OperationalError(
    "SELECT 1", {}, Exception("(20009, b'Unable to connect: Adaptive Server is unavailable')"),
)


def _headers() -> dict[str, str]:
    key = main.settings.internal_api_key
    return {"X-Internal-Api-Key": key} if key else {}


def _raise(exc):
    def raiser(*args, **kwargs):
        raise exc
    return raiser


def _payload(kind: str, as_of: str = AS_OF) -> dict:
    copurchase_list = kind == "copurchase"
    return {
        "customer_id": 7, "client_agreement_net_id": None, "count": 1, "discovery_count": 1,
        "recommendations": [{
            "product_id": 5, "score": 0.5, "rank": 1,
            "segment": "COPURCHASE" if copurchase_list else "LIGHT", "source": "discovery",
            "source_detail": "copurchase",
        }],
        "segment": "COPURCHASE" if copurchase_list else "LIGHT", "as_of_date": as_of,
        "source_history_start": "2025-01-01", "effective_start": "2025-01-01",
        "history_complete": True, "_fleet_refresh": "cycle-1",
    }


@pytest.fixture
def entries(monkeypatch):
    stored: dict[str, dict] = {}
    monkeypatch.setattr(cache, "get", lambda key: stored.get(key))
    monkeypatch.setattr(cache, "set", lambda key, value, ttl=None: True)
    return stored


def test_reason_separates_slow_from_unreachable():
    assert unavailable_reason(TIMEOUT) == "database_timeout"
    assert unavailable_reason(PoolTimeoutError("QueuePool limit reached")) == "database_timeout"
    assert unavailable_reason(DOWN) == "database_unavailable"


@pytest.mark.parametrize("path", ["/recommend", "/recommend/copurchase"])
@pytest.mark.parametrize(("error", "reason"), [(TIMEOUT, "database_timeout"), (DOWN, "database_unavailable")])
def test_database_failure_before_any_cached_list_is_503_with_reason(
    monkeypatch, entries, path, error, reason,
):
    monkeypatch.setattr(main.repo, "client_exists", _raise(error))
    response = TestClient(main.app).post(
        path, json={"customer_id": 7, "as_of_date": AS_OF}, headers=_headers(),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "source_unavailable", "reason": reason}
    assert response.headers["Retry-After"] == "30"


@pytest.mark.parametrize("path", ["/recommend", "/recommend/copurchase"])
def test_database_failure_during_calculation_is_503_not_500(monkeypatch, entries, path):
    monkeypatch.setattr(main.repo, "client_exists", lambda cid: True)
    monkeypatch.setattr(recommender, "recommend", _raise(TIMEOUT))
    monkeypatch.setattr(copurchase, "recommend", _raise(TIMEOUT))
    response = TestClient(main.app).post(
        path, json={"customer_id": 7, "as_of_date": AS_OF}, headers=_headers(),
    )
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "database_timeout"


@pytest.mark.parametrize("path", ["/recommend", "/recommend/copurchase"])
def test_a_query_defect_stays_a_500(monkeypatch, entries, path):
    defect = ProgrammingError("SELECT x", {}, Exception("Invalid column name"))
    monkeypatch.setattr(main.repo, "client_exists", lambda cid: True)
    monkeypatch.setattr(recommender, "recommend", _raise(defect))
    monkeypatch.setattr(copurchase, "recommend", _raise(defect))
    response = TestClient(main.app).post(
        path, json={"customer_id": 7, "as_of_date": AS_OF}, headers=_headers(),
    )
    assert response.status_code == 500


@pytest.mark.parametrize(
    ("path", "kind"), [("/recommend", "personal"), ("/recommend/copurchase", "copurchase")],
)
def test_cached_list_is_served_and_marked_while_the_database_is_down(
    monkeypatch, entries, path, kind,
):
    key = (cache.make_copurchase_key(7, AS_OF, 25) if kind == "copurchase"
           else cache.make_key(7, AS_OF, 25, True))
    entries[key] = _payload(kind)
    monkeypatch.setattr(main.repo, "client_exists", _raise(TIMEOUT))
    response = TestClient(main.app).post(
        path, json={"customer_id": 7, "as_of_date": AS_OF}, headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cached"] is True and body["degraded"] is True
    assert body["degraded_reason"] == "database_timeout"
    assert body["stale"] is False
    assert [item["product_id"] for item in body["recommendations"]] == [5]


def test_yesterdays_list_is_served_stale_and_degraded_while_the_database_is_down(
    monkeypatch, entries,
):
    key = cache.make_key(7, "2026-03-02", 25, True)
    entries[cache.to_latest(key, "2026-03-02")] = _payload("personal")
    monkeypatch.setattr(get_settings(), "serve_latest_fallback", True)
    monkeypatch.setattr(main.repo, "client_exists", _raise(DOWN))
    response = TestClient(main.app).post(
        "/recommend", json={"customer_id": 7, "as_of_date": "2026-03-02"}, headers=_headers(),
    )
    body = response.json()
    assert response.status_code == 200
    assert (body["stale"], body["degraded"], body["as_of_date"]) == (True, True, AS_OF)
    assert body["degraded_reason"] == "database_unavailable"


def test_batch_reports_the_source_failure_per_customer(monkeypatch, entries):
    monkeypatch.setattr(main.repo, "client_exists", _raise(TIMEOUT))
    response = TestClient(main.app).post(
        "/recommend/batch", json={"customer_ids": [7, 8], "as_of_date": AS_OF}, headers=_headers(),
    )
    body = response.json()
    assert response.status_code == 200 and body["failed"] == 2
    assert all(item["error"].startswith("source_unavailable:database_timeout") for item in body["errors"])
