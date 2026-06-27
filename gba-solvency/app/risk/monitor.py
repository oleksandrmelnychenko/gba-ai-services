"""Production drift monitoring for the solvency scorecard (v3).

Computes the Population Stability Index (PSI) of the CURRENT live score distribution and of
the key model features against a frozen TRAINING BASELINE. PSI is the standard credit-risk
drift gauge:

    PSI = sum_bins( (live% - base%) * ln(live% / base%) )

    PSI < 0.10  -> ok    (no material shift)
    0.10..0.25  -> warn  (moderate shift, investigate)
    PSI >= 0.25 -> alert (major shift, model likely stale -> retrain)

The BASELINE (per-feature quantile bin edges + expected bin proportions, and the same for the
0-100 score) is persisted ONCE into app/risk/artifacts/monitor_baseline.json from the training
artifacts (data/current_state_scores.parquet for the score, data/risk_dataset_v3.parquet for the
features). Rebuild it via `build_baseline()` after every retrain so the baseline tracks the model
that is actually serving.

drift_report() is the cheap, cached entrypoint the API calls: it samples a few hundred live
buyers, scores them with the EXACT serving path (features_many -> score_current), and returns
{psi_score, psi_top_features, drift_level, n_scored}. The result is cached in-process for
`refresh_hours` so /health never pays the DB cost more than a few times a day.
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.risk import dataset as risk_dataset
from app.risk.score_current import score_current

log = get_logger("solvency_monitor")

_ART = Path(__file__).resolve().parent / "artifacts"
_BASELINE_PATH = _ART / "monitor_baseline.json"

# Features tracked for drift — the strongest behavioral + exposure drivers of the scorecard.
# (overdue_eur_180plus / pct_debt_180plus are tautological and excluded from the model, so they
# are not tracked here either.)
MONITORED_FEATURES: list[str] = [
    "total_debt_eur",
    "current_debt_eur",
    "max_overdue_days",
    "n_open_debt_lines",
    "limit_utilization",
    "credit_limit_eur",
    "turnover_eur_12mo",
    "order_count_12mo",
    "recency_days",
    "return_rate_12mo",
]

# Drift thresholds (industry-standard PSI bands).
PSI_WARN = 0.10
PSI_ALERT = 0.25

# Live sampling / caching defaults (kept cheap on purpose).
DEFAULT_SAMPLE_SIZE = 300
DEFAULT_REFRESH_HOURS = 6.0
_SAMPLE_SEED = 13  # deterministic sample so repeated reports are comparable

_N_BINS = 10
_EPS = 1e-6  # floor on proportions so ln() never blows up on an empty bin


# --------------------------------------------------------------------------------------------
# Baseline construction (one-time / post-retrain) — bins + expected proportions
# --------------------------------------------------------------------------------------------
def _quantile_edges(values: list[float], n_bins: int) -> list[float]:
    """Interior quantile edges for `values`. Degenerate (constant / heavily-tied) series collapse
    to whatever distinct edges exist, so a near-constant feature simply yields a few bins."""
    xs = sorted(float(v) for v in values if v is not None and not _isnan(v))
    if len(xs) < 2:
        return []
    edges: list[float] = []
    for i in range(1, n_bins):
        q = i / n_bins
        pos = q * (len(xs) - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(xs) - 1)
        frac = pos - lo
        edges.append(xs[lo] + (xs[hi] - xs[lo]) * frac)
    # de-dupe collapsed edges (ties) while keeping ascending order
    out: list[float] = []
    for e in edges:
        if not out or e > out[-1] + 1e-12:
            out.append(e)
    return out


def _isnan(v: float) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _bin_index(v: float, edges: list[float]) -> int:
    """Bin a value into edges: bin k = (edges[k-1], edges[k]]; len = len(edges)+1."""
    if v is None or _isnan(v):
        return 0
    for i, e in enumerate(edges):
        if v <= e:
            return i
    return len(edges)


def _proportions(values: list[float], edges: list[float]) -> list[float]:
    n_bins = len(edges) + 1
    counts = [0] * n_bins
    total = 0
    for v in values:
        counts[_bin_index(v, edges)] += 1
        total += 1
    if total == 0:
        return [1.0 / n_bins] * n_bins
    return [max(c / total, _EPS) for c in counts]


def _baseline_from_value_map(
    score_vals: list[float],
    feat_values: dict[str, list[float]],
    n_rows: int,
    meta: dict[str, Any],
    out: Path,
) -> dict[str, Any]:
    """Freeze (edges + expected proportions) for the score and each monitored feature."""
    baseline: dict[str, Any] = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_bins": _N_BINS,
        "n_rows": int(n_rows),
        **meta,
        "score": {},
        "features": {},
    }
    s_edges = _quantile_edges(score_vals, _N_BINS)
    baseline["score"] = {
        "edges": s_edges,
        "expected": _proportions(score_vals, s_edges),
        "mean": float(sum(score_vals) / len(score_vals)) if score_vals else 0.0,
    }
    for feat, vals in feat_values.items():
        edges = _quantile_edges(vals, _N_BINS)
        baseline["features"][feat] = {
            "edges": edges,
            "expected": _proportions(vals, edges),
            "mean": float(sum(vals) / len(vals)) if vals else 0.0,
        }
    out.write_text(json.dumps(baseline, indent=2))
    log.info("monitor_baseline_built", path=str(out), n_features=len(baseline["features"]),
             source=meta.get("source"))
    return baseline


def build_baseline_serving(
    as_of: str,
    sample_size: int | None = None,
    out_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze the baseline by running the EXACT serving path (features_many -> score_current).

    This is the correct baseline: it bins the score + features produced by the same code that
    serves live, so PSI is a true apples-to-apples comparison. (Binning the raw training parquet
    instead would mismatch the no-sales recency sentinel — build_dataset uses worst_observed+1
    while the serving path uses a fixed sentinel — and fabricate drift.) Pin `as_of` to the
    model's training feature date so the baseline reflects the population the model learned on.
    """
    out = Path(out_path) if out_path else _BASELINE_PATH
    ids = risk_dataset.buyer_ids()
    if sample_size and len(ids) > sample_size:
        rng = random.Random(_SAMPLE_SEED)
        ids = sorted(rng.sample(ids, sample_size))

    feats_by_cid = risk_dataset.features_many(ids, as_of)
    score_vals: list[float] = []
    feat_values: dict[str, list[float]] = {f: [] for f in MONITORED_FEATURES}
    for cid in ids:
        feats = feats_by_cid.get(cid)
        if not feats:
            continue
        score_vals.append(float(score_current(feats)["score"]))
        for f in feat_values:
            feat_values[f].append(float(feats.get(f, 0.0)))

    return _baseline_from_value_map(
        score_vals, feat_values, len(score_vals),
        {"source": "serving_path", "as_of": as_of}, out,
    )


def build_baseline(
    scores_parquet: str | Path | None = None,
    dataset_parquet: str | Path | None = None,
    out_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze the baseline from the training parquets (score from current_state_scores.parquet,
    features from risk_dataset_v3.parquet).

    NOTE: the parquet feature path uses build_dataset's population-worst recency sentinel, which
    differs from the serving path's fixed sentinel; prefer build_baseline_serving() so PSI is
    apples-to-apples. This parquet variant is kept for quick offline baselining of features that
    don't depend on the recency sentinel.
    """
    import pandas as pd  # local import: monitoring at request time must not need pandas loaded

    root = Path(__file__).resolve().parents[2]
    scores_path = Path(scores_parquet) if scores_parquet else root / "data" / "current_state_scores.parquet"
    ds_path = Path(dataset_parquet) if dataset_parquet else root / "data" / "risk_dataset_v3.parquet"
    out = Path(out_path) if out_path else _BASELINE_PATH

    scores_df = pd.read_parquet(scores_path)
    ds_df = pd.read_parquet(ds_path)

    score_vals = [float(v) for v in scores_df["score"].tolist()]
    feat_values = {
        f: [float(v) for v in ds_df[f].tolist()]
        for f in MONITORED_FEATURES
        if f in ds_df.columns
    }
    return _baseline_from_value_map(
        score_vals, feat_values, len(ds_df), {"source": "parquet"}, out,
    )


# --------------------------------------------------------------------------------------------
# PSI
# --------------------------------------------------------------------------------------------
def _psi(expected: list[float], live: list[float]) -> float:
    """PSI between an expected (baseline) and live proportion vector over identical bins."""
    total = 0.0
    for e, a in zip(expected, live, strict=True):
        e = max(e, _EPS)
        a = max(a, _EPS)
        total += (a - e) * math.log(a / e)
    return total


def _drift_level(psi: float) -> str:
    if psi >= PSI_ALERT:
        return "alert"
    if psi >= PSI_WARN:
        return "warn"
    return "ok"


# --------------------------------------------------------------------------------------------
# Cached live drift report
# --------------------------------------------------------------------------------------------
_lock = threading.Lock()
_cache: dict[str, Any] = {"report": None, "ts": 0.0}


def _load_baseline() -> dict[str, Any] | None:
    if not _BASELINE_PATH.exists():
        return None
    try:
        return json.loads(_BASELINE_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("monitor_baseline_unreadable", error=str(exc))
        return None


def _sample_buyers(sample_size: int) -> list[int]:
    """A deterministic sample of role-1 buyers for live scoring (cheap, repeatable)."""
    ids = risk_dataset.buyer_ids()
    if len(ids) <= sample_size:
        return ids
    rng = random.Random(_SAMPLE_SEED)
    return sorted(rng.sample(ids, sample_size))


def _compute_report(sample_size: int, as_of: str | None) -> dict[str, Any]:
    """Score a sample on the EXACT serving path and PSI it against the frozen baseline."""
    from datetime import datetime

    baseline = _load_baseline()
    if baseline is None:
        return {
            "psi_score": None,
            "psi_top_features": [],
            "drift_level": "unknown",
            "n_scored": 0,
            "note": "no monitor_baseline.json — run app.risk.monitor.build_baseline()",
        }

    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    sample = _sample_buyers(sample_size)
    feats_by_cid = risk_dataset.features_many(sample, as_of)

    live_scores: list[float] = []
    feat_values: dict[str, list[float]] = {f: [] for f in baseline["features"]}
    for cid in sample:
        feats = feats_by_cid.get(cid)
        if not feats:
            continue
        live_scores.append(float(score_current(feats)["score"]))
        for f in feat_values:
            feat_values[f].append(float(feats.get(f, 0.0)))

    n_scored = len(live_scores)

    # score PSI
    s_base = baseline["score"]
    psi_score = _psi(s_base["expected"], _proportions(live_scores, s_base["edges"])) if n_scored else None

    # per-feature PSI
    feat_psi: dict[str, float] = {}
    for f, meta in baseline["features"].items():
        vals = feat_values.get(f, [])
        if not vals:
            continue
        feat_psi[f] = round(_psi(meta["expected"], _proportions(vals, meta["edges"])), 4)

    top = sorted(feat_psi.items(), key=lambda kv: kv[1], reverse=True)
    psi_top_features = [{"feature": k, "psi": v, "level": _drift_level(v)} for k, v in top[:5]]

    worst = max([psi_score or 0.0, *(feat_psi.values() or [0.0])])
    level = _drift_level(worst)

    return {
        "psi_score": round(psi_score, 4) if psi_score is not None else None,
        "psi_top_features": psi_top_features,
        "drift_level": level,
        "n_scored": n_scored,
        "as_of": as_of,
        "baseline_created_at": baseline.get("created_at"),
    }


def drift_report(
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    refresh_hours: float = DEFAULT_REFRESH_HOURS,
    as_of: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Cached live drift report. Recomputes at most once per `refresh_hours` (per process).

    Returns {psi_score, psi_top_features, drift_level: ok|warn|alert|unknown, n_scored, ...}.
    A WARNING is logged (once per refresh) whenever drift_level is not ok.
    """
    now = time.time()
    with _lock:
        cached = _cache["report"]
        fresh = cached is not None and (now - _cache["ts"]) < refresh_hours * 3600
        if fresh and not force:
            return cached

    report = _compute_report(sample_size, as_of)

    with _lock:
        _cache["report"] = report
        _cache["ts"] = now

    if report.get("drift_level") not in ("ok", "unknown"):
        log.warning(
            "model_drift_detected",
            drift_level=report["drift_level"],
            psi_score=report.get("psi_score"),
            psi_top_features=report.get("psi_top_features"),
            n_scored=report.get("n_scored"),
        )
    return report


def cached_report() -> dict[str, Any] | None:
    """Return the last computed report without triggering a recompute (for /health)."""
    with _lock:
        return _cache["report"]
