"""Real Mongo aggregation parity; opt-in disposable database, never business SQL or Mongo."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.api import main
from app.data import mongo
from app.services import seller_contacts, workday

UID = "11111111-1111-1111-1111-111111111111"
OTHER_UID = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 10, 22, 10, tzinfo=UTC)


@pytest.fixture
def fixture(monkeypatch):
    uri = os.environ.get("TEST_WORKDAY_MONGO_URI")
    if not uri:
        pytest.skip("requires disposable Mongo: TEST_WORKDAY_MONGO_URI")
    connection = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000)
    db = connection["workday_fixture_20261022"]
    db.tasks.drop()
    db.seller_contact_events.drop()
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    monkeypatch.setattr(workday, "_now", lambda: NOW)
    monkeypatch.setattr(seller_contacts, "_now", lambda: NOW)
    book = {i: {"client_id": i, "name": f"Клієнт {i}", "full_name": f"Клієнт {i}",
                "client_net_uid": str(uuid4()), "manager_id": 1} for i in range(1, 251)}
    monkeypatch.setattr(main.signals_repository, "manager_id_for_netuid",
                        lambda uid: {UID: 1, OTHER_UID: 3}.get(uid))
    monkeypatch.setattr(main.signals_repository, "manager_names",
                        lambda ids: {1: "Продавець", 3: "Новий менеджер"})
    monkeypatch.setattr(main.signals_repository, "contact_book_for_manager", lambda mid, cid=None: [
        row for row in book.values() if row["manager_id"] == mid and (cid is None or row["client_id"] == cid)
    ])
    yield TestClient(main.app), book, db
    db.tasks.drop()
    db.seller_contact_events.drop()
    connection.close()


def task(db, key="task-1", cid=1, **overrides):
    document = {"task_key": key, "client_id": cid, "manager_id": 1, "client_name": f"Клієнт {cid}",
                "task_type": "reorder_due", "status": "open", "title": "Зателефонувати",
                "reason": "Повторна покупка", "urgency": "normal", "priority": 30,
                "due_date": None, "generated_at": NOW, "updated_at": NOW, "notes": [],
                "status_history": [], **overrides}
    db.tasks.insert_one(document)
    return document


def contact(db, cid=1, due=None, **overrides):
    document = {"event_id": str(uuid4()), "client_id": cid, "manager_id": 1,
                "client_name": f"Клієнт {cid}", "client_net_uid": None, "manager_name": "Автор",
                "result": "reached", "note": "Домовились", "next_contact_at": due,
                "next_action": "Наступний дзвінок" if due else None, "at": NOW,
                "sort_id": ObjectId(), **overrides}
    db.seller_contact_events.insert_one(document)
    return document


def queue(client, uid=UID, **params):
    response = client.get("/cockpit/workday", params={"manager_net_uid": uid, **params})
    assert response.status_code == 200, response.text
    return response.json()


def details(client, key="client:1", **params):
    return client.get("/cockpit/workday/tasks", params={"manager_net_uid": UID, "key": key, **params})


def test_empty_and_out_of_range(fixture):
    client, _, db = fixture
    page = queue(client)
    assert page["counts"] == dict.fromkeys(["all", *workday.BUCKETS], 0)
    task(db)
    page = queue(client, offset=200)
    assert page["count"] == 0 and page["total"] == 1


def test_more_than_200_clients_and_duplicates_use_unique_group_counts(fixture):
    client, _, db = fixture
    for cid in range(1, 251):
        task(db, f"a-{cid}", cid)
        task(db, f"b-{cid}", cid)
    before = list(db.tasks.find())
    keys = []
    for offset in range(0, 250, 20):
        page = queue(client, offset=offset)
        assert page["total"] == page["counts"]["unscheduled"] == 250
        assert all(row["task_count"] == 2 for row in page["rows"])
        keys.extend(row["key"] for row in page["rows"])
    assert len(keys) == len(set(keys)) == 250
    assert list(db.tasks.find()) == before  # read paths do not persist ranking hydration


def test_group_primary_order_any_child_search_and_contact_only(fixture):
    client, _, db = fixture
    task(db, "manual", task_type="manual", urgency="low", due_date=NOW - timedelta(days=3))
    task(db, "critical", urgency="critical", ev_score=90000)
    task(db, "needle", title="Literal [A].*", priority=None, ev_score=None)
    contact(db, due=NOW + timedelta(days=3))
    contact(db, 2, due=NOW)
    page = queue(client, search="[a].*")
    assert page["total"] == 1
    row = page["rows"][0]
    assert row["primary_task"]["task_key"] == "manual"
    assert row["task_count"] == 3 and row["manual_count"] == 1
    assert row["bucket"] == "overdue"  # later commitment cannot erase earlier deadline
    assert row["latest_contact"] is not None
    assert queue(client, search="not present")["total"] == 0
    row = next(row for row in queue(client)["rows"] if row["client_id"] == 2)
    assert row["primary_task"] is None and row["task_count"] == 0 and row["can_contact"]


def test_manual_no_client_and_out_of_book_but_foreign_ai_hidden(fixture):
    client, _, db = fixture
    task(db, "standalone:with:colon", 0, task_type="manual")
    task(db, "outside-manual", 999, task_type="manual")
    task(db, "outside-ai", 999)
    task(db, "other-manager", 1, manager_id=3)
    task(db, "generated", 2, status="generated")
    page = queue(client)
    assert page["total"] == 2
    assert all(not row["can_contact"] for row in page["rows"])
    assert {row["key"] for row in page["rows"]} == {"task:standalone:with:colon", "client:999"}
    assert details(client, "task:standalone:with:colon").json()["total"] == 1
    assert details(client, "client:999").json()["total"] == 1
    assert details(client, "task:other-manager").status_code == 404
    assert details(client, "client:998").status_code == 404


def test_history_scope_is_own_terminal_tasks_with_notes(fixture):
    client, _, db = fixture
    task(db, "open")
    for status in workday.HISTORY:
        task(db, status, status=status, notes=[{"text": "Історія", "created_at": NOW, "author_id": 1}],
             status_history=[{"from": "open", "to": status, "at": NOW, "by": 1}])
    task(db, "foreign", manager_id=3, status="done")
    assert details(client).json()["total"] == 1
    page = details(client, scope="history", limit=2).json()
    assert page["total"] == len(workday.HISTORY) and len(page["tasks"]) == 2
    assert page["tasks"][0]["notes"][0]["created_at"].endswith("Z")
    assert details(client, scope="history", offset=2).json()["count"] == len(workday.HISTORY) - 2


def test_contact_history_follows_current_owner_without_rewriting_authors(fixture):
    client, book, db = fixture
    original = contact(db, due=NOW)
    task(db)
    book[1]["manager_id"] = 3
    assert queue(client)["total"] == 0
    assert details(client).status_code == 404
    assert client.get("/cockpit/contacts/history",
                      params={"manager_net_uid": UID, "client_id": 1}).status_code == 404
    page = queue(client, uid=OTHER_UID)
    assert page["manager_id"] == page["rows"][0]["manager_id"] == 3
    assert page["rows"][0]["latest_contact"]["manager_id"] == 1
    history = client.get("/cockpit/contacts/history",
                         params={"manager_net_uid": OTHER_UID, "client_id": 1}).json()
    assert history["events"][0]["manager_id"] == 1
    contacts = client.get("/cockpit/contacts", params={"manager_net_uid": OTHER_UID}).json()
    assert contacts["contacts"][0]["manager_id"] == 1
    assert db.seller_contact_events.find_one({"_id": original["_id"]}) == original


@pytest.mark.parametrize("now,hours", [
    (datetime(2026, 3, 29, 10, tzinfo=UTC), 23),
    (datetime(2026, 10, 25, 10, tzinfo=UTC), 25),
    (NOW, 24),
])
def test_exclusive_buckets_and_kyiv_dst_boundaries(fixture, monkeypatch, now, hours):
    client, _, db = fixture
    monkeypatch.setattr(workday, "_now", lambda: now)
    start, end = seller_contacts.day_bounds(now)
    assert (end - start).total_seconds() == hours * 3600
    task(db, "overdue", 1, due_date=start - timedelta(milliseconds=1), status="in_progress")
    task(db, "today-start", 2, due_date=start)
    task(db, "today-end", 3, due_date=end - timedelta(milliseconds=1))
    task(db, "upcoming", 4, due_date=end)
    task(db, "progress", 5, status="in_progress", due_date=end)
    task(db, "unscheduled", 6)
    page = queue(client)
    assert page["counts"] == {"all": 6, "overdue": 1, "today": 2, "in_progress": 1,
                               "upcoming": 1, "unscheduled": 1}
    assert [row["key"] for row in page["rows"]] == [f"client:{i}" for i in [1, 2, 3, 5, 4, 6]]
    for bucket in workday.BUCKETS:
        filtered = queue(client, bucket=bucket)
        assert filtered["total"] == page["counts"][bucket]
        assert all(row["bucket"] == bucket for row in filtered["rows"])


def test_snoozed_future_defers_old_due_but_does_not_hide_other_due(fixture):
    client, _, db = fixture
    task(db, "snooze", 1, status="snoozed", due_date=NOW - timedelta(days=2),
         snooze_until=NOW + timedelta(days=2))
    task(db, "woken", 2, status="snoozed", due_date=NOW - timedelta(days=2),
         snooze_until=NOW - timedelta(minutes=1))
    page = queue(client)
    assert {row["client_id"]: row["bucket"] for row in page["rows"]} == {1: "upcoming", 2: "overdue"}
    contact(db, due=NOW)
    assert next(row for row in queue(client)["rows"] if row["client_id"] == 1)["bucket"] == "today"


def test_latest_contact_wins_and_unscheduled_does_not_clear_task_due(fixture):
    client, _, db = fixture
    contact(db, due=NOW - timedelta(days=2), at=NOW - timedelta(minutes=1))
    contact(db, due=None)
    assert queue(client)["rows"][0]["bucket"] == "unscheduled"
    task(db, due_date=NOW)
    assert queue(client)["rows"][0]["bucket"] == "today"


@pytest.mark.parametrize("params", [{"bucket": "invalid"}, {"offset": -1}, {"limit": 0},
                                     {"limit": 201}, {"search": "x" * 201}])
def test_invalid_queue_inputs(fixture, params):
    client, _, _ = fixture
    assert client.get("/cockpit/workday", params={"manager_net_uid": UID, **params}).status_code == 422


@pytest.mark.parametrize("key", [
    "bad", "client:0", "client:-1", "client:abc", "task:", "client:99999999999999999999999999999",
])
def test_invalid_group_keys(fixture, key):
    assert details(fixture[0], key).status_code == 422


def test_synthetic_contract_artifacts(fixture):
    client, _, db = fixture
    task(db, "fixture-manual", task_type="manual", due_date=NOW)
    task(db, "fixture-repeat", priority=None, ev_score=None)
    task(db, "fixture-closed", status="done", outcome={"sold": True, "amount": 125.5, "note": "Замовлено"})
    task(db, "fixture-standalone", 0, task_type="manual")
    contact(db, due=NOW + timedelta(days=1), manager_id=3)
    contact(db, 2, due=None)
    output = os.environ.get("WORKDAY_FIXTURE_OUTPUT")
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        for name, result in [("queue", queue(client)), ("tasks-active", details(client).json()),
                             ("tasks-history", details(client, scope="history").json())]:
            Path(output, f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))


def test_search_current_book_name_after_rename(fixture):
    client, book, db = fixture
    task(db)
    book[1]["full_name"] = "Нова назва [Фірма]"
    page = queue(client, search="[фірма]")
    assert page["total"] == 1
    assert page["rows"][0]["client_name"] == "Нова назва [Фірма]"


def test_primary_urgency_persisted_score_priority_and_stable_key(fixture):
    client, _, db = fixture
    task(db, "normal-big-score", urgency="normal", ev_score=99999)
    task(db, "critical-null", urgency="critical", ev_score=None, priority=None)
    task(db, "critical-priority", urgency="critical", priority=40)
    task(db, "critical-score", urgency="critical", ev_score=50, priority=10)
    task(db, "critical-score-a", urgency="critical", ev_score=50, priority=10)
    row = queue(client)["rows"][0]
    assert row["primary_task"]["task_key"] == "critical-score"
    assert [item["task_key"] for item in details(client).json()["tasks"]] == [
        "critical-score", "critical-score-a", "critical-priority", "critical-null", "normal-big-score",
    ]


def test_manual_group_first_within_bucket_and_contact_search_retains_all_tasks(fixture):
    client, _, db = fixture
    task(db, "a", 1, due_date=NOW)
    task(db, "b", 2, task_type="manual", due_date=NOW + timedelta(hours=1))
    task(db, "c", 2, due_date=NOW)
    contact(db, 2, due=NOW, next_action="Literal [x].*")
    assert [row["client_id"] for row in queue(client)["rows"]] == [2, 1]
    page = queue(client, search="[X].*")
    assert page["counts"]["all"] == page["counts"]["today"] == 1
    assert page["rows"][0]["task_count"] == 2


def test_unknown_manager_and_invalid_detail_scope(fixture):
    client, _, _ = fixture
    assert client.get("/cockpit/workday", params={"manager_net_uid": str(uuid4())}).status_code == 404
    assert details(client, scope="generated").status_code == 422


def test_contact_proposal_next_kyiv_day_retry_and_transfer_journey(fixture, monkeypatch):
    client, book, db = fixture
    clock = datetime(2026, 10, 22, 6, tzinfo=UTC)  # 09:00 Kyiv
    monkeypatch.setattr(workday, "_now", lambda: clock)
    monkeypatch.setattr(seller_contacts, "_now", lambda: clock)

    def post(body, uid=UID):
        return client.post("/cockpit/contacts", params={"manager_net_uid": uid}, json=body)

    first = {"event_id": str(uuid4()), "client_id": 1, "result": "reached", "note": "Уточнили потребу",
             "next_contact_at": "2026-10-22T13:00:00+03:00", "next_action": "Підготувати пропозицію"}
    assert post(first).status_code == 200
    assert queue(client)["counts"]["today"] == 1
    clock += timedelta(hours=4)
    proposal = {"event_id": str(uuid4()), "client_id": 1, "result": "proposal",
                "note": "Надіслали пропозицію; очікуємо рішення",
                "next_contact_at": "2026-10-23T09:00:00+03:00", "next_action": "Уточнити рішення клієнта"}
    accepted = post(proposal)
    assert accepted.status_code == 200
    original_event = accepted.json()["event"]
    # Simulate a response lost after the durable insert: retry must return the original event.
    assert post(proposal).json()["event"] == original_event
    page = queue(client)
    assert page["counts"]["upcoming"] == page["total"] == 1
    assert page["rows"][0]["latest_contact"]["result"] == "proposal"
    assert page["rows"][0]["task_count"] == 0
    assert page["rows"][0]["due_at"] == "2026-10-23T06:00:00Z"
    clock = datetime(2026, 10, 22, 21, tzinfo=UTC)  # exactly next Kyiv midnight
    assert queue(client)["counts"]["today"] == 1
    clock = datetime(2026, 10, 23, 21, tzinfo=UTC)  # following midnight, deadline is overdue
    assert queue(client)["counts"]["overdue"] == 1
    assert post(proposal).json()["event"] == original_event  # old deadline cannot invalidate replay
    assert db.seller_contact_events.count_documents({}) == 2
    book[1]["manager_id"] = 3
    assert post(proposal).status_code == 404
    assert queue(client)["total"] == 0
    transferred = queue(client, uid=OTHER_UID)
    assert transferred["rows"][0]["latest_contact"]["manager_id"] == 1
    followup = {**proposal, "event_id": str(uuid4()), "result": "reached",
                "next_contact_at": "2026-10-26T09:00:00+02:00", "next_action": "Повторний контакт"}
    assert post(followup, OTHER_UID).status_code == 200
    history = client.get("/cockpit/contacts/history",
                         params={"manager_net_uid": OTHER_UID, "client_id": 1}).json()
    assert history["total"] == 3
    assert [event["manager_id"] for event in history["events"]] == [3, 1, 1]
    assert history["events"][1] == original_event
    assert queue(client, uid=OTHER_UID)["rows"][0]["latest_contact"]["manager_id"] == 3
    assert db.tasks.count_documents({}) == 0


def test_primary_task_is_independent_of_union_output_order(fixture, monkeypatch):
    client, _, db = fixture
    task(db, "z-ai", urgency="critical", ev_score=99999)
    task(db, "a-manual", task_type="manual", urgency="low")
    contact(db, due=NOW)
    collection = db.tasks
    aggregate = collection.aggregate

    def shuffled_union(pipeline, **kwargs):
        stages = list(pipeline)
        union_index = next(index for index, stage in enumerate(stages) if "$unionWith" in stage)
        # Force a legal but adversarial union order: contact first, then AI ahead of manual.
        stages.insert(union_index + 1, {"$sort": {"_kind": -1, "task_key": -1}})
        return aggregate(stages, **kwargs)

    monkeypatch.setattr(collection, "aggregate", shuffled_union)
    monkeypatch.setattr(mongo, "tasks", lambda: collection)
    row = queue(client)["rows"][0]
    assert row["primary_task"]["task_key"] == "a-manual"
    assert row["latest_contact"] is not None and row["task_count"] == 2


def test_missing_legacy_manual_client_id_normalized_without_read_write(fixture):
    client, _, db = fixture
    task(db, "legacy", task_type="manual", client_name="")
    db.tasks.update_one({"task_key": "legacy"}, {"$unset": {"client_id": ""}})
    row = queue(client)["rows"][0]
    assert row["key"] == "task:legacy" and row["client_id"] is None
    assert row["primary_task"]["client_id"] is None
    page = details(client, "task:legacy").json()
    assert page["tasks"][0]["client_id"] is None
    assert "client_id" not in db.tasks.find_one({"task_key": "legacy"})
