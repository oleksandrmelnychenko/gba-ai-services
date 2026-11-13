"""The payload states when a score is a rule, a stale model or a run without reco."""
from __future__ import annotations

import json
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.ml import calibration, score_task
from app.services import scoring

UID = "10101010-1010-1010-1010-101010101010"


@pytest.fixture
def model(monkeypatch):
    def configure(rows: dict, *, age: float | None = 2.0, heads: dict | None = None, broken=None):
        monkeypatch.setattr(score_task, "_metrics", lambda: {
            "cv_per_type": {name: {"n": n} for name, n in rows.items()}})
        monkeypatch.setattr(score_task, "_meta", lambda: {"heads": heads} if heads else {})
        monkeypatch.setattr(score_task, "model_age_days", lambda now=None: age)

        def load():
            if broken:
                raise score_task.IncompatibleModelArtifactError("x")
            return object()

        monkeypatch.setattr(score_task, "_model", load)
        monkeypatch.setattr(score_task, "model_compatibility", lambda: {
            "model_compatible": not broken, "model_reasons": [broken] if broken else []})
    return configure


ROWS = {"debt_followup": 21, "cross_sell": 0, "reorder_due": 19506, "churn_winback": 941}


def test_basis_names_the_reason_for_every_heuristic_score(model):
    model(ROWS)
    assert scoring.score_basis("reorder_due") == {"source": "model", "reason": None}
    assert scoring.score_basis("debt_followup") == {"source": "heuristic", "reason": "model_head_untrained"}
    model(ROWS, heads={"churn_winback": {"rows": 941, "published": False}})
    assert scoring.score_basis("churn_winback") == {"source": "heuristic", "reason": "model_head_withheld"}
    model(ROWS, broken="model_artifact_missing")
    assert scoring.score_basis("reorder_due") == {"source": "heuristic", "reason": "model_artifact_missing"}
    model(ROWS, broken="model_source_history_mismatch")
    assert scoring.score_basis("reorder_due") == {"source": "heuristic", "reason": "model_incompatible"}
    model({}, age=None, broken="model_artifact_missing")
    assert scoring.score_basis("debt_followup") == {"source": "heuristic", "reason": "model_artifact_missing"}


def test_stale_artifact_keeps_the_model_score_and_is_marked(model, monkeypatch):
    model(ROWS, age=22.0)
    assert scoring.score_basis("reorder_due") == {"source": "model", "reason": "model_stale"}
    state = scoring.degradation(["reco_unavailable", "reco_unavailable"])
    assert state["degraded"] is True
    assert state["degraded_reasons"] == [
        "heuristic_score:debt_followup:model_head_untrained", "model_stale",
        "heuristic_score:cross_sell:model_head_untrained", "reco_unavailable"]
    monkeypatch.setattr(scoring.get_settings(), "model_max_age_days", 30)
    assert scoring.score_basis("reorder_due")["reason"] is None


def test_all_heads_trained_and_fresh_is_not_degraded(model):
    model({name: 5000 for name in ROWS})
    assert scoring.degradation() == {"degraded": False, "degraded_reasons": [], "score_sources": {
        name: {"source": "model", "reason": None} for name in scoring.SCORED_TYPES}}


def test_generation_reports_reco_unavailable_and_failed_generators(model, monkeypatch):
    from app.data import mongo as m
    from app.domain.models import TaskType
    from app.services import orchestrator, reconciliation, worker

    model({name: 5000 for name in ROWS})
    db = mongomock.MongoClient()["t"]
    for name in ("tasks", "task_events", "manager_prefs", "generation_runs", "seller_contact_events"):
        monkeypatch.setattr(m, name, lambda name=name: db[name])
    monkeypatch.setattr(m, "get_db", lambda: db)

    def broken(manager_id, as_of, window):
        raise RuntimeError("sql down")

    monkeypatch.setattr(orchestrator, "_GENERATORS", [
        SimpleNamespace(__name__="cross", TYPE=TaskType.CROSS_SELL, generate=lambda *a: [],
                        unavailable_reason=lambda manager_id: "reco_unavailable"),
        SimpleNamespace(__name__="debt", TYPE=TaskType.DEBT_FOLLOWUP, generate=broken),
        SimpleNamespace(__name__="churn", TYPE=TaskType.CHURN_WINBACK, generate=lambda *a: []),
    ])
    monkeypatch.setattr(reconciliation, "prepare", lambda mid, candidates, as_of: (candidates, {}))
    monkeypatch.setattr(orchestrator, "_apply_pace_boost", lambda candidates, pace: None)
    stats = orchestrator.generate_for_manager(1, "2026-11-12")
    assert stats["degraded"] is True
    assert stats["degraded_reasons"] == ["reco_unavailable", "generator_failed:debt_followup"]
    assert "score_sources" not in stats

    monkeypatch.setattr(worker.sig, "all_managers", lambda: [1, 2])
    monkeypatch.setattr(worker.sig, "head_user_ids", lambda: [])
    run = worker.run(as_of="2026-11-12", push_feedback=False)
    assert run["degraded_reasons"] == ["reco_unavailable", "generator_failed:debt_followup"]
    assert m.last_run_degraded_reasons() == ["reco_unavailable", "generator_failed:debt_followup"]


def test_inbox_and_health_carry_the_marker(model, monkeypatch):
    from app.api import main
    from app.data import mongo as m

    model(ROWS)
    db = mongomock.MongoClient()["t"]
    for name in ("tasks", "task_events", "manager_prefs", "generation_runs"):
        monkeypatch.setattr(m, name, lambda name=name: db[name])
    monkeypatch.setattr(m, "get_db", lambda: db)
    monkeypatch.setattr(m, "ensure_indexes", lambda: None)
    monkeypatch.setattr(main.signals_repository, "manager_id_for_netuid", lambda uid: 7)
    db["generation_runs"].insert_one({"full_run": True, "completed_at": 1, "degraded_reasons": [
        "heuristic_score:debt_followup:model_head_untrained", "reco_unavailable"]})
    db["tasks"].insert_one({"task_key": "k", "manager_id": 7, "client_id": 1, "status": "open",
                            "task_type": "debt_followup", "urgency": "high", "priority": 50.0,
                            "payload": {"score_source": "heuristic", "degraded": True,
                                        "degraded_reason": "model_head_untrained"}})
    with TestClient(main.app) as client:
        body = client.get("/cockpit/inbox", params={"manager_net_uid": UID}).json()
    assert body["degraded"] is True
    assert body["degraded_reasons"] == [
        "heuristic_score:debt_followup:model_head_untrained",
        "heuristic_score:cross_sell:model_head_untrained", "reco_unavailable"]
    assert body["score_sources"]["reorder_due"] == {"source": "model", "reason": None}
    assert body["tasks"][0]["payload"]["degraded_reason"] == "model_head_untrained"

    monkeypatch.setattr(m, "last_run_degraded_reasons", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert main._scoring_degradation()["degraded_reasons"][-1].startswith("heuristic_score:cross_sell")


def test_calibration_is_identity_without_a_file_and_bound_to_the_model(tmp_path, monkeypatch):
    monkeypatch.setattr(calibration, "ART", tmp_path)
    calibration._load.cache_clear()
    assert calibration.apply("reorder_due", 0.3) == 0.3
    assert calibration.state()["calibration_reason"] == "calibration_absent"

    (tmp_path / calibration.MODEL_FILE).write_bytes(b"model-a")
    probabilities = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6] * 100
    outcomes = ([0] * 9 + [1]) * 60
    document = calibration.fit_document({"reorder_due": (probabilities, outcomes)},
                                        calibration.model_sha256())
    (tmp_path / calibration.FILE).write_text(json.dumps(document))
    calibration._load.cache_clear()
    assert calibration.state()["calibration_active"] is True
    assert calibration.apply("churn_winback", 0.3) == 0.3
    calibrated = [calibration.apply("reorder_due", value) for value in (0.1, 0.3, 0.6)]
    assert calibrated == sorted(calibrated) and calibrated[1] < 0.3
    assert abs(document["types"]["reorder_due"]["mean_calibrated"] - 0.1) < 0.01

    (tmp_path / calibration.MODEL_FILE).write_bytes(b"model-b")
    calibration._load.cache_clear()
    assert calibration.apply("reorder_due", 0.3) == 0.3
    assert calibration.state()["calibration_reason"] == "calibration_model_mismatch"
    calibration._load.cache_clear()


def test_calibration_needs_enough_rows_and_both_outcomes():
    with pytest.raises(ValueError):
        calibration.fit_platt([0.2] * 10, [0, 1] * 5)
    with pytest.raises(ValueError):
        calibration.fit_platt([0.2] * 400, [0] * 400)


def test_scored_task_reports_raw_and_served_probability(monkeypatch):
    class Model:
        def predict_proba(self, frame):
            import numpy as np
            return np.array([[0.6, 0.4]] * len(frame))

    monkeypatch.setattr(score_task, "_model", lambda: Model())
    monkeypatch.setattr(calibration, "apply", lambda task_type, p: p / 2)
    scored = score_task.score_task({"task_type": "reorder_due", "monetary": 100, "order_count": 4})
    assert scored["p_raw"] == 0.4 and scored["p_outcome"] == 0.2
    assert scored["ev_score"] == 5.0 and scored["priority"] == 20.0
    assert score_task.score_batch([{"task_type": "reorder_due"}])[0]["p_outcome"] == 0.2
