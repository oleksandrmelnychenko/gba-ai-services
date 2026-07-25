"""Training-data lineage and production artifact validation.

The source-history date is not proof by itself.  A production model is accepted only when its
artifact contains a lineage envelope produced from a persisted, hash-verified dataset manifest
and a passed model-specific validation gate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import get_settings
from app.core.history import coverage

LINEAGE_SCHEMA_VERSION = 1
TRANSACTIONAL_HISTORY_POLICY = "clamped_to_source_history_start"
CURRENT_STATE_EXCEPTIONS = [
    "open_debt_balances_as_of_feature_date",
    "current_credit_terms_as_of_feature_date",
]

CURRENT_DATASET_ROLE = "current_state_training"
FORWARD_DATASET_ROLE = "forward_6m_training"
CURRENT_ARTIFACT_ROLE = "current_state_scorecard"
FORWARD_ARTIFACT_ROLE = "forward_6m_scorecard"

CURRENT_MIN_ROWS = 1_000
CURRENT_MIN_POSITIVES = 20
CURRENT_MIN_OOF_AUC = 0.90
CURRENT_SEV180_MIN_EUR = 100.0
CURRENT_SEV180_PD_FLOOR = 0.16
FORWARD_MIN_UNIQUE_POSITIVE_CLIENTS = 30
FORWARD_MIN_OOT_AUC = 0.75


class LineageError(RuntimeError):
    """A dataset or model artifact cannot prove the configured history contract."""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def dataset_manifest_path(dataset_path: str | Path) -> Path:
    return Path(dataset_path).with_suffix(".lineage.json")


def write_json_atomic(path: str | Path, value: Any) -> None:
    destination = Path(path)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=float,
        )
        + "\n"
    )
    temporary.replace(destination)


def _date_pairs(feature_dates: list[str], label_dates: list[str]) -> list[dict[str, Any]]:
    if not feature_dates or len(feature_dates) != len(label_dates):
        raise LineageError("feature/label date lineage must contain matching non-empty lists")
    pairs: list[dict[str, Any]] = []
    for feature_date, label_date in zip(feature_dates, label_dates, strict=True):
        history = coverage(feature_date, 12)
        if label_date < feature_date:
            raise LineageError(
                f"label date {label_date} precedes feature date {feature_date}"
            )
        pairs.append(
            {
                "feature_date": feature_date,
                "label_date": label_date,
                "requested_start": history.requested_start.isoformat(),
                "effective_start": history.effective_start.isoformat(),
                "history_complete": history.history_complete,
            }
        )
    return pairs


def write_dataset_manifest(
    dataset_path: str | Path,
    frame: pd.DataFrame,
    *,
    dataset_role: str,
    target_column: str,
    feature_dates: list[str],
    label_dates: list[str],
    window_months: int,
    builder: str,
) -> dict[str, Any]:
    """Persist a manifest after parquet creation, binding dates and counts to its SHA-256."""
    path = Path(dataset_path)
    if window_months != 12:
        raise LineageError("production solvency lineage currently requires a 12-month window")
    pairs = _date_pairs(feature_dates, label_dates)
    manifest = {
        "lineage_schema_version": LINEAGE_SCHEMA_VERSION,
        "dataset_role": dataset_role,
        "builder": builder,
        "built_at": utc_now(),
        "source_history_start": get_settings().source_history_start_date.isoformat(),
        "transactional_history_policy": TRANSACTIONAL_HISTORY_POLICY,
        "current_state_exceptions": CURRENT_STATE_EXCEPTIONS,
        "window_months": window_months,
        "date_coverage": pairs,
        "rows": int(len(frame)),
        "positives": int(frame[target_column].astype(int).sum()),
        "unique_clients": int(frame["client_id"].nunique()),
        "unique_positive_clients": int(
            frame.loc[frame[target_column].astype(int) == 1, "client_id"].nunique()
        ),
        "target_column": target_column,
        "parquet_sha256": sha256_file(path),
    }
    write_json_atomic(dataset_manifest_path(path), manifest)
    return manifest


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LineageError(message)


def validate_dataset_manifest(
    dataset_path: str | Path,
    frame: pd.DataFrame,
    *,
    dataset_role: str,
    target_column: str,
) -> dict[str, Any]:
    """Validate a manifest against config, parquet bytes, bound dates and dataframe counts."""
    path = Path(dataset_path)
    manifest_path = dataset_manifest_path(path)
    _require(manifest_path.exists(), f"missing dataset lineage manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LineageError(f"unreadable dataset lineage manifest: {manifest_path}") from exc

    expected_floor = get_settings().source_history_start_date.isoformat()
    _require(
        manifest.get("lineage_schema_version") == LINEAGE_SCHEMA_VERSION,
        "unsupported dataset lineage schema",
    )
    _require(manifest.get("dataset_role") == dataset_role, "dataset role mismatch")
    _require(
        manifest.get("source_history_start") == expected_floor,
        "dataset source-history contract mismatch",
    )
    _require(
        manifest.get("transactional_history_policy") == TRANSACTIONAL_HISTORY_POLICY,
        "dataset transactional-history policy mismatch",
    )
    _require(
        manifest.get("current_state_exceptions") == CURRENT_STATE_EXCEPTIONS,
        "dataset current-state exceptions mismatch",
    )
    _require(manifest.get("window_months") == 12, "dataset window-months mismatch")
    _require(manifest.get("target_column") == target_column, "dataset target mismatch")
    _require(
        manifest.get("parquet_sha256") == sha256_file(path),
        "dataset parquet hash mismatch",
    )
    _require(manifest.get("rows") == len(frame), "dataset row-count mismatch")
    _require(
        manifest.get("positives") == int(frame[target_column].astype(int).sum()),
        "dataset positive-count mismatch",
    )
    _require(
        manifest.get("unique_clients") == int(frame["client_id"].nunique()),
        "dataset unique-client count mismatch",
    )
    _require(
        manifest.get("unique_positive_clients")
        == int(frame.loc[frame[target_column].astype(int) == 1, "client_id"].nunique()),
        "dataset unique-positive-client count mismatch",
    )

    date_coverage = manifest.get("date_coverage")
    _require(isinstance(date_coverage, list) and date_coverage, "missing dataset date coverage")
    for item in date_coverage:
        feature_date = item.get("feature_date")
        label_date = item.get("label_date")
        _require(isinstance(feature_date, str), "invalid feature date lineage")
        _require(isinstance(label_date, str), "invalid label date lineage")
        history = coverage(feature_date, 12)
        _require(label_date >= feature_date, "label date precedes feature date")
        _require(
            item.get("requested_start") == history.requested_start.isoformat(),
            "dataset requested history start mismatch",
        )
        _require(
            item.get("effective_start") == history.effective_start.isoformat(),
            "dataset effective history start mismatch",
        )
        _require(
            item.get("history_complete") is history.history_complete,
            "dataset history-completeness mismatch",
        )

    feature_date_column = (
        "vintage" if dataset_role == FORWARD_DATASET_ROLE else "feature_date"
    )
    _require(
        feature_date_column in frame.columns and "label_date" in frame.columns,
        "dataset is missing bound feature/label date columns",
    )
    frame_pairs = sorted(
        {
            (str(feature_date), str(label_date))
            for feature_date, label_date in zip(
                frame[feature_date_column], frame["label_date"], strict=True
            )
        }
    )
    manifest_pairs = sorted(
        (item["feature_date"], item["label_date"]) for item in date_coverage
    )
    _require(frame_pairs == manifest_pairs, "dataset date columns do not match lineage")
    return manifest


def model_payload_sha256(artifact: dict[str, Any]) -> str:
    payload = {key: value for key, value in artifact.items() if key != "training_lineage"}
    return canonical_sha256(payload)


def attach_training_lineage(
    artifact: dict[str, Any],
    *,
    artifact_role: str,
    dataset_manifest: dict[str, Any],
    validation: dict[str, Any],
    training_run_id: str,
    trained_at: str,
) -> dict[str, Any]:
    result = dict(artifact)
    result["training_lineage"] = {
        "lineage_schema_version": LINEAGE_SCHEMA_VERSION,
        "artifact_role": artifact_role,
        "training_run_id": training_run_id,
        "trained_at": trained_at,
        "source_history_start": get_settings().source_history_start_date.isoformat(),
        "training_code": (
            "scripts/train_current_state.py"
            if artifact_role == CURRENT_ARTIFACT_ROLE
            else "scripts/train_forward_risk.py"
        ),
        "dataset": dataset_manifest,
        "validation": validation,
        "model_payload_sha256": model_payload_sha256(result),
    }
    return result


def validate_production_artifact(
    artifact: dict[str, Any],
    *,
    artifact_role: str,
) -> dict[str, Any]:
    """Reject stamped legacy artifacts and any artifact whose gate/payload is inconsistent."""
    lineage = artifact.get("training_lineage")
    _require(isinstance(lineage, dict), "missing training_lineage (legacy/stamped artifact)")
    expected_floor = get_settings().source_history_start_date.isoformat()
    _require(
        lineage.get("lineage_schema_version") == LINEAGE_SCHEMA_VERSION,
        "unsupported model lineage schema",
    )
    _require(lineage.get("artifact_role") == artifact_role, "model artifact role mismatch")
    _require(
        lineage.get("source_history_start") == expected_floor,
        "model artifact source-history contract mismatch",
    )
    _require(
        artifact.get("source_history_start") == expected_floor,
        "model payload source-history contract mismatch",
    )
    _require(
        isinstance(lineage.get("training_run_id"), str)
        and bool(lineage["training_run_id"]),
        "missing model training run id",
    )
    _require(
        artifact.get("trained_at") == lineage.get("trained_at"),
        "model trained-at lineage mismatch",
    )
    _require(
        lineage.get("model_payload_sha256") == model_payload_sha256(artifact),
        "model artifact payload hash mismatch",
    )

    dataset = lineage.get("dataset")
    _require(isinstance(dataset, dict), "missing model dataset lineage")
    expected_dataset_role = (
        CURRENT_DATASET_ROLE
        if artifact_role == CURRENT_ARTIFACT_ROLE
        else FORWARD_DATASET_ROLE
    )
    _require(dataset.get("dataset_role") == expected_dataset_role, "model dataset role mismatch")
    _require(
        dataset.get("lineage_schema_version") == LINEAGE_SCHEMA_VERSION,
        "unsupported embedded dataset lineage schema",
    )
    _require(
        dataset.get("source_history_start") == expected_floor,
        "model dataset source-history contract mismatch",
    )
    _require(
        dataset.get("transactional_history_policy") == TRANSACTIONAL_HISTORY_POLICY,
        "model dataset transactional-history policy mismatch",
    )
    _require(
        dataset.get("current_state_exceptions") == CURRENT_STATE_EXCEPTIONS,
        "model dataset current-state exceptions mismatch",
    )
    _require(
        isinstance(dataset.get("parquet_sha256"), str)
        and len(dataset["parquet_sha256"]) == 64,
        "missing model training-dataset hash",
    )
    date_coverage = dataset.get("date_coverage")
    _require(
        isinstance(date_coverage, list) and bool(date_coverage),
        "missing embedded dataset date coverage",
    )
    for item in date_coverage:
        feature_date = item.get("feature_date")
        label_date = item.get("label_date")
        _require(
            isinstance(feature_date, str) and isinstance(label_date, str),
            "invalid embedded dataset dates",
        )
        history = coverage(feature_date, 12)
        _require(label_date >= feature_date, "embedded label date precedes feature date")
        _require(
            item.get("requested_start") == history.requested_start.isoformat()
            and item.get("effective_start") == history.effective_start.isoformat()
            and item.get("history_complete") is history.history_complete,
            "embedded dataset history coverage mismatch",
        )

    validation = lineage.get("validation")
    _require(isinstance(validation, dict), "missing model validation lineage")
    _require(validation.get("passed") is True, "model validation gate did not pass")
    if artifact_role == CURRENT_ARTIFACT_ROLE:
        policy = artifact.get("current_state_policy")
        _require(
            isinstance(policy, dict)
            and float(policy.get("sev180_threshold_eur", -1.0))
            == CURRENT_SEV180_MIN_EUR
            and float(policy.get("minimum_pd", -1.0)) == CURRENT_SEV180_PD_FLOOR,
            "current-state SEV180 policy mismatch",
        )
        _require(
            all(
                item.get("history_complete") is True
                for item in dataset.get("date_coverage", [])
            ),
            "current-state model was not trained on a complete feature window",
        )
        _require(dataset.get("rows", 0) >= CURRENT_MIN_ROWS, "current model row support too low")
        _require(
            dataset.get("positives", 0) >= CURRENT_MIN_POSITIVES,
            "current model positive support too low",
        )
        _require(
            float(validation.get("oof_auc", 0.0)) >= CURRENT_MIN_OOF_AUC,
            "current model OOF AUC below production floor",
        )
        _require(
            validation.get("minimum_rows") == CURRENT_MIN_ROWS
            and validation.get("minimum_positives") == CURRENT_MIN_POSITIVES
            and float(validation.get("minimum_oof_auc", -1.0))
            == CURRENT_MIN_OOF_AUC,
            "current model validation thresholds mismatch",
        )
        _require(
            validation.get("observed_rows") == dataset.get("rows")
            and validation.get("observed_positives") == dataset.get("positives"),
            "current model validation support mismatch",
        )
        _require(
            validation.get("sev180_policy_passed") is True,
            "current-state SEV180 policy validation failed",
        )
        _require(artifact.get("n_rows") == dataset.get("rows"), "artifact row lineage mismatch")
        _require(
            artifact.get("n_pos") == dataset.get("positives"),
            "artifact positive lineage mismatch",
        )
    else:
        _require(
            dataset.get("unique_positive_clients", 0)
            >= FORWARD_MIN_UNIQUE_POSITIVE_CLIENTS,
            "forward model unique-positive-client support too low",
        )
        _require(
            float(validation.get("oot_auc", 0.0)) >= FORWARD_MIN_OOT_AUC,
            "forward model OOT AUC below production floor",
        )
        _require(
            validation.get("minimum_unique_positive_clients")
            == FORWARD_MIN_UNIQUE_POSITIVE_CLIENTS
            and float(validation.get("minimum_oot_auc", -1.0))
            == FORWARD_MIN_OOT_AUC,
            "forward model validation thresholds mismatch",
        )
        _require(
            validation.get("observed_unique_positive_clients")
            == dataset.get("unique_positive_clients"),
            "forward model validation support mismatch",
        )
    return lineage
