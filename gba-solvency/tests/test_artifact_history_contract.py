from __future__ import annotations

import json

import pytest

from app.risk import score_current, score_forward
from app.risk.lineage import FORWARD_ARTIFACT_ROLE, LINEAGE_SCHEMA_VERSION


def _write(path, value) -> None:
    path.write_text(json.dumps(value))


def test_current_model_rejects_a_date_stamped_legacy_artifact(monkeypatch, tmp_path):
    artifact = tmp_path / "current.json"
    _write(artifact, {"source_history_start": "2025-01-01"})
    monkeypatch.setattr(score_current, "_ART", artifact)
    score_current._card.cache_clear()

    with pytest.raises(RuntimeError, match="missing training_lineage"):
        score_current._card()

    score_current._card.cache_clear()


def test_current_model_rejects_nested_wrong_history_floor(monkeypatch, tmp_path):
    source = json.loads(score_current._ART.read_text())
    source["training_lineage"]["source_history_start"] = "2024-01-01"
    artifact = tmp_path / "current.json"
    _write(artifact, source)
    monkeypatch.setattr(score_current, "_ART", artifact)
    score_current._card.cache_clear()

    with pytest.raises(RuntimeError, match="source-history contract mismatch"):
        score_current._card()

    score_current._card.cache_clear()


def test_current_model_rejects_payload_tampering(monkeypatch, tmp_path):
    source = json.loads(score_current._ART.read_text())
    source["logistic"]["intercept"] += 1
    artifact = tmp_path / "current.json"
    _write(artifact, source)
    monkeypatch.setattr(score_current, "_ART", artifact)
    score_current._card.cache_clear()

    with pytest.raises(RuntimeError, match="payload hash mismatch"):
        score_current._card()

    score_current._card.cache_clear()


def test_forward_model_fails_closed_from_supported_status_evidence(
    monkeypatch, tmp_path
):
    status = tmp_path / "forward-status.json"
    _write(status, json.loads(score_forward._STATUS.read_text()))
    monkeypatch.setattr(score_forward, "_STATUS", status)
    score_forward._load_status.cache_clear()
    score_forward._load.cache_clear()

    with pytest.raises(
        score_forward.ForwardModelUnavailableError,
        match="3 < 30 unique positive clients",
    ):
        score_forward.score_forward({"total_debt_eur": 100.0})

    score_forward._load.cache_clear()
    score_forward._load_status.cache_clear()


def test_forward_available_status_cannot_activate_stamped_legacy(
    monkeypatch, tmp_path
):
    artifact = tmp_path / "forward.json"
    status = tmp_path / "forward-status.json"
    _write(artifact, {"source_history_start": "2025-01-01"})
    _write(
        status,
        {
            "lineage_schema_version": LINEAGE_SCHEMA_VERSION,
            "artifact_role": FORWARD_ARTIFACT_ROLE,
            "status": "available",
            "reason": None,
            "source_history_start": "2025-01-01",
            "training_run_id": "fake",
            "model_payload_sha256": "a" * 64,
            "dataset_sha256": "b" * 64,
            "evaluated_at": "2026-07-25T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(score_forward, "_ART", artifact)
    monkeypatch.setattr(score_forward, "_STATUS", status)
    score_forward._load_status.cache_clear()
    score_forward._load.cache_clear()

    with pytest.raises(
        score_forward.ForwardModelUnavailableError,
        match="missing training_lineage",
    ):
        score_forward._load()

    score_forward._load.cache_clear()
    score_forward._load_status.cache_clear()
