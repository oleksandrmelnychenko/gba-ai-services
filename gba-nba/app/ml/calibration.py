"""Optional post-hoc probability calibration, fitted out of time and bound to one model artefact.

artifacts/calibration.json holds per task type a logit-linear (Platt) map
p' = sigmoid(a * logit(p) + b). The file names the SHA-256 of the propensity model it was fitted
for; any other artefact, a missing file or a missing type leaves the probability unchanged.
The base model is never refitted here.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

ART = Path(__file__).resolve().parent / "artifacts"
FILE = "calibration.json"
MODEL_FILE = "propensity_model.joblib"
MIN_ROWS = 300
_EPS = 1e-4


def model_sha256(directory: Path | None = None) -> str | None:
    path = (directory or ART) / MODEL_FILE
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _logit(p: float) -> float:
    p = min(max(float(p), _EPS), 1.0 - _EPS)
    return math.log(p / (1.0 - p))


def transform(p: float, a: float, b: float) -> float:
    return 1.0 / (1.0 + math.exp(-(a * _logit(p) + b)))


@lru_cache(maxsize=1)
def _load() -> dict:
    path = ART / FILE
    if not path.is_file():
        return {"active": False, "reason": "calibration_absent", "types": {}}
    try:
        document = json.loads(path.read_text())
        types = {
            name: (float(row["a"]), float(row["b"]))
            for name, row in (document.get("types") or {}).items()
        }
    except (OSError, TypeError, ValueError, KeyError):
        return {"active": False, "reason": "calibration_unreadable", "types": {}}
    if document.get("model_sha256") != model_sha256():
        return {"active": False, "reason": "calibration_model_mismatch", "types": {}}
    if any(not math.isfinite(a) or not math.isfinite(b) or a <= 0 for a, b in types.values()):
        return {"active": False, "reason": "calibration_invalid", "types": {}}
    return {"active": bool(types), "reason": None if types else "calibration_empty", "types": types,
            "fitted_at": document.get("fitted_at"), "method": document.get("method")}


def state() -> dict:
    loaded = _load()
    return {"calibration_active": loaded["active"], "calibration_reason": loaded["reason"],
            "calibration_types": sorted(loaded["types"]),
            "calibration_fitted_at": loaded.get("fitted_at")}


def apply(task_type: str | None, p: float) -> float:
    params = _load()["types"].get(task_type or "")
    return float(p) if params is None else transform(p, *params)


def fit_platt(probabilities, outcomes) -> tuple[float, float]:
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    x = np.array([_logit(value) for value in probabilities], dtype=float).reshape(-1, 1)
    y = np.array(list(outcomes), dtype=int)
    if len(y) < MIN_ROWS or y.min() == y.max():
        raise ValueError("calibration needs both outcomes and at least MIN_ROWS rows")
    model = LogisticRegression(C=1e6, max_iter=1000).fit(x, y)
    return float(model.coef_[0][0]), float(model.intercept_[0])


def fit_document(pairs: dict[str, tuple[list[float], list[int]]], sha256: str) -> dict:
    """pairs: task_type -> (out-of-time probabilities of this model design, realised outcomes)."""
    types = {}
    for name, (probabilities, outcomes) in sorted(pairs.items()):
        a, b = fit_platt(probabilities, outcomes)
        if a <= 0:
            continue
        calibrated = [transform(value, a, b) for value in probabilities]
        types[name] = {"a": a, "b": b, "n": len(outcomes),
                       "mean_raw": sum(probabilities) / len(probabilities),
                       "mean_calibrated": sum(calibrated) / len(calibrated),
                       "mean_outcome": sum(outcomes) / len(outcomes)}
    return {"version": 1, "method": "platt", "model_sha256": sha256,
            "fitted_at": datetime.now(UTC).isoformat(), "types": types}
