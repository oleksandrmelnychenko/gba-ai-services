"""Isolated generator unit tests — each generator's row->Task mapping, with the signal repository
(and reco client) monkeypatched, so a wrong mapping/task_key/urgency is caught without a live DB."""
from __future__ import annotations

import pytest

from app.services.generators import (
    churn_winback,
    cross_sell,
    debt_followup,
    new_client_activation,
    reorder_due,
)

AS_OF, WIN = "2026-06-08", "2026-06"


def test_debt_followup_builds_task(monkeypatch):
    monkeypatch.setattr(debt_followup.sig, "overdue_debts_for_manager",
                        lambda mid, as_of, max_age_days, min_amount: [
                            {"client_id": 10, "overdue_amount": 5000, "max_overdue_days": 40,
                             "max_days_past_terms": 33, "debt_lines": 2}])
    monkeypatch.setattr(debt_followup.sig, "contacts_for_clients",
                        lambda ids: {10: {"full_name": "Acme", "phone": "+380", "email": "a@b.c"}})
    monkeypatch.setattr(debt_followup.sig, "client_features",
                        lambda ids, as_of: {10: {"monetary": 20000.0, "recency_days": 5, "order_count": 30}})

    tasks = debt_followup.generate(1, AS_OF, WIN)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.task_key == "mgr:1|client:10|type:debt_followup|win:2026-06"
    assert t.task_type.value == "debt_followup" and t.manager_id == 1 and t.client_id == 10
    assert t.contact.phone == "+380" and t.priority > 0
    assert t.urgency.value in {"critical", "high", "normal", "low"}
    # model-scored fields are stamped and consistent: priority == 100*p_outcome, ev_score ~= p*ev
    # (ev_score is computed from the unrounded probability inside score_task, hence approx).
    assert t.priority == round(100.0 * t.p_outcome, 2)
    assert t.ev_score == pytest.approx(t.p_outcome * t.expected_value, abs=0.5)
    assert t.expected_value == 5000.0          # debt E[value] = overdue amount (cash at stake)


def test_reorder_due_bundles_per_client(monkeypatch):
    rows = [
        {"client_id": 10, "product_id": 100, "n_orders": 5, "cycle_days": 30.0, "elapsed_days": 60},
        {"client_id": 10, "product_id": 101, "n_orders": 4, "cycle_days": 20.0, "elapsed_days": 50},
    ]
    monkeypatch.setattr(reorder_due.sig, "reorder_candidates_for_manager",
                        lambda mid, as_of, min_cycle_days, max_overdue_mult: rows)
    monkeypatch.setattr(reorder_due.sig, "ubiquitous_product_ids", lambda pct: frozenset())
    monkeypatch.setattr(reorder_due.sig, "contacts_for_clients",
                        lambda ids: {10: {"name": "Acme", "phone": "+1"}})
    monkeypatch.setattr(reorder_due.sig, "client_features",
                        lambda ids, as_of: {10: {"monetary": 9000.0, "recency_days": 4, "order_count": 12}})
    monkeypatch.setattr(reorder_due.sig, "product_names", lambda ids: {100: "Bolt", 101: "Nut"})

    tasks = reorder_due.generate(1, AS_OF, WIN)
    assert len(tasks) == 1                                   # two products -> ONE bundled client task
    t = tasks[0]
    assert t.task_key == "mgr:1|client:10|type:reorder_due|win:2026-06"
    assert len(t.payload["products"]) == 2
    assert t.priority == round(100.0 * t.p_outcome, 2) and t.p_outcome > 0


def test_churn_winback_builds_task(monkeypatch):
    monkeypatch.setattr(churn_winback.sig, "churn_candidates_for_manager",
                        lambda mid, as_of: [
                            {"client_id": 10, "recent_orders": 0, "prior_orders": 6, "silence_days": 120}])
    monkeypatch.setattr(churn_winback.sig, "contacts_for_clients", lambda ids: {10: {"name": "Acme"}})
    monkeypatch.setattr(churn_winback.sig, "client_features",
                        lambda ids, as_of: {10: {"monetary": 12000.0, "recency_days": 120, "order_count": 6}})

    tasks = churn_winback.generate(1, AS_OF, WIN)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.task_key == "mgr:1|client:10|type:churn_winback|win:2026-06"
    assert t.priority == round(100.0 * t.p_outcome, 2)
    assert t.expected_value == 12000.0         # churn E[value] = client annual turnover at risk


def test_new_client_activation_is_retired_and_generates_nothing(monkeypatch):
    # RETIRED (nba-v3-propensity): no model head + fires on 1C-sync stamp dates -> junk tasks.
    # The generator is hard-guarded (ENABLED=False) so even a direct call yields no tasks, and it is
    # unwired from the orchestrator registry (see test_orchestrator_diversity).
    assert new_client_activation.ENABLED is False
    # would have produced a task pre-retirement; now returns [] without even touching the DB.
    called = {"n": 0}
    monkeypatch.setattr(new_client_activation.sig, "new_clients_for_manager",
                        lambda mid, as_of: (called.__setitem__("n", called["n"] + 1) or [
                            {"client_id": 10, "full_name": "Acme", "name": "Acme",
                             "phone": "+1", "email": "a@b.c", "days_since_created": 13, "n_orders": 0}]))
    assert new_client_activation.generate(1, AS_OF, WIN) == []
    assert called["n"] == 0  # guarded off before any signal query runs


def test_cross_sell_uses_reco_discovery(monkeypatch):
    monkeypatch.setattr(cross_sell.reco_client, "is_healthy", lambda: True)
    monkeypatch.setattr(cross_sell.reco_client, "recommend",
                        lambda cid, top_n, as_of_date, path, timeout: [
                            {"product_id": 200, "score": 0.4, "source": "discovery"},
                            {"product_id": 201, "score": 0.02, "source": "discovery"},   # below _MIN_SCORE
                            {"product_id": 202, "score": 0.9, "source": "repurchase"}])   # not discovery
    monkeypatch.setattr(cross_sell.sig, "active_clients_for_manager",
                        lambda mid, as_of, recent_days, min_orders: [
                            {"client_id": 10, "full_name": "Acme", "phone": "+1"}])
    monkeypatch.setattr(cross_sell.sig, "client_features",
                        lambda ids, as_of: {10: {"monetary": 9000.0, "recency_days": 3, "order_count": 18}})
    monkeypatch.setattr(cross_sell.sig, "product_names", lambda ids: {200: "Filter"})

    tasks = cross_sell.generate(1, AS_OF, WIN)
    assert len(tasks) == 1
    t = tasks[0]
    assert t.task_key == "mgr:1|client:10|type:cross_sell|win:2026-06"
    assert [p["product_id"] for p in t.payload["products"]] == [200]   # only the qualifying discovery item
    assert t.priority == round(100.0 * t.p_outcome, 2)
    assert t.ev_score == pytest.approx(t.p_outcome * t.expected_value, abs=0.5)


def test_cross_sell_empty_when_reco_offline(monkeypatch):
    monkeypatch.setattr(cross_sell.reco_client, "is_healthy", lambda: False)
    assert cross_sell.generate(1, AS_OF, WIN) == []
