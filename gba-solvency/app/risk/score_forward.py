"""Forward 6-month SEV180 risk scoring (production, explainable).

Loads the WOE+logistic scorecard from app/risk/artifacts/forward_scorecard_coeffs.json
and produces a 0-100 forward-risk score + PD-based band for an at-risk client.

Two scorecards live in the artifact:
  * behavioral_only  -- the PRIMARY honest early-warning ranker (chronicity,
    trajectory, RFM, terms). Drives the 0-100 score and the PD band.
  * with_aging       -- includes overdue aging buckets + total_debt. This model
    is near-deterministic because a 6mo SEV180 label is largely the ARITHMETIC
    of existing overdue debt aging into 180+. We expose its PD as an override
    flag ("debt already rolling into default") rather than as the score basis.

Population: at-risk-with-debt only (total_debt_eur > 0, not already SEV180).
Clients with zero debt are not at risk on a 6mo horizon -> score 0, band "none".
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.history import coverage
from app.risk.lineage import (
    CURRENT_STATE_EXCEPTIONS,
    FORWARD_ARTIFACT_ROLE,
    FORWARD_DATASET_ROLE,
    FORWARD_MIN_UNIQUE_POSITIVE_CLIENTS,
    LINEAGE_SCHEMA_VERSION,
    TRANSACTIONAL_HISTORY_POLICY,
    LineageError,
    model_payload_sha256,
    validate_production_artifact,
)

_ART = Path(__file__).resolve().parent / "artifacts" / "forward_scorecard_coeffs.json"
_STATUS = Path(__file__).resolve().parent / "artifacts" / "forward_model_status.json"


class ForwardModelUnavailableError(RuntimeError):
    """The forward model is deliberately unavailable; current-state scoring remains usable."""


@lru_cache(maxsize=1)
def _load_status() -> dict[str, Any]:
    try:
        status = json.loads(_STATUS.read_text())
    except FileNotFoundError as exc:
        raise ForwardModelUnavailableError("forward model status artifact is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardModelUnavailableError("forward model status artifact is unreadable") from exc

    expected = get_settings().source_history_start_date.isoformat()
    if (
        status.get("lineage_schema_version") != LINEAGE_SCHEMA_VERSION
        or status.get("artifact_role") != FORWARD_ARTIFACT_ROLE
        or status.get("source_history_start") != expected
    ):
        raise ForwardModelUnavailableError("forward model status lineage rejected")
    state = status.get("status")
    if state not in {"available", "unavailable"}:
        raise ForwardModelUnavailableError("forward model status is invalid")
    if state == "unavailable" and (
        status.get("reason") != "insufficient_unique_positive_clients"
        or status.get("minimum_unique_positive_clients")
        != FORWARD_MIN_UNIQUE_POSITIVE_CLIENTS
        or not isinstance(status.get("observed_unique_positive_clients"), int)
        or status["observed_unique_positive_clients"]
        >= FORWARD_MIN_UNIQUE_POSITIVE_CLIENTS
    ):
        raise ForwardModelUnavailableError("forward unavailable status evidence is invalid")
    if state == "unavailable":
        dataset = status.get("dataset_lineage")
        if (
            not isinstance(dataset, dict)
            or dataset.get("lineage_schema_version") != LINEAGE_SCHEMA_VERSION
            or dataset.get("dataset_role") != FORWARD_DATASET_ROLE
            or dataset.get("source_history_start") != expected
            or dataset.get("transactional_history_policy")
            != TRANSACTIONAL_HISTORY_POLICY
            or dataset.get("current_state_exceptions") != CURRENT_STATE_EXCEPTIONS
            or dataset.get("parquet_sha256") != status.get("dataset_sha256")
            or dataset.get("positives") != status.get("observed_positive_rows")
            or dataset.get("unique_positive_clients")
            != status.get("observed_unique_positive_clients")
            or not isinstance(status.get("observed_atrisk_rows"), int)
            or status["observed_atrisk_rows"] < status["observed_positive_rows"]
            or not isinstance(status.get("evaluated_at"), str)
        ):
            raise ForwardModelUnavailableError(
                "forward unavailable dataset lineage is invalid"
            )
        date_coverage = dataset.get("date_coverage")
        if not isinstance(date_coverage, list) or not date_coverage:
            raise ForwardModelUnavailableError(
                "forward unavailable date lineage is missing"
            )
        for item in date_coverage:
            try:
                history = coverage(item["feature_date"], 12)
            except (KeyError, TypeError, ValueError) as exc:
                raise ForwardModelUnavailableError(
                    "forward unavailable date lineage is invalid"
                ) from exc
            if (
                item.get("requested_start") != history.requested_start.isoformat()
                or item.get("effective_start") != history.effective_start.isoformat()
                or item.get("history_complete") is not history.history_complete
                or item.get("label_date", "") < item["feature_date"]
            ):
                raise ForwardModelUnavailableError(
                    "forward unavailable date lineage is invalid"
                )
    return status


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    status = _load_status()
    if status["status"] != "available":
        raise ForwardModelUnavailableError(
            f"forward model unavailable: {status['reason']} "
            f"({status['observed_unique_positive_clients']} < "
            f"{status['minimum_unique_positive_clients']} unique positive clients)"
        )
    try:
        artifact = json.loads(_ART.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ForwardModelUnavailableError(
            "forward scorecard artifact is missing or unreadable"
        ) from exc
    try:
        lineage = validate_production_artifact(
            artifact,
            artifact_role=FORWARD_ARTIFACT_ROLE,
        )
    except LineageError as exc:
        raise ForwardModelUnavailableError(
            f"forward scorecard lineage rejected: {exc}"
        ) from exc
    if (
        status.get("training_run_id") != lineage["training_run_id"]
        or status.get("model_payload_sha256") != model_payload_sha256(artifact)
    ):
        raise ForwardModelUnavailableError("forward status/model artifact mismatch")
    return artifact


def forward_model_readiness() -> dict[str, Any]:
    """Return explicit degraded metadata instead of probing the legacy scorecard directly."""
    try:
        status = _load_status()
    except ForwardModelUnavailableError as exc:
        return {"ready": False, "status": "unavailable", "reason": str(exc)}
    if status["status"] != "available":
        return {
            "ready": False,
            "status": "unavailable",
            "reason": status["reason"],
            "source_history_start": status["source_history_start"],
            "observed_unique_positive_clients": status[
                "observed_unique_positive_clients"
            ],
            "minimum_unique_positive_clients": status[
                "minimum_unique_positive_clients"
            ],
            "observed_positive_rows": status["observed_positive_rows"],
            "dataset_sha256": status["dataset_sha256"],
            "evaluated_at": status["evaluated_at"],
        }
    try:
        artifact = _load()
    except (OSError, json.JSONDecodeError, ForwardModelUnavailableError) as exc:
        return {"ready": False, "status": "unavailable", "reason": str(exc)}
    lineage = artifact["training_lineage"]
    return {
        "ready": True,
        "status": "available",
        "reason": None,
        "training_run_id": lineage["training_run_id"],
        "trained_at": lineage["trained_at"],
        "dataset_sha256": lineage["dataset"]["parquet_sha256"],
    }


def _apply_woe(value: float, bins: list[dict]) -> float:
    v = float(value)
    for b in bins:
        lo = b["lo"]
        hi = b["hi"]
        lo_v = -math.inf if lo is None else float(lo)
        hi_v = math.inf if hi is None else float(hi)
        if lo_v == -math.inf:
            if v <= hi_v:
                return float(b["woe"])
        else:
            if lo_v < v <= hi_v:
                return float(b["woe"])
    # value below all bins (e.g. negative) -> nearest (first) bin woe
    return float(bins[0]["woe"]) if bins else 0.0


def _scorecard_pd(features: dict[str, float], card: dict) -> float:
    logit = card["intercept"]
    for c in card["feat_cols"]:
        woe = _apply_woe(features.get(c, 0.0), card["bins"][c])
        logit += card["coefficients"][c] * woe
    return 1.0 / (1.0 + math.exp(-logit))


def _band(pd_beh: float, bands: dict) -> str:
    if pd_beh >= bands["very_high"]:
        return "very_high"
    if pd_beh >= bands["high"]:
        return "high"
    if pd_beh >= bands["medium"]:
        return "medium"
    return "low"


def score_forward(features: dict[str, float]) -> dict[str, Any]:
    """Score one client for 6-month forward SEV180 risk.

    `features` -> mapping of the raw feature columns (same names as the vintage
    dataset). Missing keys default to 0.

    Returns dict with:
      score          : 0-100, higher = higher forward-default risk (behavioral)
      pd_behavioral  : behavioral-only PD (honest early-warning)
      pd_with_aging  : with-aging PD (near-deterministic arithmetic view)
      band           : low / medium / high / very_high (or 'none' if no debt)
      already_rolling: bool, pd_with_aging >= 0.5 (overdue debt arithmetically
                       on track to hit 180+ within horizon)
    """
    art = _load()
    total_debt = float(features.get("total_debt_eur", 0.0) or 0.0)
    if total_debt <= 0:
        return {"score": 0.0, "pd_behavioral": 0.0, "pd_with_aging": 0.0,
                "band": "none", "already_rolling": False,
                "note": "no debt -> not at risk on 6mo horizon"}

    pd_beh = _scorecard_pd(features, art["scorecard_behavioral_only"])
    pd_aging = _scorecard_pd(features, art["scorecard_with_aging"])
    band = _band(pd_beh, art["pd_bands"])
    return {
        "score": round(100.0 * pd_beh, 1),
        "pd_behavioral": round(pd_beh, 4),
        "pd_with_aging": round(pd_aging, 4),
        "band": band,
        "already_rolling": bool(pd_aging >= 0.5),
    }
