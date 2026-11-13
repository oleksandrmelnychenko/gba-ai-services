"""Durable contacts: hermetic Mongo fixture; optional disposable real Mongo for pipeline parity."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.api import main
from app.data import mongo
from app.services import seller_contacts

UID = "11111111-1111-1111-1111-111111111111"
HEAD_UID = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 10, 22, 9, 30, 5, 123456, tzinfo=UTC)


@pytest.fixture
def fixture(monkeypatch):
    uri = os.environ.get("TEST_CONTACTS_MONGO_URI")
    if uri:
        # Opt-in only: this exact database is reserved for empty, disposable test fixtures.
        connection = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000)
    else:
        connection = mongomock.MongoClient(tz_aware=True)
    db = connection["contacts_fixture_20261022"]
    db.seller_contact_events.drop()
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    monkeypatch.setattr(mongo, "get_client", lambda: connection)
    monkeypatch.setattr(seller_contacts, "_now", lambda: NOW)
    book = {i: {"client_id": i, "name": f"Client {i}", "full_name": f"Client {i}",
                "client_net_uid": str(uuid4()), "manager_id": 1} for i in range(1, 251)}
    book[1]["full_name"] = "Клієнт [A].*"
    monkeypatch.setattr(main.signals_repository, "manager_id_for_netuid",
                        lambda uid: {UID: 1, HEAD_UID: 3}.get(uid))
    monkeypatch.setattr(main.signals_repository, "manager_names", lambda ids: {1: "Менеджер", 3: "Head"})
    monkeypatch.setattr(main.signals_repository, "contact_book_for_manager", lambda mid, cid=None: [
        row for row in book.values() if row["manager_id"] == mid and (cid is None or row["client_id"] == cid)
    ])
    monkeypatch.setattr(main.signals_repository, "is_head_of_sales", lambda uid: uid == HEAD_UID)
    # Never enter application lifespan: warmers would otherwise use real SQL.
    yield TestClient(main.app), book, db
    db.seller_contact_events.drop()
    connection.close()


def payload(client_id=1, **overrides):
    return {"event_id": str(uuid4()), "client_id": client_id, "result": "reached", "note": "Обговорили",
            "next_contact_at": "2026-10-23T12:00:00Z", "next_action": "Подзвонити", **overrides}


def post(client, body, uid=UID):
    return client.post("/cockpit/contacts", params={"manager_net_uid": uid}, json=body)


def get_queue(client, **params):
    return client.get("/cockpit/contacts", params={"manager_net_uid": UID, **params})


def get_history(client, client_id=1, uid=UID, **params):
    return client.get("/cockpit/contacts/history", params={"manager_net_uid": uid,
                                                         "client_id": client_id, **params})


def test_empty_contacts_without_ai_generation(fixture):
    client, _, db = fixture
    result = get_queue(client).json()
    assert result["counts"] == dict.fromkeys(("all", "overdue", "today", "upcoming", "unscheduled"), 0)
    assert result["contacts"] == [] and result["as_of"] == "2026-10-22"
    assert result["timezone"] == "Europe/Kyiv"
    assert db.tasks.count_documents({}) == 0


def test_trusted_identity_and_same_retry_after_deadline(fixture, monkeypatch):
    client, book, db = fixture
    body = payload(event_id="ABCDEFAB-1111-1111-1111-111111111111")
    first = post(client, body)
    assert first.status_code == 200, first.text
    event = first.json()["event"]
    assert event["event_id"] == body["event_id"].lower()
    assert event["client_name"] == book[1]["full_name"]
    assert event["client_net_uid"] == book[1]["client_net_uid"]
    assert event["manager_name"] == "Менеджер"
    assert event["at"] == "2026-10-22T09:30:05.123000Z"
    monkeypatch.setattr(seller_contacts, "_now", lambda: NOW + timedelta(days=7))
    retry = post(client, {**body, "event_id": body["event_id"].lower(),
                          "next_contact_at": "2026-10-23T15:00:00+03:00"})
    assert retry.status_code == 200 and retry.json() == first.json()
    assert db.seller_contact_events.count_documents({}) == 1
    assert db.tasks.count_documents({}) == 0


def test_retry_with_changed_payload_conflicts(fixture):
    client, _, db = fixture
    body = payload()
    assert post(client, body).status_code == 200
    for field, value in (("note", "changed"), ("client_id", 2), ("result", "no_answer")):
        changed = post(client, {**body, field: value})
        assert changed.status_code == 409
    assert db.seller_contact_events.count_documents({}) == 1


@pytest.mark.parametrize("changes", [
    {"event_id": "bad"}, {"event_id": "00000000-0000-0000-0000-000000000000"},
    {"client_id": 0}, {"client_id": True}, {"result": "done"},
    {"next_contact_at": None}, {"next_action": None}, {"next_action": "   "},
    {"next_contact_at": "2026-10-23T12:00:00"}, {"next_contact_at": 1792756800},
    {"next_contact_at": "2026-10-22T09:30:05Z"},
    {"note": "x" * 4001}, {"next_action": "x" * 501},
    {"manager_id": 99}, {"client_name": "forged"}, {"at": "2000-01-01T00:00:00Z"},
])
def test_invalid_write_rejected_without_history(fixture, changes):
    client, _, db = fixture
    assert post(client, payload(**changes)).status_code == 422
    assert db.seller_contact_events.count_documents({}) == 0


def test_current_ownership_for_write_history_and_queue_no_head_bypass(fixture):
    client, book, _ = fixture
    body = payload()
    assert post(client, body).status_code == 200
    assert post(client, payload(), HEAD_UID).status_code == 404
    assert get_history(client, uid=HEAD_UID).status_code == 404
    book[1]["manager_id"] = 3
    assert post(client, body).status_code == 404  # replay also checks current ownership
    assert get_history(client).status_code == 404
    assert get_queue(client).json()["total"] == 0
    transferred = get_history(client, uid=HEAD_UID).json()
    assert transferred["manager_id"] == 3
    assert transferred["events"][0]["manager_id"] == 1
    assert transferred["total"] == 1
    assert post(client, payload(), HEAD_UID).status_code == 200
    assert get_history(client, uid=HEAD_UID).json()["total"] == 2
    del book[1]
    assert get_history(client, uid=HEAD_UID).status_code == 404


def test_latest_per_client_full_counts_over_200_and_stable_paging(fixture, monkeypatch):
    client, _, db = fixture
    # Insert using the same service while time was before all commitments; move clock for queue reads.
    monkeypatch.setattr(seller_contacts, "_now", lambda: NOW - timedelta(days=10))
    times = ["2026-10-21T20:59:59Z", "2026-10-21T21:00:00Z",
             "2026-10-22T21:00:00Z", None]
    for client_id in range(1, 251):
        when = times[(client_id - 1) % 4]
        assert post(client, payload(client_id, next_contact_at=when,
                                   next_action="Call" if when else None)).status_code == 200
    monkeypatch.setattr(seller_contacts, "_now", lambda: NOW)
    # Newer unscheduled contact replaces queue row, but preserves old commitment in history.
    newer = post(client, payload(1, next_contact_at=None, next_action=None)).json()["event"]
    result = get_queue(client).json()
    assert result["counts"] == {"all": 250, "overdue": 62, "today": 63, "upcoming": 62, "unscheduled": 63}
    assert result["total"] == 250 and result["count"] == 20
    assert all(row["next_contact_at"] == "2026-10-21T20:59:59Z" for row in result["contacts"])
    ids = []
    for offset in range(0, 260, 20):
        page = get_queue(client, offset=offset).json()
        assert page["total"] == 250
        ids += [row["client_id"] for row in page["contacts"]]
    assert len(ids) == len(set(ids)) == 250
    assert get_queue(client, bucket="today").json()["total"] == 63
    history = get_history(client).json()
    assert history["total"] == 2 and history["events"][0] == newer
    assert db.tasks.count_documents({}) == 0


def test_search_is_literal_case_insensitive_and_latest_event_only(fixture):
    client, _, _ = fixture
    assert post(client, payload()).status_code == 200
    assert post(client, payload(2, next_action="Proposal [B] SPECIAL")).status_code == 200
    result = get_queue(client, search="  [a].*  ").json()
    assert result["search"] == "[a].*" and result["total"] == 1
    assert result["contacts"][0]["client_id"] == 1
    assert get_queue(client, search="special").json()["total"] == 1
    assert post(client, payload(2, next_action=None, next_contact_at=None)).status_code == 200
    assert get_queue(client, search="special").json()["total"] == 0
    assert get_queue(client, search=".*").json()["total"] == 1
    assert get_queue(client, search="x" * 201).status_code == 422


def test_history_order_same_millisecond_and_retry_never_reorders(fixture):
    client, _, _ = fixture
    bodies = [payload(note=f"note {i}") for i in range(5)]
    for body in bodies:
        assert post(client, body).status_code == 200
    assert post(client, bodies[0]).status_code == 200
    history = get_history(client, offset=1, limit=2).json()
    assert history["total"] == 5 and history["count"] == 2
    assert [e["note"] for e in history["events"]] == ["note 3", "note 2"]
    assert get_queue(client).json()["contacts"][0]["note"] == "note 4"


def test_parallel_identical_retry_is_one_event_without_secondary_indexes(fixture):
    client, _, db = fixture
    body = payload()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: post(client, body), range(16)))
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert db.seller_contact_events.count_documents({}) == 1
    assert list(db.seller_contact_events.index_information()) == ["_id_"]


@pytest.mark.parametrize(("now", "hours"), [
    (datetime(2026, 3, 29, 10, tzinfo=UTC), 23),
    (datetime(2026, 10, 25, 10, tzinfo=UTC), 25),
])
def test_kyiv_day_bounds_follow_dst(now, hours):
    start, end = seller_contacts.day_bounds(now)
    assert (end - start).total_seconds() == hours * 3600
    assert start.astimezone(seller_contacts.KYIV).hour == 0
    assert end.astimezone(seller_contacts.KYIV).hour == 0


def test_kyiv_midnight_not_utc_date(fixture, monkeypatch):
    client, _, _ = fixture
    monkeypatch.setattr(seller_contacts, "_now", lambda: datetime(2026, 10, 21, 21, tzinfo=UTC))
    assert get_queue(client).json()["as_of"] == "2026-10-22"


def test_contact_indexes_no_ttl_or_task_cleanup(fixture):
    _, _, db = fixture
    mongo.ensure_indexes()
    indexes = db.seller_contact_events.index_information()
    assert "ix_contact_latest" in indexes and "ix_contact_client_history" in indexes
    assert all("expireAfterSeconds" not in index for index in indexes.values())


def test_missing_explicit_agreement_fields_rejected(fixture):
    client, _, db = fixture
    body = payload()
    del body["next_contact_at"]
    del body["next_action"]
    assert post(client, body).status_code == 422
    assert db.seller_contact_events.count_documents({}) == 0


def test_blank_trusted_names_and_null_net_uid_have_safe_display_fallback(fixture, monkeypatch):
    client, book, _ = fixture
    book[1].update(full_name=" ", name=None, client_net_uid=None)
    monkeypatch.setattr(main.signals_repository, "manager_names", lambda ids: {1: None})
    response = post(client, payload())
    assert response.status_code == 200
    event = response.json()["event"]
    assert event["client_name"] == event["manager_name"] == "1"
    assert event["client_net_uid"] is None


def test_contract_fixture_export(fixture):
    """Optional cross-service fixture export contains only synthetic test records."""
    client, _, _ = fixture
    response = post(client, payload(event_id="abcdefab-1111-1111-1111-111111111111"))
    assert response.status_code == 200
    target = os.environ.get("TEST_CONTACTS_FIXTURE_EXPORT")
    if target:
        import json
        from pathlib import Path
        data = {"post": response.json(), "queue": get_queue(client).json(),
                "history": get_history(client).json()}
        Path(target).write_text(json.dumps(data, ensure_ascii=False, indent=2))
