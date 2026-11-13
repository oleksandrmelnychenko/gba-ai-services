"""Explicit attribution mechanics; SQL facts substituted, Mongo can be disposable real DB."""

import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app.api import main
from app.data import mongo
from app.services import sale_attribution as service

UID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
HEAD = "33333333-3333-3333-3333-333333333333"
SALE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CONTACT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def fact():
    return {
        "sale_id": 1,
        "sale_net_uid": SALE,
        "client_id": 7,
        "number": "INV-1",
        "document_date": "2026-10-22",
        "shipment_date": "2026-10-22",
        "order_id": 8,
        "offer_net_uid": None,
        "document_currency": "UAH",
        "is_invoice": True,
        "canceled": False,
        "merged": False,
        "retained_lines": 2,
        "date_basis": "native_invoice_kyiv",
        "payments": [
            {
                "allocation_id": 1,
                "payment_id": 10,
                "document_date": "2026-10-22",
                "source_currency": "UAH",
                "currency": "EUR",
                "amount": 25.1234,
                "canceled": False,
            }
        ],
        "returns": [],
    }


@pytest.fixture
def env(monkeypatch):
    uri = os.environ.get("TEST_ATTRIBUTION_MONGO_URI")
    conn = (
        MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000)
        if uri
        else mongomock.MongoClient(tz_aware=True)
    )
    name = "attribution_fixture_" + uuid4().hex
    db = conn[name]
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    owner = {7: 1}
    facts = {SALE: fact()}
    for key in ["task-a", "task-b"]:
        db.tasks.insert_one({"task_key": key, "manager_id": 1, "client_id": 7, "status": "open"})
    db.seller_contact_events.insert_one(
        {"event_id": CONTACT, "client_id": 7, "manager_id": 1, "at": datetime.now(UTC)}
    )
    monkeypatch.setattr(
        main.signals_repository, "manager_id_for_netuid", lambda uid: {UID: 1, OTHER: 2, HEAD: 3}.get(uid)
    )
    monkeypatch.setattr(
        main.signals_repository, "manager_names", lambda ids: {1: "Seller", 2: "New owner", 3: "Head"}
    )
    monkeypatch.setattr(
        main.signals_repository,
        "contact_book_for_manager",
        lambda mid, cid=None: [
            {"client_id": k} for k, v in owner.items() if v == mid and (cid is None or cid == k)
        ],
    )
    monkeypatch.setattr(main.signals_repository, "is_head_of_sales", lambda uid: uid == HEAD)
    monkeypatch.setattr(
        service.source,
        "facts",
        lambda ids: {key: deepcopy(value) for key, value in facts.items() if key in ids},
    )
    monkeypatch.setattr(
        service.source,
        "candidates",
        lambda cid, off, limit: ([deepcopy(facts[SALE])] if cid == 7 and off == 0 else [], 1),
    )
    monkeypatch.setattr(main.signals_repository, "task_manager_ids", lambda ids: [1, 2])
    yield TestClient(main.app), db, facts, owner
    conn.drop_database(name)
    conn.close()


def post(client, task="task-a", uid=UID, **extra):
    return client.post(
        "/cockpit/attribution/links",
        params={"manager_net_uid": uid},
        json={"task_key": task, "sale_net_uid": SALE, **extra},
    )


def links(client, uid=UID, head=False, **params):
    return client.get(
        "/cockpit/" + ("head/" if head else "") + "attribution/links",
        params={"manager_net_uid": uid, **params},
    )


def test_explicit_link_contact_offer_and_replay_do_not_create_financial_entries(env):
    client, db, facts, _ = env
    facts[SALE]["offer_net_uid"] = str(uuid4())
    first = post(client, contact_event_id=CONTACT)
    assert first.status_code == 200, first.text
    repeated = post(client, contact_event_id=CONTACT)
    assert repeated.json() == first.json()
    row = first.json()["link"]
    assert row["manager_id"] == 1 and row["contact_manager_id"] == 1
    assert row["offer_net_uid_at_link"] == facts[SALE]["offer_net_uid"]
    assert db.explicit_sale_attributions.count_documents({}) == 1
    assert db.tasks.find_one({"task_key": "task-a"})["status"] == "open"
    assert links(client).json()["rows"][0]["evidence"]["allocated_payment_eur"] == 25.12


@pytest.mark.parametrize("field", ["sale_net_uid", "contact_event_id"])
def test_empty_identity_is_rejected(env, field):
    client, db, _, _ = env
    assert post(client, **{field: str(UUID(int=0))}).status_code == 422
    assert db.explicit_sale_attributions.count_documents({}) == 0


def test_one_sale_cannot_be_fully_credited_to_two_concurrent_tasks(env):
    client, db, _, _ = env
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda key: post(client, key), ["task-a", "task-b"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    assert db.explicit_sale_attributions.count_documents({}) == 1


@pytest.mark.parametrize(
    "field,value",
    [("client_id", 8), ("canceled", True), ("merged", True), ("is_invoice", False), ("retained_lines", 0)],
)
def test_unrelated_cancelled_merged_draft_returned_documents_cannot_be_linked(env, field, value):
    client, db, facts, _ = env
    facts[SALE][field] = value
    assert post(client).status_code == 422
    assert db.explicit_sale_attributions.count_documents({}) == 0


def test_unknown_or_other_client_contact_is_rejected(env):
    client, db, _, _ = env
    assert post(client, contact_event_id=str(uuid4())).status_code == 422
    db.seller_contact_events.update_one({"event_id": CONTACT}, {"$set": {"client_id": 99}})
    assert post(client, contact_event_id=CONTACT).status_code == 422


def test_partial_payments_cancellation_and_return_after_original_month_remain_separate(env):
    client, _, facts, _ = env
    assert post(client).status_code == 200
    facts[SALE]["payments"] += [
        dict(facts[SALE]["payments"][0], allocation_id=2, payment_id=11, amount=10.1256)
    ]
    facts[SALE]["returns"] = [
        {
            "return_id": 22,
            "return_item_id": 23,
            "document_date": "2026-11-02",
            "quantity": 1,
            "credit_processed": True,
            "recorded_return_credit_eur": 4.20,
            "canceled": False,
        }
    ]
    row = links(client).json()["rows"][0]
    assert row["source_at_link"]["returns"] == []
    assert row["evidence"]["allocated_payment_eur"] == 35.25
    assert row["evidence"]["recorded_return_credit_eur"] == 4.20
    facts[SALE]["payments"][0]["canceled"] = True
    assert links(client).json()["rows"][0]["evidence"]["allocated_payment_eur"] == 10.13
    facts[SALE]["returns"][0]["recorded_return_credit_eur"] = None
    evidence = links(client).json()["rows"][0]["evidence"]
    assert evidence["recorded_return_credit_eur"] is None and not evidence["return_credit_coverage_complete"]


@pytest.mark.parametrize("field,state", [("merged", "merged_requires_review"), ("canceled", "canceled")])
def test_later_merge_cancel_preserves_link_but_removes_confirmed_financial_credit(env, field, state):
    client, db, facts, _ = env
    assert post(client).status_code == 200
    facts[SALE][field] = True
    evidence = links(client).json()["rows"][0]["evidence"]
    assert evidence["state"] == state and evidence["allocated_payment_eur"] is None
    assert db.explicit_sale_attributions.count_documents({}) == 1


def test_missing_source_never_becomes_zero_and_manager_transfer_preserves_historical_owner(env):
    client, db, facts, owner = env
    assert post(client).status_code == 200
    owner[7] = 2
    db.tasks.update_many({}, {"$set": {"manager_id": 2}})
    assert post(client).status_code == 200
    assert links(client).json()["rows"][0]["manager_id"] == 1
    assert links(client, OTHER).json()["total"] == 0
    assert post(client, uid=OTHER).status_code == 409
    assert links(client, HEAD, True).json()["rows"][0]["manager_id"] == 1
    facts.clear()
    evidence = links(client).json()["rows"][0]["evidence"]
    assert evidence["state"] == "source_unavailable" and evidence["allocated_payment_eur"] is None


def test_owner_and_head_scope_and_paging(env):
    client, _, _, _ = env
    assert post(client, uid=OTHER).status_code == 404
    assert (
        client.get(
            "/cockpit/attribution/candidates", params={"manager_net_uid": OTHER, "task_key": "task-a"}
        ).status_code
        == 404
    )
    assert post(client).status_code == 200
    assert links(client, head=True).json()["is_head"] is False
    assert links(client, head=True).json()["rows"] == []
    beyond = links(client, offset=20).json()
    assert beyond["total"] == 1 and beyond["rows"] == []
    assert links(client, limit=101).status_code == 422


def test_same_client_multiple_offers_do_not_create_implicit_attribution(env):
    client, db, facts, _ = env
    candidate = client.get(
        "/cockpit/attribution/candidates", params={"manager_net_uid": UID, "task_key": "task-a"}
    ).json()
    assert candidate["total"] == 1 and db.explicit_sale_attributions.count_documents({}) == 0
    assert post(client).status_code == 200
    assert post(client, task="task-b").status_code == 409



def test_assignment_during_source_read_rejects_old_owner_credit(env, monkeypatch):
    from app.services import task_delegation
    client, db, _, _ = env
    stamp = datetime.now(UTC).replace(microsecond=0)
    db.tasks.update_one({"task_key": "task-a"}, {"$set": {"updated_at": stamp}})
    monkeypatch.setattr(main.signals_repository, "all_managers", lambda: [1, 2])
    original = service.source.facts
    def facts_then_assign(ids):
        result = original(ids)
        task_delegation.assign(3, task_delegation.AssignmentRequest(task_key="task-a",
            expected_manager_id=1, expected_updated_at=stamp, manager_id=2,
            clarification="Continue with new manager"))
        return result
    monkeypatch.setattr(service.source, "facts", facts_then_assign)
    response = post(client)
    assert response.status_code == 409, response.text
    assert db.explicit_sale_attributions.count_documents({}) == 0
    assert db.tasks.find_one({"task_key": "task-a"})["manager_id"] == 2


def test_link_linearized_before_assignment_keeps_original_credit_and_retry(env, monkeypatch):
    from app.services import task_delegation
    client, db, _, _ = env
    monkeypatch.setattr(main.signals_repository, "all_managers", lambda: [1, 2])
    collection = db.explicit_sale_attributions
    monkeypatch.setattr(service, "collection", lambda: collection)
    original = collection.insert_one
    def assign_before_insert(row):
        accepted = db.tasks.find_one({"task_key": "task-a"})
        task_delegation.assign(3, task_delegation.AssignmentRequest(task_key="task-a",
            expected_manager_id=1, expected_updated_at=accepted["updated_at"], manager_id=2,
            clarification="Continue after accepted link"))
        return original(row)
    monkeypatch.setattr(collection, "insert_one", assign_before_insert)
    response = post(client)
    assert response.status_code == 200, response.text
    link = response.json()["link"]
    assert link["manager_id"] == 1
    assert db.tasks.find_one({"task_key": "task-a"})["manager_id"] == 2
    assert post(client).json() == response.json()
    assert db.explicit_sale_attributions.count_documents({}) == 1
