from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.api import main
from app.data import mongo, signals_repository
from app.services import task_delegation

HEAD = "11111111-1111-1111-1111-111111111111"
SELLER = "22222222-2222-2222-2222-222222222222"
NOW = datetime.now(UTC).replace(microsecond=0)

@pytest.fixture
def fixture(monkeypatch):
    uri = os.getenv("TEST_WORKDAY_MONGO_URI")
    if not uri:
        pytest.skip("disposable Mongo URI required")
    connection = MongoClient(uri, tz_aware=True)
    name = "head_coaching_fixture_" + uuid4().hex
    db = connection[name]
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    monkeypatch.setattr(signals_repository, "manager_id_for_netuid", lambda uid: 9 if uid == HEAD else 2)
    monkeypatch.setattr(signals_repository, "is_head_of_sales", lambda uid: uid == HEAD)
    monkeypatch.setattr(signals_repository, "all_managers", lambda: [1, 2])
    monkeypatch.setattr(signals_repository, "task_manager_ids", lambda ids: [1, 2])
    monkeypatch.setattr(signals_repository, "manager_names", lambda ids: {mid: f"Manager {mid}" for mid in ids})
    book = [{"client_id": i, "manager_id": 1, "client_name": f"Client {i}", "name": f"Client {i}"} for i in range(1, 252)]
    monkeypatch.setattr(signals_repository, "query", lambda sql, params: [r for r in book if r["manager_id"] in params.values()])
    monkeypatch.setattr(signals_repository, "contact_book_for_manager", lambda mid, cid=None: [r for r in book if r["manager_id"] == mid and (cid is None or r["client_id"] == cid)])
    monkeypatch.setattr(signals_repository, "contacts_for_clients", lambda ids: {r["client_id"]: r for r in book if r["client_id"] in ids})
    yield TestClient(main.app), db, book
    connection.drop_database(name)
    connection.close()

def task(db, key="one", **kw):
    row = {"task_key": key, "manager_id": 1, "client_id": 1, "client_name": "Client 1", "title": "Call", "task_type": "manual", "status": "open", "updated_at": NOW, "due_date": NOW, **kw}
    db.tasks.insert_one(row)
    return row

def assign(client, **overrides):
    payload = {"task_key": "one", "expected_manager_id": 1, "expected_updated_at": NOW.isoformat(), "manager_id": 2, "clarification": "Call today", **overrides}
    return client.post("/cockpit/head/tasks/assign", params={"manager_net_uid": HEAD}, json=payload)

def test_task_only_reassignment_is_atomic_and_preserves_sale_credit(fixture):
    client, db, book = fixture
    task(db)
    db.explicit_sale_attributions.insert_one({"_id": "sale", "manager_id": 1, "task_key": "one"})
    assert assign(client).status_code == 200
    after = db.tasks.find_one({"task_key": "one"})
    assert after["manager_id"] == 2 and after["assignment_mode"] == "head_task_only"
    assert after["assignment_history"][0]["by"] == 9
    assert book[0]["manager_id"] == 1
    assert db.explicit_sale_attributions.find_one()["manager_id"] == 1
    assert assign(client).status_code == 409
    assert len(db.tasks.find_one()["assignment_history"]) == 1

def test_delegated_context_expires_with_assignment(fixture):
    client, db, _ = fixture
    task(db)
    assert task_delegation.contact_book(2, 1) == []
    assert assign(client).status_code == 200
    assert task_delegation.contact_book(2, 1)[0]["delegated_task_context"]
    assert task_delegation.contact_book(2, 2) == []
    assert client.get("/cockpit/contacts/history", params={"manager_net_uid": SELLER, "client_id": 1}).status_code == 200
    db.tasks.update_one({"task_key": "one"}, {"$set": {"status": "done"}})
    assert task_delegation.contact_book(2, 1) == []
    assert client.get("/cockpit/contacts/history", params={"manager_net_uid": SELLER, "client_id": 1}).status_code == 404

@pytest.mark.parametrize("status", ["done", "dismissed", "superseded"])
def test_closed_tasks_cannot_be_reassigned(fixture, status):
    client, db, _ = fixture
    task(db, status=status)
    assert assign(client).status_code == 409

@pytest.mark.parametrize("changes", [{"manager_id": 999}, {"clarification": " "}, {"expected_updated_at": "2026-10-22T10:00:00"}])
def test_invalid_assignment_has_no_write(fixture, changes):
    client, db, _ = fixture
    task(db)
    assert assign(client, **changes).status_code == 422
    assert db.tasks.find_one()["manager_id"] == 1

def test_head_authorization(fixture):
    client, db, _ = fixture
    assert client.get("/cockpit/head/coaching", params={"manager_net_uid": SELLER}).status_code == 403
    task(db)
    payload = {"task_key": "one", "expected_manager_id": 1, "expected_updated_at": NOW.isoformat(), "manager_id": 2, "clarification": "Test"}
    assert client.post("/cockpit/head/tasks/assign", params={"manager_net_uid": SELLER}, json=payload).status_code == 403
    assert db.tasks.find_one()["manager_id"] == 1

def test_full_counts_pagination_and_latest_commitments(fixture):
    client, db, _ = fixture
    for i in range(1, 252):
        task(db, str(i), client_id=i, task_type="reorder_due" if i < 51 else "manual")
    old = NOW - timedelta(days=4)
    db.seller_contact_events.insert_many([
        {"client_id": 1, "at": old, "next_contact_at": old},
        {"client_id": 1, "at": NOW, "next_contact_at": NOW + timedelta(days=2)},
        {"client_id": 2, "at": old, "next_contact_at": old}])
    params = {"manager_net_uid": HEAD, "bucket": "workload", "offset": 240, "limit": 20}
    response = client.get("/cockpit/head/coaching", params=params)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 251 and page["count"] == 11
    assert page["counts"] == {"workload": 251, "repeat_tasks": 50, "overdue_promises": 1, "without_next_contact": 249}
    page2 = client.get("/cockpit/head/coaching", params={**params, "manager_id": 2}).json()
    assert page2["total"] == 0 and page2["rows"] == []
    assert client.get("/cockpit/head/coaching", params={**params, "date_from": "2026-10-23", "date_to": "2026-10-21"}).status_code == 422
    assert client.get("/cockpit/head/coaching", params={**params, "manager_id": 999}).status_code == 422

def test_status_filter_does_not_hide_contact_commitments(fixture):
    client, db, _ = fixture
    task(db, status="in_progress")
    result = client.get("/cockpit/head/coaching", params={"manager_net_uid": HEAD, "status": "open", "bucket": "without_next_contact"}).json()
    assert result["counts"]["workload"] == 0
    assert result["total"] == 251


@pytest.mark.parametrize("route,payload", [
    ("status", {"to": "done"}), ("notes", {"text": "Old owner note"}),
])
def test_assignment_after_route_ownership_check_rejects_former_owner(fixture, monkeypatch, route, payload):
    from app.services import lifecycle
    client, db, _ = fixture
    task(db, manager_id=2)
    original = lifecycle.get_task
    def read_then_assign(key):
        snapshot = original(key)
        task_delegation.assign(9, task_delegation.AssignmentRequest(
            task_key=key, expected_manager_id=2, expected_updated_at=NOW,
            manager_id=1, clarification="New assignee"))
        return snapshot
    monkeypatch.setattr(lifecycle, "get_task", read_then_assign)
    response = client.post("/cockpit/"+route, params={"manager_net_uid": SELLER},
                           json={"task_key": "one", **payload})
    assert response.status_code == 409, response.text
    row = db.tasks.find_one({"task_key": "one"})
    assert row["manager_id"] == 1 and row["status"] == "open"
    assert all(note["text"] != "Old owner note" for note in row.get("notes", []))
    assert db.task_events.count_documents({}) == 0


def test_assignment_between_lifecycle_read_and_status_write_is_cas_guarded(fixture, monkeypatch):
    from app.domain.models import TaskStatus
    from app.services import lifecycle
    _, db, _ = fixture
    task(db, manager_id=2)
    collection = db.tasks
    monkeypatch.setattr(mongo, "tasks", lambda: collection)
    original = collection.find_one_and_update
    def assign_before_write(selector, update, **kwargs):
        if update.get("$set", {}).get("status") == "done":
            task_delegation.assign(9, task_delegation.AssignmentRequest(
                task_key="one", expected_manager_id=2, expected_updated_at=NOW,
                manager_id=1, clarification="Concurrent assignment"))
        return original(selector, update, **kwargs)
    monkeypatch.setattr(collection, "find_one_and_update", assign_before_write)
    with pytest.raises(lifecycle.TaskConflictError):
        lifecycle.change_status("one", TaskStatus.DONE, 2,
                                expected_manager_id=2, expected_updated_at=NOW)
    assert db.tasks.find_one()["status"] == "open"
    assert db.tasks.find_one()["manager_id"] == 1


def test_head_override_and_assignment_clarification_are_visible_to_assignee(fixture):
    client, db, _ = fixture
    previous = {"author_id": 1, "text": "Original promise", "created_at": NOW}
    task(db, notes=[previous])
    assert assign(client).status_code == 200
    result = client.get("/cockpit/workday/tasks", params={"manager_net_uid": SELLER,
        "key": "client:1", "scope": "active"})
    assert result.status_code == 200, result.text
    row = result.json()["tasks"][0]
    assert [note["text"] for note in row["notes"]] == [
        "Original promise", "Призначення / уточнення: Call today"]
    assert row["notes"][-1]["author_id"] == 9
    assert row["notes"][-1]["author_name"] == "Manager 9"
    # Authenticated head can still act on another manager's manual task.
    assert client.post("/cockpit/notes", params={"manager_net_uid": HEAD},
                       json={"task_key": "one", "text": "Head follow-up"}).status_code == 200
    assert client.post("/cockpit/status", params={"manager_net_uid": HEAD},
                       json={"task_key": "one", "to": "done"}).status_code == 200



def test_scoped_notes_advance_revision_with_frozen_clock(fixture, monkeypatch):
    from app.services import lifecycle
    _, db, _ = fixture
    task(db)
    monkeypatch.setattr(lifecycle, "_now", lambda: NOW)
    row = lifecycle.add_note("one", 1, "First", expected_manager_id=1, expected_updated_at=NOW)
    assert row["updated_at"] == NOW + timedelta(milliseconds=1)
    with pytest.raises(lifecycle.TaskConflictError):
        lifecycle.add_note("one", 1, "Stale", expected_manager_id=1, expected_updated_at=NOW)
    assert [entry["text"] for entry in db.tasks.find_one()["notes"]] == ["First"]



def test_zero_book_assignee_remains_visible_and_filter_keeps_all_recipient_choices(fixture):
    client, db, _ = fixture
    task(db)
    assert assign(client).status_code == 200
    response = client.get("/cockpit/head/coaching", params={"manager_net_uid": HEAD,
        "manager_id": 2, "bucket": "workload"})
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 1 and page["rows"][0]["manager_id"] == 2
    assert [manager["id"] for manager in page["managers"]] == [1, 2]
    board = client.get("/head/tasks", params={"manager_net_uid": HEAD, "manager_id": 2})
    assert board.status_code == 200, board.text
    assert board.json()["total"] == 1


def test_new_manual_task_keeps_explicit_assignee_and_grants_active_context(fixture):
    client, db, _ = fixture
    response = client.post("/head/tasks/new", params={"manager_net_uid": HEAD},
        json={"manager_id": 2, "client_id": 1, "title": "Delegated call"})
    assert response.status_code == 200, response.text
    row = db.tasks.find_one()
    assert row["assignment_mode"] == "head_task_only" and row["manager_id"] == 2
    assert task_delegation.contact_book(2, 1)[0]["delegated_task_context"]
    db.tasks.update_one({"_id": row["_id"]}, {"$set": {"status": "done"}})
    assert task_delegation.contact_book(2, 1) == []
