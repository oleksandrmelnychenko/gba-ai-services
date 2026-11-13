"""Whole generator -> reconciler -> quota allocator -> Mongo acceptance on business fixtures.

Only source I/O and target lookup are replaced. Actual debt/reorder generators, identity,
manager transfer, lifecycle and quota code run together; no live source or workers are used.
"""

from copy import deepcopy
from datetime import UTC, datetime

import mongomock
import pytest

from app.core.config import get_settings
from app.data import mongo, reconciliation_repository
from app.data import signals_repository as sig
from app.domain.models import Task, TaskStatus, TaskType
from app.services import lifecycle, orchestrator, targets
from app.services.generators import debt_followup, reorder_due


def purchase(identifier, hour=8, day=1):
    value = datetime(2026, 10, day, hour, tzinfo=UTC)
    return {
        "order_id": identifier,
        "purchased_at": value.replace(tzinfo=None).isoformat(),
        "timestamp_basis": "native_utc",
        "purchased_at_utc": value.isoformat(),
    }


@pytest.fixture
def scenario(monkeypatch):
    database = mongomock.MongoClient(tz_aware=True).acceptance
    database.tasks.create_index("task_key", unique=True)
    monkeypatch.setattr(mongo, "tasks", lambda: database.tasks)
    monkeypatch.setattr(mongo, "task_events", lambda: database.events)
    monkeypatch.setattr(mongo, "manager_prefs", lambda: database.preferences)
    state = {
        "owner": 1,
        "debt_id": 501,
        "debt_balance": "50",
        "elapsed": 15,
        "orders": {"7": purchase(100)},
        "returns": {},
        "source_failed": False,
    }
    monkeypatch.setattr(lifecycle, "_now", lambda: datetime(2026, 10, 16, 9, tzinfo=UTC))

    def facts(ids, asof):
        if state["source_failed"]:
            raise TimeoutError("fixture source unavailable")
        return {
            10: {
                "manager_id": state["owner"],
                "debts": {
                    "501": state["debt_balance"] if state["debt_id"] == 501 else "0",
                    str(state["debt_id"]): state["debt_balance"],
                },
                "orders": deepcopy(state["orders"]),
                "returns": deepcopy(state["returns"]),
            }
        }

    monkeypatch.setattr(reconciliation_repository, "source_facts", facts)

    def debts(manager, asof, **kwargs):
        if manager != state["owner"] or float(state["debt_balance"]) <= 0:
            return []
        return [
            {
                "client_id": 10,
                "debt_ids": str(state["debt_id"]),
                "max_days_past_terms": 3,
                "max_overdue_days": 33,
                "overdue_amount": float(state["debt_balance"]),
                "debt_lines": 1,
            }
        ]

    monkeypatch.setattr(sig, "debt_task_candidates", debts)
    monkeypatch.setattr(
        sig,
        "reorder_candidates_for_manager",
        lambda manager, asof, **kwargs: (
            [
                {
                    "client_id": 10,
                    "product_id": 7,
                    "n_orders": 4,
                    "cycle_days": 7,
                    "elapsed_days": state["elapsed"],
                }
            ]
            if manager == state["owner"]
            else []
        ),
    )
    monkeypatch.setattr(sig, "ubiquitous_product_ids", lambda *args: [])
    monkeypatch.setattr(sig, "contacts_for_clients", lambda ids: {10: {"full_name": "Fixture buyer"}})
    monkeypatch.setattr(
        sig, "client_features", lambda *args: {10: {"monetary": 100, "recency_days": 15, "order_count": 4}}
    )
    monkeypatch.setattr(sig, "product_names", lambda ids: {7: "Fixture product"})
    monkeypatch.setattr(targets, "compute_target", lambda *args: {})
    monkeypatch.setattr(orchestrator, "_GENERATORS", [debt_followup, reorder_due])
    settings = get_settings().model_copy(
        update={
            "max_active_tasks_per_manager": 2,
            "max_tasks_per_client_per_day": 2,
            "crit_debt_reserve": 0,
            "max_pace_boost": 1.0,
        }
    )
    monkeypatch.setattr(orchestrator, "get_settings", lambda: settings)
    return database, state


def generate(manager=1, day="2026-10-16"):
    return orchestrator.generate_for_manager(manager, day)


def test_payment_purchase_return_second_cycle_transfer_preserve_manual_backlog(scenario):
    database, state = scenario
    # Manual head commitments exceed BOTH global and per-client AI caps.
    for i in range(6):
        lifecycle.create_manual(
            Task(
                task_key=f"manual-{i}",
                manager_id=1,
                client_id=10,
                task_type=TaskType.MANUAL,
                title="Call back",
                reason="Agreed",
            ),
            created_by=99,
        )
    first = generate()
    assert first["persisted"] == 2 and first["skipped_capped"] == 0
    active = {row["task_type"]: row for row in database.tasks.find({"task_type": {"$ne": "manual"}})}
    debt_key, order_key = active["debt_followup"]["task_key"], active["reorder_due"]["task_key"]
    lifecycle.add_note(debt_key, 1, "Promise to call Friday")
    lifecycle.change_status(
        debt_key, TaskStatus.SNOOZED, by=1, snooze_until=datetime(2026, 10, 23, 10, tzinfo=UTC)
    )
    lifecycle.change_status(order_key, TaskStatus.IN_PROGRESS, by=1)
    assert lifecycle.active_workload_counts(1) == {"ai_active": 2, "manual_backlog": 6, "active_total": 8}

    # Explicit payment and a verified purchase supersede both stale generators' advice.
    state["debt_balance"] = "0"
    state["orders"] = {"7": purchase(101, hour=10, day=16)}
    paid_and_ordered = generate()
    assert paid_and_ordered["superseded"] == 2
    assert lifecycle.active_workload_counts(1) == {"ai_active": 0, "manual_backlog": 8, "active_total": 8}
    assert database.tasks.find_one({"task_key": debt_key})["source_evidence"]["debts"] == {"501": "0"}
    copied = database.tasks.find_one({"source_task_key": debt_key})
    assert copied["notes"][0]["text"] == "Promise to call Friday"
    assert copied["snooze_until"].day == 23
    public_purchase = database.tasks.find_one({"task_key": order_key})["source_evidence"]["orders"]["7"]
    assert public_purchase == {
        "order_id": 101,
        "purchased_on": "2026-10-16",
        "date_basis": "source_document_date",
    }

    # Fully returning the purchase renews the need; positive return identity creates
    # another cycle that day without resurrecting the historical recommendation.
    state["orders"] = {"7": purchase(100)}
    state["returns"] = {"7": {"order_id": 101, "quantity": "10", "returned_at": "2026-10-16T11:00:00"}}
    state["debt_id"], state["debt_balance"] = 502, "80"
    second = generate()
    assert second["persisted"] == 2
    current = list(database.tasks.find({"status": "open", "task_type": {"$ne": "manual"}}))
    assert len(current) == 2 and all(row["task_key"] not in (debt_key, order_key) for row in current)
    assert database.tasks.count_documents({"status": "superseded"}) == 2
    second_keys = {row["task_key"] for row in current}
    assert generate()["persisted"] == 0

    # New native purchase on the SAME day closes the second reorder cycle because
    # both purchase instants have verified clocks. Manual next contacts survive.
    state["orders"] = {"7": purchase(102, hour=12, day=16)}
    same_day = generate()
    assert same_day["superseded"] == 1
    assert database.tasks.count_documents({"status": "superseded"}) == 3

    state["owner"] = 2
    transferred = generate(2)
    assert transferred["reassigned"] == 3  # copied source commitments plus remaining AI debt task
    # Explicit head-assigned tasks remain with their assignee when the client book moves.
    assert lifecycle.active_workload_counts(1) == {"ai_active": 0, "manual_backlog": 6, "active_total": 6}
    assert lifecycle.active_workload_counts(2) == {"ai_active": 1, "manual_backlog": 2, "active_total": 3}
    assert database.tasks.count_documents({"manager_id": 1, "assignment_mode": "head_task_only"}) == 6
    assert (
        database.tasks.find_one({"task_key": copied["task_key"]})["notes"][0]["text"]
        == "Promise to call Friday"
    )
    assert database.tasks.find_one({"task_key": copied["task_key"]})["assignment_history"][0]["from"] == 1
    assert database.tasks.find_one({"task_key": debt_key})["status"] == "superseded"
    assert second_keys.issubset({row["task_key"] for row in database.tasks.find()})


def test_partial_source_generator_failure_and_month_rollover_keep_active_identity(scenario, monkeypatch):
    database, state = scenario
    assert generate()["persisted"] == 2
    keys = {row["task_key"] for row in database.tasks.find()}
    state["source_failed"] = True

    def failed_generator(*args):
        raise TimeoutError("fixture generator unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr(debt_followup, "generate", failed_generator)
        failed = generate(day="2026-11-01")
    assert failed["generators_failed"] == 1 and failed["reconciliation_failed"] == 1
    assert failed["superseded"] == 0
    assert {row["task_key"] for row in database.tasks.find()} == keys
    assert lifecycle.active_workload_counts(1)["ai_active"] == 2
    state["source_failed"] = False
    assert generate(day="2026-11-01")["persisted"] == 0
    assert {row["task_key"] for row in database.tasks.find()} == keys
