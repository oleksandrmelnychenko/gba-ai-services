"""Generator rules changed after the 2026-11-13 time-split backtest."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.data import signals_repository as sig
from app.services import repeat_sales, scoring
from app.services.generators import churn_winback, cross_sell, reorder_due


def _churn_row(client_id, silence, days, span):
    return {"client_id": client_id, "recent_orders": 0, "prior_orders": 6, "silence_days": silence,
            "purchase_days": days, "purchase_span_days": span}


@pytest.fixture
def churn_rows(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(sig, "query", lambda sql, params: [dict(row) for row in rows])
    return rows


def test_churn_requires_silence_beyond_the_floor_and_twice_the_own_interval(churn_rows):
    churn_rows.extend([
        _churn_row(1, 12, 20, 190),
        _churn_row(2, 29, 20, 190),
        _churn_row(3, 30, 20, 190),
        _churn_row(4, 100, 4, 180),
        _churn_row(5, 120, 4, 180),
        _churn_row(6, 45, 1, 0),
    ])
    kept = {row["client_id"]: row for row in sig.churn_candidates_for_manager(7, "2026-11-13")}
    assert set(kept) == {3, 5, 6}
    assert kept[3]["own_interval_days"] == 10.0
    assert kept[5]["own_interval_days"] == 60.0
    assert kept[6]["own_interval_days"] is None


def test_churn_gate_is_configurable_and_can_be_switched_off(churn_rows, monkeypatch):
    churn_rows.extend([_churn_row(1, 5, 20, 190), _churn_row(4, 100, 4, 180)])
    settings = sig.get_settings()
    monkeypatch.setattr(settings, "churn_min_silence_days", 0)
    monkeypatch.setattr(settings, "churn_silence_cycle_mult", 0.0)
    assert {row["client_id"] for row in sig.churn_candidates_for_manager(7, "2026-11-13")} == {1, 4}
    monkeypatch.setattr(settings, "churn_silence_cycle_mult", 3.0)
    assert sig.churn_candidates_for_manager(7, "2026-11-13") == []


def test_churn_sql_counts_purchase_days_inside_the_baseline_window(monkeypatch):
    seen = {}
    monkeypatch.setattr(sig, "query", lambda sql, params: seen.update(sql=sql, params=params) or [])
    sig.churn_candidates_for_manager(7, "2026-11-13")
    assert "COUNT(DISTINCT CONVERT(date, dt)) AS purchase_days" in seen["sql"]
    assert seen["params"]["start"] == "2025-11-13"


def test_churn_task_states_the_clients_own_interval(monkeypatch):
    monkeypatch.setattr(churn_winback.sig, "churn_candidates_for_manager", lambda mid, as_of: [
        {"client_id": 5, "recent_orders": 0, "prior_orders": 6, "silence_days": 120,
         "own_interval_days": 60.0}])
    monkeypatch.setattr(churn_winback.sig, "contacts_for_clients", lambda ids: {})
    monkeypatch.setattr(churn_winback.sig, "client_features", lambda ids, as_of: {})
    monkeypatch.setattr(scoring, "score_task_priority", lambda tt, feat: (50.0, 0.5, 10.0, 5.0))
    task = churn_winback.generate(7, "2026-11-13", "2026-11")[0]
    assert task.payload["churn"]["own_interval_days"] == 60.0
    assert "не замовляв 120 дн при звичному інтервалі ~60 дн" in task.explanation.factors


def test_client_book_reactivation_segment_uses_the_same_churn_rule(monkeypatch):
    monkeypatch.setattr(repeat_sales.sig, "repeat_products_for_manager", lambda *a, **kw: [
        {"client_id": cid, "client_net_uid": None, "name": str(cid), "full_name": None, "phone": None,
         "email": None, "last_order": None, "orders_cnt": 3, "retained_orders_cnt": 3,
         "turnover_eur": 0, "reorder_due_count": 0, "reorder_overdue_count": 0,
         "cooldown_cycle_start": None, "product_id": None, "prior_orders": 9, "recent_orders": 0}
        for cid in (1, 2)])
    asked = {}
    monkeypatch.setattr(repeat_sales.sig, "churn_candidates_for_manager",
                        lambda mid, as_of: asked.update(as_of=as_of) or [{"client_id": 2}])
    _, _, churn = repeat_sales.read_snapshot(7, "2026-11-13")
    assert churn == {2}
    assert asked["as_of"] == "2026-11-14"


def _due(product_id, n_orders, elapsed, cycle):
    return {"client_id": 1, "product_id": product_id, "n_orders": n_orders,
            "elapsed_days": elapsed, "cycle_days": cycle, "last_buy": None}


def test_reorder_task_leads_with_the_most_purchased_due_product(monkeypatch):
    rows = [_due(10, 3, 58, 20.0), _due(11, 9, 22, 20.0), _due(12, 9, 30, 20.0),
            _due(13, 5, 40, 20.0), _due(14, 4, 41, 20.0), _due(15, 3, 59, 20.0), _due(16, 3, 21, 20.0)]
    monkeypatch.setattr(reorder_due.sig, "reorder_candidates_for_manager", lambda *a, **kw: rows)
    monkeypatch.setattr(reorder_due.sig, "ubiquitous_product_ids", lambda pct, as_of: frozenset())
    monkeypatch.setattr(reorder_due.sig, "contacts_for_clients", lambda ids: {})
    monkeypatch.setattr(reorder_due.sig, "client_features", lambda ids, as_of: {})
    monkeypatch.setattr(reorder_due.sig, "product_names", lambda ids: {i: f"P{i}" for i in ids})
    scored = {}
    monkeypatch.setattr(scoring, "score_task_priority",
                        lambda tt, feat: scored.update(feat) or (50.0, 0.5, 10.0, 5.0))
    task = reorder_due.generate(7, "2026-11-13", "2026-11")[0]
    assert [p["product_id"] for p in task.payload["products"]] == [11, 12, 13, 14, 16]
    assert scored["n_orders"] == 9 and scored["elapsed_days"] == 22.0
    assert task.reason.startswith("«P11»")
    assert task.signals["products_due"] == 7


def test_cross_sell_reports_reco_unavailable_only_when_reco_gave_no_answer(monkeypatch):
    monkeypatch.setattr(cross_sell.reco_client, "is_healthy", lambda: False)
    assert cross_sell.generate(7, "2026-11-13", "2026-11") == []
    assert cross_sell.unavailable_reason(7) == scoring.RECO_UNAVAILABLE

    monkeypatch.setattr(cross_sell.reco_client, "is_healthy", lambda: True)
    monkeypatch.setattr(cross_sell.sig, "active_clients_for_manager",
                        lambda *a, **kw: [{"client_id": 1}, {"client_id": 2}])
    monkeypatch.setattr(cross_sell.sig, "client_features", lambda ids, as_of: {})
    failures = SimpleNamespace(n=0)
    monkeypatch.setattr(cross_sell.reco_client, "recommend_failures", lambda: failures.n)

    def failing(*args, **kwargs):
        failures.n += 1
        return []

    monkeypatch.setattr(cross_sell.reco_client, "recommend", failing)
    assert cross_sell.generate(7, "2026-11-13", "2026-11") == []
    assert cross_sell.unavailable_reason(7) == scoring.RECO_UNAVAILABLE

    monkeypatch.setattr(cross_sell.reco_client, "recommend", lambda *a, **kw: [])
    assert cross_sell.generate(7, "2026-11-13", "2026-11") == []
    assert cross_sell.unavailable_reason(7) is None
    assert cross_sell.unavailable_reason(99) is None
