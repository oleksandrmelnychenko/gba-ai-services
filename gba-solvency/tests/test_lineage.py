from __future__ import annotations

import json

import pandas as pd
import pytest

from app.core.history import coverage
from app.risk import score_current
from app.risk.lineage import (
    CURRENT_ARTIFACT_ROLE,
    CURRENT_DATASET_ROLE,
    LineageError,
    dataset_manifest_path,
    validate_dataset_manifest,
    validate_production_artifact,
    write_dataset_manifest,
)


def _current_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "client_id": [1, 2],
            "label_sev180": [0, 1],
            "feature_date": ["2026-04-26", "2026-04-26"],
            "label_date": ["2026-07-25", "2026-07-25"],
            "signal": [1.0, 2.0],
        }
    )


def test_dataset_manifest_binds_dates_counts_and_parquet_hash(tmp_path):
    path = tmp_path / "training.parquet"
    frame = _current_frame()
    frame.to_parquet(path, index=False)
    written = write_dataset_manifest(
        path,
        frame,
        dataset_role=CURRENT_DATASET_ROLE,
        target_column="label_sev180",
        feature_dates=["2026-04-26"],
        label_dates=["2026-07-25"],
        window_months=12,
        builder="test",
    )

    validated = validate_dataset_manifest(
        path,
        frame,
        dataset_role=CURRENT_DATASET_ROLE,
        target_column="label_sev180",
    )

    assert validated["parquet_sha256"] == written["parquet_sha256"]
    assert validated["date_coverage"][0]["history_complete"] is True


def test_dataset_manifest_rejects_parquet_changed_after_build(tmp_path):
    path = tmp_path / "training.parquet"
    frame = _current_frame()
    frame.to_parquet(path, index=False)
    write_dataset_manifest(
        path,
        frame,
        dataset_role=CURRENT_DATASET_ROLE,
        target_column="label_sev180",
        feature_dates=["2026-04-26"],
        label_dates=["2026-07-25"],
        window_months=12,
        builder="test",
    )
    frame.loc[0, "signal"] = 999.0
    frame.to_parquet(path, index=False)

    with pytest.raises(LineageError, match="parquet hash mismatch"):
        validate_dataset_manifest(
            path,
            frame,
            dataset_role=CURRENT_DATASET_ROLE,
            target_column="label_sev180",
        )


def test_dataset_manifest_rejects_detached_date_claim(tmp_path):
    path = tmp_path / "training.parquet"
    frame = _current_frame()
    frame.to_parquet(path, index=False)
    write_dataset_manifest(
        path,
        frame,
        dataset_role=CURRENT_DATASET_ROLE,
        target_column="label_sev180",
        feature_dates=["2026-04-26"],
        label_dates=["2026-07-25"],
        window_months=12,
        builder="test",
    )
    manifest_path = dataset_manifest_path(path)
    manifest = json.loads(manifest_path.read_text())
    manifest["date_coverage"][0]["feature_date"] = "2026-04-25"
    history = coverage("2026-04-25", 12)
    manifest["date_coverage"][0].update(
        {
            "requested_start": history.requested_start.isoformat(),
            "effective_start": history.effective_start.isoformat(),
            "history_complete": history.history_complete,
        }
    )
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(LineageError, match="date columns do not match lineage"):
        validate_dataset_manifest(
            path,
            frame,
            dataset_role=CURRENT_DATASET_ROLE,
            target_column="label_sev180",
        )


def test_current_artifact_rejects_incomplete_12_month_training_window():
    artifact = json.loads(score_current._ART.read_text())
    history = coverage("2025-06-01", 12)
    artifact["training_lineage"]["dataset"]["date_coverage"] = [
        {
            "feature_date": "2025-06-01",
            "label_date": "2025-09-01",
            "requested_start": history.requested_start.isoformat(),
            "effective_start": history.effective_start.isoformat(),
            "history_complete": history.history_complete,
        }
    ]

    with pytest.raises(LineageError, match="complete feature window"):
        validate_production_artifact(
            artifact,
            artifact_role=CURRENT_ARTIFACT_ROLE,
        )
