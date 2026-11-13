"""A task type the model has no basis to rank is scored by the deterministic heuristic."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.ml import score_task
from app.services import scoring

_spec = importlib.util.spec_from_file_location(
    "retrain_for_score_source", Path(__file__).resolve().parents[1] / "scripts" / "retrain.py")
retrain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retrain)

DEBT = {"days_past_terms": 40, "overdue_amount": 8000, "max_overdue_days": 40, "debt_lines": 3,
        "monetary": 50000, "recency_days": 5, "order_count": 120}


@pytest.fixture
def heads(monkeypatch):
    def set_heads(rows: dict, meta_heads: dict | None = None):
        monkeypatch.setattr(score_task, "_metrics", lambda: {
            "cv_per_type": {name: {"n": n} for name, n in rows.items()}})
        monkeypatch.setattr(score_task, "_meta", lambda: {"heads": meta_heads} if meta_heads else {})
        monkeypatch.setattr(score_task, "_model", lambda: object())
        monkeypatch.setattr(score_task, "model_age_days", lambda now=None: 1.0)
    return set_heads


def test_head_with_too_few_training_rows_uses_the_heuristic(heads, monkeypatch):
    heads({"debt_followup": 21, "cross_sell": 0, "reorder_due": 19506, "churn_winback": 941})
    assert scoring.score_source("debt_followup") == "heuristic"
    assert scoring.score_source("cross_sell") == "heuristic"
    assert scoring.score_source("reorder_due") == "model"
    assert scoring.score_source("churn_winback") == "model"
    assert scoring.score_source("unknown_type") == "heuristic"

    def no_model(feats):
        raise AssertionError("model must not score a head without training basis")

    monkeypatch.setattr(score_task, "score_task", no_model)
    prio, p_out, ev, ev_score = scoring.score_task_priority("debt_followup", DEBT)
    expected = scoring._heuristic_probability("debt_followup", DEBT)
    assert p_out == round(expected, 6)
    assert prio == round(100 * expected, 2)
    assert ev == 8000.0
    assert ev_score == round(expected * 8000.0, 4)
    assert scoring.score_task_priority("debt_followup", DEBT) == (prio, p_out, ev, ev_score)


def test_threshold_is_configurable(heads, monkeypatch):
    heads({"debt_followup": 21})
    monkeypatch.setattr(get_settings(), "min_model_training_rows", 20)
    assert scoring.score_source("debt_followup") == "model"
    monkeypatch.setattr(get_settings(), "min_model_training_rows", 22)
    assert scoring.score_source("debt_followup") == "heuristic"


def test_head_withheld_by_the_retrain_gate_uses_the_heuristic(heads):
    heads({"churn_winback": 941}, {"churn_winback": {"rows": 941, "published": False}})
    assert scoring.score_source("churn_winback") == "heuristic"
    heads({"churn_winback": 0}, {"churn_winback": {"rows": 941, "published": True}})
    assert scoring.score_source("churn_winback") == "model"


def test_incompatible_artifact_uses_the_heuristic(heads, monkeypatch):
    heads({"reorder_due": 19506})

    def incompatible():
        raise score_task.IncompatibleModelArtifactError("x")

    monkeypatch.setattr(score_task, "_model", incompatible)
    assert scoring.score_source("reorder_due") == "heuristic"


def test_generation_marks_score_source_in_the_payload(heads, monkeypatch):
    from types import SimpleNamespace

    import mongomock

    from app.data import mongo as m
    from app.domain.models import Task, TaskType
    from app.services import orchestrator, reconciliation
    db = mongomock.MongoClient()["t"]
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    monkeypatch.setattr(m, "task_events", lambda: db["task_events"])
    monkeypatch.setattr(m, "manager_prefs", lambda: db["manager_prefs"])
    heads({"debt_followup": 21, "reorder_due": 19506})

    def generate(manager_id, as_of, window):
        return [Task(task_key=f"k-{tt.value}", manager_id=1, client_id=cid, task_type=tt,
                     title="t", reason="r", priority=50.0, ev_score=1.0, payload={"x": 1})
                for cid, tt in ((1, TaskType.DEBT_FOLLOWUP), (2, TaskType.REORDER_DUE))]

    monkeypatch.setattr(orchestrator, "_GENERATORS", [SimpleNamespace(__name__="g", generate=generate)])
    monkeypatch.setattr(reconciliation, "prepare", lambda mid, candidates, as_of: (candidates, {}))
    monkeypatch.setattr(orchestrator, "_apply_pace_boost", lambda candidates, pace: None)
    orchestrator.generate_for_manager(1, "2026-11-12")
    payloads = {doc["task_type"]: doc["payload"] for doc in db["tasks"].find({})}
    assert payloads["debt_followup"] == {"x": 1, "score_source": "heuristic", "degraded": True,
                                         "degraded_reason": "model_head_untrained"}
    assert payloads["reorder_due"] == {"x": 1, "score_source": "model", "degraded": False,
                                       "degraded_reason": None}


def test_expected_value_uses_one_horizon_for_every_type(monkeypatch):
    monkeypatch.setattr(get_settings(), "ev_horizon_days", 60)
    churn = score_task.expected_value({"task_type": "churn_winback", "monetary": 36500.0})
    assert churn == pytest.approx(6000.0)
    assert score_task.expected_value(
        {"task_type": "reorder_due", "monetary": 36500.0, "order_count": 10}) == 3650.0
    assert score_task.expected_value(
        {"task_type": "debt_followup", "sig_overdue_amount": 800.0, "monetary": 36500.0}) == 800.0
    from app.services import lifecycle
    assert lifecycle._derived_expected_value(
        {"task_type": "churn_winback", "signals": {"monetary": 36500.0}}) == pytest.approx(6000.0)


def test_retrain_gate_withholds_heads_with_too_few_rows_or_low_auc(tmp_path):
    metrics = tmp_path / "metrics.json"
    meta = tmp_path / "model_meta.json"
    metrics.write_text(json.dumps({
        "production_model": "logit",
        "cv_per_type": {"reorder_due": {"n": 19506}, "debt_followup": {"n": 21},
                        "churn_winback": {"n": 941}, "cross_sell": {"n": 0}},
        "oot_per_type": {"logit": {"reorder_due": {"auc": 0.68}, "debt_followup": {"auc": 1.0},
                                   "churn_winback": {"auc": 0.55},
                                   "cross_sell": {"auc": float("nan")}}},
    }))
    meta.write_text(json.dumps({"production_model": "logit"}))
    heads = retrain._publish_heads(metrics, meta, min_rows=500, auc_floor=0.62)
    assert {name: head["published"] for name, head in heads.items()} == {
        "reorder_due": True, "debt_followup": False, "churn_winback": False, "cross_sell": False}
    assert heads["debt_followup"]["reasons"] == ["rows 21 < 500"]
    assert heads["churn_winback"]["reasons"] == ["OOT AUC 0.5500 < floor 0.62"]
    assert heads["cross_sell"]["reasons"] == ["rows 0 < 500", "OOT AUC unavailable"]
    written = json.loads(meta.read_text())
    assert written["heads"] == heads
    assert written["head_gate"] == {"min_rows": 500, "auc_floor": 0.62}
    assert written["production_model"] == "logit"


def test_retrain_prunes_backups_to_the_newest_four(tmp_path, monkeypatch):
    monkeypatch.setattr(retrain, "ART", tmp_path)
    names = [f"backup_202611{day:02d}_040000" for day in range(1, 8)]
    for name in names:
        (tmp_path / name).mkdir()
        (tmp_path / name / "model_meta.json").write_text("{}")
    (tmp_path / "model_meta.json").write_text("{}")
    removed = retrain._prune_backups()
    assert [path.name for path in removed] == names[:3]
    assert sorted(path.name for path in tmp_path.glob("backup_*")) == names[3:]
    assert (tmp_path / "model_meta.json").exists()
    assert retrain._prune_backups() == []
