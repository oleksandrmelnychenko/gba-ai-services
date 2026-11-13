"""/cockpit/clients reuses one short-lived client-book snapshot across pages and filters."""
from __future__ import annotations

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api import main

UID = "11111111-1111-1111-1111-111111111111"
AS_OF = "2026-11-12"


def _client(cid: int) -> dict:
    return {"client_id": cid, "client_net_uid": f"00000000-0000-0000-0000-{cid:012d}",
            "name": f"Client {cid}", "full_name": f"Client {cid}", "phone": None, "email": None,
            "last_order": None, "orders_cnt": 0, "retained_orders_cnt": 0, "turnover_eur": 10 * cid,
            "_due_count": 0, "_overdue_count": 0, "_cooldown_cycle_start": None}


@pytest.fixture
def api(monkeypatch):
    db = mongomock.MongoClient()["t"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "seller_contact_events", lambda: db["seller_contact_events"])
    calls = []
    debts = {"rows": []}
    monkeypatch.setattr(main.signals_repository, "manager_id_for_netuid",
                        lambda uid: 1 if uid == UID else 2)
    monkeypatch.setattr(main.signals_repository, "_excluded", lambda day: [])
    monkeypatch.setattr(
        main.repeat_sales, "read_snapshot",
        lambda mid, day: calls.append((mid, day)) or ([_client(1), _client(2), _client(3)], [], set()))
    monkeypatch.setattr(main.signals_repository, "overdue_debts_for_manager",
                        lambda *a, **kw: debts["rows"])
    return TestClient(main.app), calls, debts


def _get(client, **extra):
    response = client.get("/cockpit/clients",
                          params={"manager_net_uid": UID, "as_of_date": AS_OF, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def test_pages_searches_and_segments_share_one_snapshot(api):
    client, calls, _ = api
    first = _get(client, limit=2)
    second = _get(client, limit=2, offset=2)
    searched = _get(client, search="Client 3")
    segment = _get(client, segment="new_client")
    assert calls == [(1, AS_OF)]
    assert [row["client_id"] for row in first["clients"]] == [1, 2]
    assert [row["client_id"] for row in second["clients"]] == [3]
    assert [row["client_id"] for row in searched["clients"]] == [3]
    assert segment["total"] == 3
    assert first["clients"][0]["turnover_eur"] == 10.0


def test_cached_snapshot_is_not_mutated_and_debts_stay_live(api):
    client, calls, debts = api
    assert _get(client)["clients"][0]["overdue_eur"] == 0.0
    debts["rows"] = [{"client_id": 1, "overdue_amount": 55.5, "max_days_past_terms": 9,
                      "opening_balance_amount": 40.25, "opening_balance_lines": 2}]
    body = _get(client)
    assert body["clients"][0]["overdue_eur"] == 55.5
    assert body["clients"][0]["max_days_past_terms"] == 9
    assert body["clients"][0]["debt_opening_balance_eur"] == 40.25
    assert body["clients"][0]["debt_opening_balance_count"] == 2
    assert calls == [(1, AS_OF)]
    cached = main._client_book_state[(1, AS_OF, "2025-01-01")]["value"][0]
    assert "overdue_eur" not in cached[0] and "_due_count" in cached[0]


def test_cache_is_keyed_by_manager_and_date_and_expires(api, monkeypatch):
    client, calls, _ = api
    _get(client)
    client.get("/cockpit/clients", params={"manager_net_uid": "22222222-2222-2222-2222-222222222222",
                                           "as_of_date": AS_OF})
    _get(client, as_of_date="2026-11-11")
    assert calls == [(1, AS_OF), (2, AS_OF), (1, "2026-11-11")]
    monkeypatch.setattr(main, "_CLIENT_BOOK_TTL_S", 0.0)
    _get(client)
    assert calls[-1] == (1, AS_OF) and len(calls) == 4


def test_failed_snapshot_is_not_cached(api, monkeypatch):
    client, calls, _ = api

    def broken(mid, day):
        raise RuntimeError("sql_unavailable")

    monkeypatch.setattr(main.repeat_sales, "read_snapshot", broken)
    failed = client.get("/cockpit/clients", params={"manager_net_uid": UID, "as_of_date": AS_OF})
    assert failed.status_code == 500
    assert main._client_book_state == {}
