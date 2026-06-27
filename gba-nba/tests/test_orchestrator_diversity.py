"""Orchestrator diversity-quota tests — the inbox stays a balanced mix under over-subscription."""
from __future__ import annotations

from types import SimpleNamespace

import mongomock
import pytest

from app.domain.models import Outcome, Task, TaskStatus, TaskType, Urgency


@pytest.fixture
def mongo_db(monkeypatch):
    mc = mongomock.MongoClient()
    db = mc["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "get_client", lambda: mc)
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: db["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: db["manager_prefs"])
    return db


def _mk(task_type: TaskType, i: int, prio: float, ev_score: float = 0.0) -> Task:
    return Task(
        task_key=f"mgr:1|client:{task_type.value}:{i}|type:{task_type.value}|win:2026-06",
        manager_id=1, client_id=hash((task_type.value, i)) % 1_000_000,
        task_type=task_type, title="t", reason="r", priority=prio, ev_score=ev_score,
        urgency=Urgency.HIGH,
    )


def _patch_generators(monkeypatch, counts: dict[TaskType, int]):
    from app.services.generators import (
        churn_winback,
        cross_sell,
        debt_followup,
        new_client_activation,
        reorder_due,
    )
    mapping = {
        debt_followup: TaskType.DEBT_FOLLOWUP,
        reorder_due: TaskType.REORDER_DUE,
        churn_winback: TaskType.CHURN_WINBACK,
        new_client_activation: TaskType.NEW_CLIENT_ACTIVATION,
        cross_sell: TaskType.CROSS_SELL,
    }
    for mod, tt in mapping.items():
        n = counts.get(tt, 0)
        monkeypatch.setattr(mod, "generate",
                            lambda mid, as_of, win, _tt=tt, _n=n: [_mk(_tt, i, 50.0 + i) for i in range(_n)])


def test_quota_keeps_a_balanced_mix(mongo_db, monkeypatch):
    # every type over-produces; without a quota debt would fill all 50 slots
    _patch_generators(monkeypatch, {tt: 30 for tt in TaskType})
    from app.services import orchestrator
    stats = orchestrator.generate_for_manager(1, "2026-06-07")
    bt = stats["by_type"]
    assert stats["persisted"] == 50                       # cap respected
    assert len(bt) == 4                                   # 4 live types (new_client unwired) -> diversity
    assert max(bt.values()) <= 21                         # no single type dominates the inbox
    assert bt["debt_followup"] >= bt["cross_sell"]        # debt is the heaviest-weighted quota


def test_new_client_activation_is_unwired_from_generation(mongo_db, monkeypatch):
    # even if new_client_activation.generate is patched to over-produce, the orchestrator must never
    # run it (it is dropped from _GENERATORS and _TYPE_SHARE) -> zero new_client tasks ever persisted.
    _patch_generators(monkeypatch, {tt: 30 for tt in TaskType})
    from app.services import orchestrator
    assert TaskType.NEW_CLIENT_ACTIVATION not in {g for g in orchestrator._TYPE_SHARE}
    stats = orchestrator.generate_for_manager(1, "2026-06-07")
    assert "new_client_activation" not in stats["by_type"]


def test_caps_keep_higher_ev_over_higher_probability_same_type(mongo_db, monkeypatch):
    from app.services import orchestrator
    from app.services.generators import churn_winback, cross_sell, debt_followup, reorder_due

    low_probability_high_ev = _mk(TaskType.DEBT_FOLLOWUP, 1, prio=25.0, ev_score=900.0)
    high_probability_low_ev = _mk(TaskType.DEBT_FOLLOWUP, 2, prio=90.0, ev_score=100.0)
    monkeypatch.setattr(debt_followup, "generate",
                        lambda manager_id, as_of, window: [high_probability_low_ev, low_probability_high_ev])
    for mod in (reorder_due, churn_winback, cross_sell):
        monkeypatch.setattr(mod, "generate", lambda manager_id, as_of, window: [])

    monkeypatch.setattr(orchestrator, "get_settings", lambda: SimpleNamespace(
        max_active_tasks_per_manager=1,
        max_tasks_per_client_per_day=2,
        crit_debt_reserve=0,
        feedback_window_days=90,
        max_pace_boost=1.0,
        feedback_penalty_floor=0.5,
        feedback_penalty_per_rejection=0.15,
    ))
    monkeypatch.setattr(orchestrator.lifecycle, "feedback_rejections", lambda manager_id, days: {})

    stats = orchestrator.generate_for_manager(1, "2026-06-07")
    assert stats["persisted"] == 1
    assert [d["task_key"] for d in mongo_db["tasks"].find({}, {"_id": 0, "task_key": 1})] == [
        low_probability_high_ev.task_key
    ]


def test_inbox_orders_by_urgency_then_ev_before_type_tiebreaker(mongo_db):
    from app.services import lifecycle
    lifecycle.upsert_generated(_band_task("reorder_crit", TaskType.REORDER_DUE, Urgency.CRITICAL,
                                          99.0, ev_score=900.0))
    lifecycle.upsert_generated(_band_task("debt_crit", TaskType.DEBT_FOLLOWUP, Urgency.CRITICAL,
                                          80.0, ev_score=100.0))
    lifecycle.upsert_generated(_band_task("reorder_high", TaskType.REORDER_DUE, Urgency.HIGH,
                                          95.0, ev_score=9999.0))
    order = [d["task_key"] for d in lifecycle.inbox(1)]
    # within the critical band, EV wins across task types; the high-band reorder still comes after
    # both criticals because urgency band wins over raw EV.
    assert order == ["reorder_crit", "debt_crit", "reorder_high"]


def _band_task(key: str, tt: TaskType, urg: Urgency, prio: float, ev_score: float = 0.0) -> Task:
    return Task(task_key=key, manager_id=1, client_id=hash(key) % 1_000_000,
                task_type=tt, title="t", reason="r", priority=prio, ev_score=ev_score, urgency=urg)


def test_sweep_expired_purges_only_stale_active(mongo_db):
    from datetime import UTC, datetime, timedelta

    from app.data import mongo
    from app.services import lifecycle
    past = datetime.now(UTC) - timedelta(days=1)

    lifecycle.upsert_generated(_band_task("stale_active", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0))
    mongo.tasks().update_one({"task_key": "stale_active"}, {"$set": {"expires_at": past}})
    lifecycle.upsert_generated(_band_task("fresh_active", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0))
    lifecycle.upsert_generated(_band_task("stale_done", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0))
    lifecycle.change_status("stale_done", TaskStatus.DONE, by=1)
    mongo.tasks().update_one({"task_key": "stale_done"}, {"$set": {"expires_at": past}})

    assert lifecycle.sweep_expired() == 1                 # only the stale ACTIVE one
    assert lifecycle.get_task("stale_active") is None
    assert lifecycle.get_task("fresh_active") is not None  # not yet expired
    assert lifecycle.get_task("stale_done") is not None    # DONE kept for KPI/audit


def test_feedback_penalty_sinks_rejected_pairs():
    from app.services import orchestrator
    a = _band_task("fa", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 100.0)
    b = _band_task("fb", TaskType.REORDER_DUE, Urgency.HIGH, 100.0)
    c = _band_task("fc", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 100.0)
    orchestrator._apply_feedback_penalty([a, b, c], {
        (a.client_id, "debt_followup"): 2,     # ×(1-0.30)=0.70
        (c.client_id, "debt_followup"): 5,     # ×max(0.5, 1-0.75)=0.50 (floor)
    })
    assert a.priority == 70.0
    assert b.priority == 100.0                 # no rejections -> untouched
    assert c.priority == 50.0                  # floored


def test_feedback_rejections_counts_dismiss_and_done_not_sold(mongo_db):
    from app.services import lifecycle
    lifecycle.upsert_generated(_band_task("rej1", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0))
    lifecycle.change_status("rej1", TaskStatus.DISMISSED, by=1)
    lifecycle.upsert_generated(_band_task("rej2", TaskType.REORDER_DUE, Urgency.HIGH, 50.0))
    lifecycle.change_status("rej2", TaskStatus.DONE, by=1, outcome=Outcome(sold=False))
    lifecycle.upsert_generated(_band_task("won", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0))
    lifecycle.change_status("won", TaskStatus.DONE, by=1, outcome=Outcome(sold=True, amount=100.0))

    rej = lifecycle.feedback_rejections(1)
    assert sum(rej.values()) == 2                                  # dismissed + done-not-sold
    assert rej.get((_band_task("rej1", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0).client_id,
                    "debt_followup")) == 1                         # the won (done-sold) is NOT counted


def test_pace_boost_lifts_revenue_when_shipped_behind():
    from app.services import orchestrator
    tasks = [_band_task("r", TaskType.REORDER_DUE, Urgency.HIGH, 50.0),
             _band_task("d", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0),
             _band_task("c", TaskType.CHURN_WINBACK, Urgency.HIGH, 50.0)]
    # shipped 100% behind -> max boost 1.25; paid on pace -> no debt boost
    orchestrator._apply_pace_boost(tasks, {
        "shipped": {"expected_to_date": 100.0, "gap": 100.0},
        "paid": {"expected_to_date": 100.0, "gap": 0.0},
    })
    by = {t.task_type: t.priority for t in tasks}
    assert by[TaskType.REORDER_DUE] == 62.5      # 50 * 1.25
    assert by[TaskType.DEBT_FOLLOWUP] == 50.0    # paid on pace
    assert by[TaskType.CHURN_WINBACK] == 50.0    # never boosted


def test_pace_boost_lifts_debt_when_paid_behind():
    from app.services import orchestrator
    tasks = [_band_task("r", TaskType.REORDER_DUE, Urgency.HIGH, 50.0),
             _band_task("d", TaskType.DEBT_FOLLOWUP, Urgency.HIGH, 50.0)]
    # paid 50% behind -> boost 1.125; shipped on pace -> no revenue boost
    orchestrator._apply_pace_boost(tasks, {
        "shipped": {"expected_to_date": 100.0, "gap": 0.0},
        "paid": {"expected_to_date": 100.0, "gap": 50.0},
    })
    by = {t.task_type: t.priority for t in tasks}
    assert by[TaskType.DEBT_FOLLOWUP] == 56.25   # 50 * 1.125
    assert by[TaskType.REORDER_DUE] == 50.0


def test_leftover_capacity_redistributes_when_other_types_empty(mongo_db, monkeypatch):
    # only debt has candidates (e.g. reco offline, no churn/reorder) -> pass 2 fills the rest with debt
    _patch_generators(monkeypatch, {TaskType.DEBT_FOLLOWUP: 80})
    from app.services import orchestrator
    stats = orchestrator.generate_for_manager(1, "2026-06-07")
    assert stats["persisted"] == 50                       # cap still filled, not stuck at the 20 quota
    assert stats["by_type"]["debt_followup"] == 50


def test_critical_debt_reserve_is_total_active_allowance_not_per_run(mongo_db, monkeypatch):
    from app.services import orchestrator
    from app.services.generators import churn_winback, cross_sell, debt_followup, reorder_due

    def debt_candidates(manager_id, as_of, window):
        return [
            Task(
                task_key=f"mgr:{manager_id}|client:{i}|type:debt_followup|win:{window}",
                manager_id=manager_id,
                client_id=i,
                task_type=TaskType.DEBT_FOLLOWUP,
                title="t",
                reason="r",
                priority=90.0,
                ev_score=1000.0 - i,
                urgency=Urgency.CRITICAL,
            )
            for i in range(10)
        ]

    monkeypatch.setattr(debt_followup, "generate", debt_candidates)
    for mod in (reorder_due, churn_winback, cross_sell):
        monkeypatch.setattr(mod, "generate", lambda manager_id, as_of, window: [])

    monkeypatch.setattr(orchestrator, "get_settings", lambda: SimpleNamespace(
        max_active_tasks_per_manager=3,
        max_tasks_per_client_per_day=2,
        crit_debt_reserve=2,
        feedback_window_days=90,
        max_pace_boost=1.0,
        feedback_penalty_floor=0.5,
        feedback_penalty_per_rejection=0.15,
    ))
    monkeypatch.setattr(orchestrator.lifecycle, "feedback_rejections", lambda manager_id, days: {})

    first = orchestrator.generate_for_manager(1, "2026-06-07")
    second = orchestrator.generate_for_manager(1, "2026-07-07")

    assert first["persisted"] == 5
    assert first["crit_debt_reserved"] == 2
    assert second["persisted"] == 0
    assert second["crit_debt_reserved"] == 0
    assert mongo_db["tasks"].count_documents({"manager_id": 1, "status": "open"}) == 5
