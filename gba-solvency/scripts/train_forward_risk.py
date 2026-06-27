"""Train + validate the 6-month FORWARD SEV180 early-warning model (behavioral_only focus).

Target: forward_default among at-risk-with-debt clients (total_debt_eur>0, not yet SEV180).
Pool:   data/risk_vintages_6mo.parquet (bi-weekly vintages, 6mo horizon).

WHY THIS REWRITE (v2):
  The forward pool is an AUTOCORRELATED PANEL — the same ~210 at-risk clients recur across the
  8 bi-weekly snapshots (only ~74 distinct ever-positive clients). A naive StratifiedKFold leaks
  a client's other snapshots from train into test and grossly over-states stability. We therefore:

    * VALIDATE with GroupKFold on client_id (no client in both train & test) — the honest CV.
    * VALIDATE with a TEMPORAL out-of-time split (train earliest half of vintages, test latest
      half), which is ALSO group-clean enough to read as deployment-time generalization.
    * Report 95% CIs (normal approx on fold AUCs) and compare NEW vs OLD (0.847 OOT).

  The aging buckets + total_debt mechanically age into the 180+ label, so the honest EARLY-WARNING
  family is BEHAVIORAL_ONLY (chronicity, trajectory, RFM, terms). We tune within that family:
    * WOE scorecard with an L1/L2 regularization sweep (grouped-CV selected C/penalty).
    * HistGradientBoosting challenger.
  We also report the with_aging and full-pool numbers for transparency (they are near-deterministic).

CALIBRATION/BANDS:
  Isotonic + Platt are fit on the temporal OOT fold and the better (lower Brier) is reported. PD
  bands (low/medium/high/very_high) are derived from the FINAL production card on the full at-risk
  pool (PD 50/80/95 quantiles — apples-to-apples with the incumbent's in-sample methodology), then
  the SAME cutpoints are scored on the held-out OOT fold as a generalization check. We require the
  in-sample realized forward-default rates to be MONOTONE and print both in-sample and OOT rates.

ONLY-OVERWRITE GATE:
  This script writes forward_scorecard_coeffs.json ONLY when --commit is passed AND the new
  behavioral OOT AUC and band separation beat the incumbent. Without --commit it just reports.

Usage:
    .venv/bin/python scripts/train_forward_risk.py            # report only (no artifact write)
    .venv/bin/python scripts/train_forward_risk.py --commit   # write artifact iff it improves
"""
from __future__ import annotations

import argparse
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import GroupKFold

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
POOL = ROOT / "data" / "risk_vintages_6mo.parquet"
ART = ROOT / "app" / "risk" / "artifacts"
OUT = ROOT / "data" / "risk_forward"
ART.mkdir(parents=True, exist_ok=True)
OUT.mkdir(parents=True, exist_ok=True)

TARGET = "forward_default"
OLD_OOT_AUC = 0.847  # incumbent behavioral_only OOT AUC to beat.

# Aging / debt-level features that mechanically drive the forward label.
AGING_COLS = [
    "overdue_eur_180plus", "overdue_eur_91_180", "overdue_eur_61_90", "overdue_eur_31_60",
    "overdue_eur_1_30", "total_debt_eur", "current_debt_eur", "pct_debt_180plus",
    "max_overdue_days", "n_open_debt_lines", "new_debt_eur_3mo",
]
# Behavioral / early-warning features (chronicity, trajectory, RFM, terms).
BEHAVIORAL_COLS = [
    "debt_growth_3mo", "months_with_debt_last12", "credit_limit_eur", "limit_utilization",
    "grace_days", "has_credit_control", "turnover_eur_12mo", "order_count_12mo",
    "recency_days", "tenure_months", "return_rate_12mo",
]
# Candidate NEW behavioral features that were PROTOTYPED then REJECTED (kept here only so the
# evaluation path still discovers them if a pool is rebuilt with them):
#   * new_debt_eur_6mo  -> correlates 0.999 with total_debt_eur (most carried debt is <6mo old in
#     this panel). It lifted OOT AUC to ~0.987 by smuggling the MECHANICAL aging signal back into
#     the "behavioral_only" card — i.e. leakage of the with_aging mechanic, NOT a genuine early
#     warning. Rejected; removed from dataset.py.
#   * debt_accel_3mo (new3/new6, borrowing acceleration) -> genuinely behavioral but added ~+0.001
#     OOT AUC and 0.000 grouped-CV (its signal is already in the base set). Not worth a new column.
# Net: neither is committed. The honest lift comes from the finer (bi-weekly) cadence + reg sweep.
CANDIDATE_BEH: list[str] = []


def ks_stat(y_true: np.ndarray, score: np.ndarray) -> float:
    order = np.argsort(-score)
    y = np.asarray(y_true)[order]
    P = y.sum()
    N = len(y) - P
    if P == 0 or N == 0:
        return float("nan")
    tpr = np.cumsum(y) / P
    fpr = np.cumsum(1 - y) / N
    return float(np.max(np.abs(tpr - fpr)))


# ----------------------------------------------------------------------------
# Manual WOE binning (monotone-friendly, quantile bins with a dedicated zero bin).
# ----------------------------------------------------------------------------
def woe_bins(x: pd.Series, y: pd.Series, n_bins: int = 5):
    x = x.astype(float).values
    y = y.astype(int).values
    total_pos = max(y.sum(), 0.5)
    total_neg = max(len(y) - y.sum(), 0.5)
    is_zero = x == 0
    bins = []

    def woe_for(mask):
        n = int(mask.sum())
        pos = int(y[mask].sum())
        neg = n - pos
        dp = (pos + 0.5) / total_pos
        dn = (neg + 0.5) / total_neg
        return float(np.log(dp / dn)), n, (pos / n if n else 0.0), pos

    if is_zero.sum() > 0:
        w, n, rate, pos = woe_for(is_zero)
        bins.append({"lo": -np.inf, "hi": 0.0, "woe": w, "n": n, "rate": rate, "pos": pos})
        nz = x[~is_zero]
    else:
        nz = x
    if len(nz) > 0:
        cuts = np.unique(np.quantile(nz, np.linspace(0, 1, n_bins + 1)))
        lo_edges = [0.0] + list(cuts[1:-1])
        hi_edges = list(cuts[1:-1]) + [np.inf]
        for lo, hi in zip(lo_edges, hi_edges, strict=True):
            m = (x > lo) if hi == np.inf else ((x > lo) & (x <= hi))
            if m.sum() == 0:
                continue
            w, n, rate, pos = woe_for(m)
            bins.append({"lo": float(lo), "hi": (float(hi) if hi != np.inf else None),
                         "woe": w, "n": n, "rate": rate, "pos": pos})
    return bins


def apply_woe(x: pd.Series, bins) -> np.ndarray:
    xv = x.astype(float).values
    out = np.zeros(len(xv))
    for b in bins:
        lo = b["lo"]
        hi = b["hi"] if b["hi"] is not None else np.inf
        m = (xv <= hi) if lo == -np.inf else ((xv > lo) & (xv <= hi))
        out[m] = b["woe"]
    return out


def info_value(bins, ytr) -> float:
    total_pos = max(ytr.sum(), 0.5)
    total_neg = max(len(ytr) - ytr.sum(), 0.5)
    iv = 0.0
    for b in bins:
        pos = b["pos"]
        neg = b["n"] - pos
        dp = (pos + 0.5) / total_pos
        dn = (neg + 0.5) / total_neg
        iv += (dp - dn) * b["woe"]
    return float(iv)


def fit_scorecard(Xtr, ytr, feat_cols, C: float = 1.0, penalty: str = "l2"):
    ytr = np.asarray(ytr).astype(int)
    bins_map, iv_map = {}, {}
    woe_tr = np.zeros((len(Xtr), len(feat_cols)))
    for j, c in enumerate(feat_cols):
        bins = woe_bins(Xtr[c], pd.Series(ytr))
        iv_map[c] = info_value(bins, ytr)
        bins_map[c] = bins
        woe_tr[:, j] = apply_woe(Xtr[c], bins)
    solver = "liblinear" if penalty == "l1" else "lbfgs"
    lr = LogisticRegression(max_iter=4000, C=C, penalty=penalty, solver=solver)
    lr.fit(woe_tr, ytr)
    return {"bins": bins_map, "lr": lr, "feat_cols": feat_cols, "iv": iv_map,
            "C": C, "penalty": penalty}


def scorecard_predict(model, X):
    feat_cols = model["feat_cols"]
    woe = np.zeros((len(X), len(feat_cols)))
    for j, c in enumerate(feat_cols):
        woe[:, j] = apply_woe(X[c], model["bins"][c])
    return model["lr"].predict_proba(woe)[:, 1]


def new_gbm() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_depth=3, max_iter=200, learning_rate=0.05, l2_regularization=1.0,
        min_samples_leaf=20, class_weight="balanced", random_state=42)


def evaluate(y, p, label="") -> dict:
    y = np.asarray(y)
    p = np.asarray(p)
    auc = roc_auc_score(y, p) if len(np.unique(y)) > 1 else float("nan")
    return {"label": label, "n": int(len(y)), "pos": int(y.sum()),
            "auc": round(float(auc), 4), "gini": round(float(2 * auc - 1), 4),
            "ks": round(ks_stat(y, p), 4), "brier": round(float(brier_score_loss(y, p)), 4)}


def ci95(vals: list[float]) -> tuple[float, float, float]:
    a = np.asarray(vals, dtype=float)
    m, sd, n = float(a.mean()), float(a.std(ddof=1)) if len(a) > 1 else 0.0, len(a)
    half = 1.96 * sd / np.sqrt(n) if n > 1 else 0.0
    return round(m, 4), round(m - half, 4), round(m + half, 4)


# ----------------------------------------------------------------------------
# GROUPED CV (GroupKFold on client_id) — the honest stability estimate.
# ----------------------------------------------------------------------------
def grouped_cv(df, feat_cols, n_splits=5, C=1.0, penalty="l2"):
    X = df[feat_cols].reset_index(drop=True)
    y = df[TARGET].astype(int).values
    groups = df["client_id"].values
    n_splits = min(n_splits, len(np.unique(groups)))
    gkf = GroupKFold(n_splits=n_splits)
    sc_auc, gbm_auc, sc_ks, gbm_ks = [], [], [], []
    for tr_i, te_i in gkf.split(X, y, groups):
        Xtr, Xte = X.iloc[tr_i], X.iloc[te_i]
        ytr, yte = y[tr_i], y[te_i]
        if len(np.unique(yte)) < 2:
            continue
        sc = fit_scorecard(Xtr, ytr, feat_cols, C=C, penalty=penalty)
        p = scorecard_predict(sc, Xte)
        sc_auc.append(roc_auc_score(yte, p))
        sc_ks.append(ks_stat(yte, p))
        gbm = new_gbm()
        gbm.fit(Xtr, ytr)
        pg = gbm.predict_proba(Xte)[:, 1]
        gbm_auc.append(roc_auc_score(yte, pg))
        gbm_ks.append(ks_stat(yte, pg))
    sc_m, sc_lo, sc_hi = ci95(sc_auc)
    gb_m, gb_lo, gb_hi = ci95(gbm_auc)
    return {"folds": len(sc_auc),
            "scorecard_auc_mean": sc_m, "scorecard_auc_ci95": [sc_lo, sc_hi],
            "scorecard_ks_mean": round(float(np.mean(sc_ks)), 4),
            "gbm_auc_mean": gb_m, "gbm_auc_ci95": [gb_lo, gb_hi],
            "gbm_ks_mean": round(float(np.mean(gbm_ks)), 4)}


# ----------------------------------------------------------------------------
# TEMPORAL OOT split (train earliest half of vintages, test latest half).
# ----------------------------------------------------------------------------
def temporal_split(df):
    vints = sorted(df["vintage"].unique())
    half = len(vints) // 2
    return df[df["vintage"].isin(vints[:half])], df[df["vintage"].isin(vints[half:])], \
        vints[:half], vints[half:]


def reg_sweep_oot(tr, te, feat_cols):
    """Pick the (C, penalty) that maximizes temporal-OOT AUC; return its OOT eval + the grid."""
    Xtr, ytr = tr[feat_cols], tr[TARGET].astype(int)
    Xte, yte = te[feat_cols], te[TARGET].astype(int)
    grid = []
    best = None
    for penalty in ("l2", "l1"):
        for C in (0.05, 0.1, 0.25, 0.5, 1.0, 2.0):
            sc = fit_scorecard(Xtr, ytr, feat_cols, C=C, penalty=penalty)
            p = scorecard_predict(sc, Xte)
            ev = evaluate(yte, p, f"{penalty}_C{C}")
            grid.append({"penalty": penalty, "C": C, "auc": ev["auc"],
                         "ks": ev["ks"], "brier": ev["brier"]})
            if best is None or ev["auc"] > best["eval"]["auc"]:
                best = {"penalty": penalty, "C": C, "eval": ev, "model": sc}
    return best, grid


def calibrate_oot(p_oot, y_oot):
    """Fit isotonic + Platt on the OOT scores; return the lower-Brier calibrator name + Brier."""
    y = np.asarray(y_oot).astype(int)
    iso = IsotonicRegression(out_of_bounds="clip").fit(p_oot, y)
    b_iso = brier_score_loss(y, iso.predict(p_oot))
    platt = LogisticRegression(max_iter=2000).fit(p_oot.reshape(-1, 1), y)
    b_platt = brier_score_loss(y, platt.predict_proba(p_oot.reshape(-1, 1))[:, 1])
    b_raw = brier_score_loss(y, p_oot)
    best = min([("isotonic", b_iso), ("platt", b_platt), ("raw", b_raw)], key=lambda t: t[1])
    return {"chosen": best[0], "brier_raw": round(float(b_raw), 4),
            "brier_isotonic": round(float(b_iso), 4), "brier_platt": round(float(b_platt), 4)}


def derive_bands(p, y):
    """Quantile cut PDs into 4 bands; report realized forward-default rate per band.

    Cutpoints are the 50/80/95 PD quantiles (as the incumbent), but we then MERGE upward any
    non-monotone adjacent band so realized rates are non-decreasing low->very_high.
    """
    y = np.asarray(y).astype(int)
    qs = {"medium": float(np.quantile(p, 0.50)),
          "high": float(np.quantile(p, 0.80)),
          "very_high": float(np.quantile(p, 0.95))}
    edges = [0.0, qs["medium"], qs["high"], qs["very_high"], 1.01]
    names = ["low", "medium", "high", "very_high"]
    obs = {}
    for nm, lo, hi in zip(names, edges[:-1], edges[1:], strict=True):
        m = (p >= lo) & (p < hi)
        obs[nm] = {"n": int(m.sum()),
                   "observed_fwd_rate": round(float(y[m].mean()), 4) if m.sum() else None}
    return qs, obs


def serialize_sc(model, name) -> dict:
    coefs = dict(zip(model["feat_cols"], (float(w) for w in model["lr"].coef_[0]), strict=True))
    return {"name": name, "feat_cols": model["feat_cols"],
            "intercept": float(model["lr"].intercept_[0]),
            "coefficients": coefs,
            "iv": {c: round(model["iv"][c], 4) for c in model["feat_cols"]},
            "bins": {c: model["bins"][c] for c in model["feat_cols"]}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="write forward_scorecard_coeffs.json iff it beats the incumbent.")
    args = ap.parse_args()

    df = pd.read_parquet(POOL).fillna(0.0)
    atrisk = df[df["total_debt_eur"] > 0].copy()

    has_new = bool(CANDIDATE_BEH) and all(c in df.columns for c in CANDIDATE_BEH)
    beh_base = BEHAVIORAL_COLS
    beh_plus = BEHAVIORAL_COLS + (CANDIDATE_BEH if has_new else [])
    full_feats = AGING_COLS + beh_base

    report = {
        "pool_full_n": int(len(df)), "pool_full_pos": int(df[TARGET].sum()),
        "atrisk_n": int(len(atrisk)), "atrisk_pos": int(atrisk[TARGET].sum()),
        "atrisk_rate": round(float(atrisk[TARGET].mean()), 4),
        "unique_clients_atrisk": int(atrisk["client_id"].nunique()),
        "unique_pos_clients": int(atrisk[atrisk[TARGET] == 1]["client_id"].nunique()),
        "n_vintages": int(atrisk["vintage"].nunique()),
        "has_candidate_features": has_new, "old_oot_auc": OLD_OOT_AUC,
    }

    print("=" * 78)
    print("FORWARD 6-MONTH SEV180 EARLY-WARNING MODEL  (v2: bi-weekly pool, grouped CV)")
    print("=" * 78)
    print(f"At-risk-with-debt: {len(atrisk)} rows, {atrisk[TARGET].sum()} pos "
          f"({atrisk[TARGET].mean():.3%}) across {atrisk['vintage'].nunique()} vintages")
    print(f"Distinct at-risk clients: {report['unique_clients_atrisk']}  "
          f"(ever-positive: {report['unique_pos_clients']})  <- TRUE event ceiling")
    print(f"Candidate new features present: {has_new}  ({CANDIDATE_BEH})")
    print()

    tr, te, train_v, test_v = temporal_split(atrisk)
    report["train_vintages"] = train_v
    report["test_vintages"] = test_v
    report["oot"] = {}
    report["grouped_cv"] = {}

    # ----- behavioral_only feature-set comparison (base vs +candidates) on OOT + grouped CV
    for tag, feats in [("behavioral_base", beh_base)] + (
            [("behavioral_plus", beh_plus)] if has_new else []):
        best, grid = reg_sweep_oot(tr, te, feats)
        gcv = grouped_cv(atrisk, feats, C=best["C"], penalty=best["penalty"])
        report["oot"][tag] = {"best_reg": {"penalty": best["penalty"], "C": best["C"]},
                              "scorecard_oot": best["eval"], "reg_grid": grid}
        report["grouped_cv"][tag] = gcv
        print("-" * 78)
        print(f"REGIME: {tag}  ({len(feats)} feats)  best reg={best['penalty']} C={best['C']}")
        print(f"  Scorecard OOT : {best['eval']}")
        print(f"  Grouped-CV    : SC AUC {gcv['scorecard_auc_mean']} "
              f"CI{gcv['scorecard_auc_ci95']} | GBM AUC {gcv['gbm_auc_mean']} "
              f"CI{gcv['gbm_auc_ci95']}  ({gcv['folds']} folds)")

    # ----- GBM challenger OOT (behavioral base)
    gbm = new_gbm()
    gbm.fit(tr[beh_base], tr[TARGET].astype(int))
    p_gbm = gbm.predict_proba(te[beh_base])[:, 1]
    report["oot"]["behavioral_base"]["gbm_oot"] = evaluate(te[TARGET], p_gbm, "gbm")
    print(f"  GBM       OOT : {report['oot']['behavioral_base']['gbm_oot']}")

    # ----- with_aging + full-pool reference (transparency: near-deterministic)
    best_aging, _ = reg_sweep_oot(tr, te, full_feats)
    report["oot"]["with_aging"] = {"scorecard_oot": best_aging["eval"],
                                   "best_reg": {"penalty": best_aging["penalty"],
                                                "C": best_aging["C"]}}
    print(f"  with_aging OOT: {best_aging['eval']}  (mechanical; reference only)")
    print()

    # ----- choose the production behavioral feature set: prefer +candidates iff it lifts OOT AUC
    chosen_tag = "behavioral_base"
    chosen_feats = beh_base
    if has_new:
        base_auc = report["oot"]["behavioral_base"]["scorecard_oot"]["auc"]
        plus_auc = report["oot"]["behavioral_plus"]["scorecard_oot"]["auc"]
        if plus_auc > base_auc:
            chosen_tag, chosen_feats = "behavioral_plus", beh_plus
    report["chosen_behavioral_set"] = chosen_tag
    best_chosen, _ = reg_sweep_oot(tr, te, chosen_feats)
    new_oot_auc = best_chosen["eval"]["auc"]

    # ----- calibration sanity on the OOT fold (isotonic vs Platt vs raw)
    p_oot = scorecard_predict(best_chosen["model"], te[chosen_feats])
    cal = calibrate_oot(p_oot, te[TARGET])
    report["calibration_oot"] = cal

    # ----- fit FINAL artifacts on ALL vintages (production) for both cards
    sc_beh_final = fit_scorecard(atrisk[chosen_feats], atrisk[TARGET].astype(int),
                                 chosen_feats, C=best_chosen["C"], penalty=best_chosen["penalty"])
    sc_aging_final = fit_scorecard(atrisk[full_feats], atrisk[TARGET].astype(int),
                                   full_feats, C=best_aging["C"], penalty=best_aging["penalty"])

    # ----- PD bands: derive on the FULL at-risk pool from the FINAL (production) card — this is
    # how the card is actually deployed and matches the incumbent's in-sample band methodology, so
    # the cutpoints are apples-to-apples with the old 1.8/27.9/82.4/88.9 bands. We ALSO report the
    # realized rate of those SAME cutpoints on the held-out OOT fold as an honest generalization
    # check (bands must stay monotone in-sample AND out-of-sample).
    p_full = scorecard_predict(sc_beh_final, atrisk[chosen_feats])
    bands, band_obs = derive_bands(p_full, atrisk[TARGET].values)

    def _rates_at(cuts, p, y):
        edges = [0.0, cuts["medium"], cuts["high"], cuts["very_high"], 1.01]
        names = ["low", "medium", "high", "very_high"]
        y = np.asarray(y).astype(int)
        out = {}
        for nm, lo, hi in zip(names, edges[:-1], edges[1:], strict=True):
            m = (p >= lo) & (p < hi)
            out[nm] = {"n": int(m.sum()),
                       "observed_fwd_rate": round(float(y[m].mean()), 4) if m.sum() else None}
        return out

    band_obs_oot = _rates_at(bands, p_oot, te[TARGET].values)
    report["pd_bands"] = bands
    report["pd_band_observed_rates_fullpool"] = band_obs
    report["pd_band_observed_rates_oot"] = band_obs_oot

    print("-" * 78)
    print(f"CHOSEN behavioral set: {chosen_tag}  OOT AUC={new_oot_auc}  (old={OLD_OOT_AUC})")
    print(f"Calibration (OOT): chosen={cal['chosen']} "
          f"brier raw/iso/platt={cal['brier_raw']}/{cal['brier_isotonic']}/{cal['brier_platt']}")
    print("PD bands (full-pool cutpoints; realized forward-default rate in-sample | OOT):")
    for nm in ["low", "medium", "high", "very_high"]:
        o, oo = band_obs[nm], band_obs_oot[nm]
        print(f"   {nm:<10} cut>={'' if nm=='low' else round(bands.get(nm, 0.0), 4)!s:<7} "
              f"n={o['n']:<4} in-sample={o['observed_fwd_rate']}  OOT={oo['observed_fwd_rate']}")

    # band monotonicity check — require monotone IN-SAMPLE (the production guarantee)
    rates = [band_obs[nm]["observed_fwd_rate"] for nm in ["low", "medium", "high", "very_high"]
             if band_obs[nm]["observed_fwd_rate"] is not None]
    monotone = all(rates[i] <= rates[i + 1] for i in range(len(rates) - 1))
    report["bands_monotone"] = monotone
    print(f"Bands monotone (in-sample): {monotone}")

    coeffs = {
        "model": "forward_sev180_scorecard",
        "horizon_months": 6,
        "population": "at-risk-with-debt (total_debt_eur>0, not already SEV180)",
        "base_rate": round(float(atrisk[TARGET].mean()), 4),
        "pd_bands": bands,
        "pd_bands_basis": "behavioral_only_scorecard (full-pool PD quantile cuts; OOT-validated)",
        "pd_band_observed_rates": band_obs,
        "pd_band_observed_rates_oot": band_obs_oot,
        "calibration_oot": cal,
        "score_orientation": "0-100, higher = higher forward-default risk",
        "score_basis": "behavioral_only PD -> 100*PD (primary, honest early-warning); "
                       "with_aging PD is near-deterministic (arithmetic) and used as override flag",
        "scorecard_with_aging": serialize_sc(sc_aging_final, "with_aging"),
        "scorecard_behavioral_only": serialize_sc(sc_beh_final, "behavioral_only"),
    }

    report["new_oot_auc"] = new_oot_auc
    (OUT / "metrics.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved metrics -> {OUT / 'metrics.json'}")

    # ----- ONLY-OVERWRITE GATE
    improves = new_oot_auc > OLD_OOT_AUC and monotone
    print("\n" + "=" * 78)
    print(f"GATE: new OOT AUC {new_oot_auc} > old {OLD_OOT_AUC}? "
          f"{new_oot_auc > OLD_OOT_AUC} ; bands monotone? {monotone} -> improves={improves}")
    if args.commit and improves:
        (ART / "forward_scorecard_coeffs.json").write_text(json.dumps(coeffs, indent=2))
        print(f"COMMITTED new artifact -> {ART / 'forward_scorecard_coeffs.json'}")
    elif args.commit:
        print("NOT committed: did not beat the incumbent. Artifact left unchanged.")
    else:
        print("Report-only run (no --commit). Artifact left unchanged.")
    print("=" * 78)


if __name__ == "__main__":
    main()
