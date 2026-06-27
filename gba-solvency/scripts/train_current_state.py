"""Train the CURRENT-STATE solvency risk model (target = label_sev180, cross-sectional).

Two models:
  (1) WOE + L2-regularized logistic scorecard  (production, explainable)
  (2) HistGradientBoosting GBM                 (challenger)

CRITICAL leakage rule: overdue_eur_180plus and pct_debt_180plus are TAUTOLOGICAL with the
SEV180 label (the label IS >=EUR100 of 180+ overdue debt). The PRIMARY model EXCLUDES those
two so the score predicts current risk from leading/correlated signals. We also fit a WITH
variant purely to report the AUC delta and prove the model is not just the defining rule.

WOE binning is done MANUALLY (optbinning 0.20 is incompatible with sklearn 1.9), using a
monotonic, zero-aware, quantile-seeded binner that is trivially reproducible by the pure
scorer (just split thresholds + per-bin WoE).

Outputs (under app/risk/artifacts/ and data/):
  - scorecard_coefficients.json   WOE bins + logistic coefs + PD->score calibration + bands
  - gbm_model.joblib              fitted GBM (challenger)
  - cv_report.json                honest CV metrics, ablation, benchmark vs old score
  - calibration_current.png       reliability curves
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import calibration_curve
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.model_selection import StratifiedKFold

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "app" / "risk" / "artifacts"
ART.mkdir(parents=True, exist_ok=True)
DATA = ROOT / "data"

RANDOM_STATE = 42
N_FOLDS = 5

TAUTOLOGICAL = ["overdue_eur_180plus", "pct_debt_180plus"]

PRIMARY_FEATURES = [
    "overdue_eur_91_180",
    "overdue_eur_61_90",
    "overdue_eur_31_60",
    "overdue_eur_1_30",
    "current_debt_eur",
    "total_debt_eur",
    "max_overdue_days",
    "n_open_debt_lines",
    "debt_growth_3mo",
    "months_with_debt_last12",
    "new_debt_eur_3mo",
    "credit_limit_eur",
    "limit_utilization",
    "grace_days",
    "has_credit_control",
    "turnover_eur_12mo",
    "order_count_12mo",
    "recency_days",
    "tenure_months",
    "return_rate_12mo",
]

# "risk_up": higher feature value => higher SEV180 risk (enforce WOE descending in risk dir).
# "risk_down": higher feature value => lower risk.
RISK_DIRECTION = {
    "overdue_eur_91_180": "up",
    "overdue_eur_61_90": "up",
    "overdue_eur_31_60": "up",
    "overdue_eur_1_30": "up",
    "current_debt_eur": "up",
    "total_debt_eur": "up",
    "max_overdue_days": "up",
    "n_open_debt_lines": "up",
    "debt_growth_3mo": "up",
    "months_with_debt_last12": "up",
    "new_debt_eur_3mo": "up",
    "credit_limit_eur": "down",
    "limit_utilization": "up",
    "grace_days": "up",
    "has_credit_control": "up",
    "turnover_eur_12mo": "down",
    "order_count_12mo": "down",
    "recency_days": "up",
    "tenure_months": "down",
    "return_rate_12mo": "up",
}

WOE_CLIP = 4.0  # clip extreme WoE for stability


# ---------------------------------------------------------------------------------------------
# Manual monotonic WOE binning
# ---------------------------------------------------------------------------------------------
def _initial_edges(x: np.ndarray, max_bins: int) -> list[float]:
    """Quantile-seeded interior split thresholds, with a dedicated zero-vs-positive split when
    the feature is heavily zero-inflated (so the 'has any signal' bin is isolated cleanly)."""
    x = x[~np.isnan(x)]
    uniq = np.unique(x)
    if len(uniq) <= 1:
        return []
    edges: list[float] = []
    zero_frac = float(np.mean(x == 0))
    pos = x[x > 0]
    if zero_frac >= 0.30 and len(pos) > 0:
        # isolate zeros: first split at 0 (bin0 = {x<=0})
        edges.append(0.0)
        qs = np.quantile(pos, np.linspace(0, 1, max_bins)[1:-1]) if len(pos) > 5 else []
        edges.extend([float(q) for q in qs])
    else:
        qs = np.quantile(x, np.linspace(0, 1, max_bins + 1)[1:-1])
        edges.extend([float(q) for q in qs])
    edges = sorted(set(round(e, 6) for e in edges))
    return edges


def _bin_index(x: np.ndarray, edges: list[float]) -> np.ndarray:
    """Bin index for each x: bin k = (edges[k-1], edges[k]]; right-closed; len = len(edges)+1."""
    idx = np.searchsorted(np.asarray(edges), x, side="left")  # x<=edge -> that bin
    return idx.astype(int)


def _woe_table(idx: np.ndarray, y: np.ndarray, n_bins: int) -> tuple[np.ndarray, np.ndarray]:
    """Return (woe per bin, event_rate per bin) with Laplace smoothing."""
    tot_e = max(y.sum(), 1)
    tot_n = max((1 - y).sum(), 1)
    woe = np.zeros(n_bins)
    rate = np.zeros(n_bins)
    for k in range(n_bins):
        m = idx == k
        e = y[m].sum()
        n = (1 - y[m]).sum()
        cnt = m.sum()
        rate[k] = e / cnt if cnt else 0.0
        dist_e = (e + 0.5) / (tot_e + 0.5 * n_bins)
        dist_n = (n + 0.5) / (tot_n + 0.5 * n_bins)
        woe[k] = float(np.clip(np.log(dist_e / dist_n), -WOE_CLIP, WOE_CLIP))
    return woe, rate


def fit_woe(x: np.ndarray, y: np.ndarray, direction: str,
            max_bins: int = 5, min_bin_frac: float = 0.03) -> dict:
    """Fit a monotonic WOE binning. Greedily merge adjacent bins until WoE is monotone in the
    feature's risk direction AND every bin meets min size. Returns {splits, woe}."""
    edges = _initial_edges(x, max_bins)
    if not edges:
        # constant / binary-ish: single bin
        woe, _ = _woe_table(np.zeros(len(x), int), y, 1)
        return {"splits": [], "woe": [float(woe[0])]}
    n = len(x)
    min_cnt = max(int(min_bin_frac * n), 20)

    def recompute(edges_):
        idx = _bin_index(x, edges_)
        nb = len(edges_) + 1
        # reindex to contiguous present bins
        present = sorted(set(idx.tolist()))
        remap = {b: i for i, b in enumerate(present)}
        idx2 = np.array([remap[b] for b in idx])
        woe, rate = _woe_table(idx2, y, len(present))
        counts = np.array([(idx2 == k).sum() for k in range(len(present))])
        return idx2, woe, rate, counts, present

    # iterate: merge smallest-violating boundary
    for _ in range(50):
        idx2, woe, rate, counts, present = recompute(edges)
        if len(present) <= 1:
            break
        # enforce monotonic WoE in risk direction: risk 'up' => event_rate should be
        # non-decreasing with feature, i.e. WoE non-increasing? WoE = log(distE/distN); higher
        # event rate => higher WoE. So 'up' => WoE non-decreasing across bins.
        sign = 1 if direction == "up" else -1
        # find violations
        merged = False
        # 1) min-size merges first
        for k in range(len(counts)):
            if counts[k] < min_cnt and len(edges) > 0:
                # merge with neighbor by removing an edge adjacent to this present-bin
                e_idx = min(max(k - 1, 0), len(edges) - 1)
                del edges[e_idx]
                merged = True
                break
        if merged:
            continue
        # 2) monotonicity merges
        seq = woe * sign
        for k in range(len(seq) - 1):
            if seq[k] > seq[k + 1] + 1e-9:
                e_idx = min(k, len(edges) - 1)
                if edges:
                    del edges[e_idx]
                merged = True
                break
        if not merged:
            break

    idx2, woe, rate, counts, present = recompute(edges)
    return {"splits": [float(e) for e in edges], "woe": [float(w) for w in woe]}


def woe_value(v: float, splits: list[float], woe: list[float]) -> float:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return woe[0]
    k = 0
    for i, s in enumerate(splits):
        if v <= s:
            k = i
            break
    else:
        k = len(splits)
    if k >= len(woe):
        k = len(woe) - 1
    return woe[k]


def woe_matrix(X: pd.DataFrame, binmap: dict, features: list[str]) -> pd.DataFrame:
    out = {}
    for f in features:
        sp = binmap[f]["splits"]
        wo = binmap[f]["woe"]
        xv = X[f].values
        col = np.empty(len(xv))
        for i, v in enumerate(xv):
            col[i] = woe_value(float(v), sp, wo)
        out[f] = col
    return pd.DataFrame(out, index=X.index)


def fit_binmap(X: pd.DataFrame, y: np.ndarray, features: list[str]) -> dict:
    return {f: fit_woe(X[f].values.astype(float), y, RISK_DIRECTION.get(f, "up")) for f in features}


# ---------------------------------------------------------------------------------------------
def ks_stat(y: np.ndarray, p: np.ndarray) -> float:
    order = np.argsort(p)
    y = y[order]
    pos = np.cumsum(y) / max(y.sum(), 1)
    neg = np.cumsum(1 - y) / max((1 - y).sum(), 1)
    return float(np.max(np.abs(pos - neg)))


def cv_eval_woe(X: pd.DataFrame, y: np.ndarray, features: list[str]) -> dict:
    skf = StratifiedKFold(n_splits=N_FOLDS, shuffle=True, random_state=RANDOM_STATE)
    aucs, kss, briers = [], [], []
    oof = np.zeros(len(y))
    for tr, te in skf.split(X, y):
        Xtr, Xte, ytr = X.iloc[tr], X.iloc[te], y[tr]
        bm = fit_binmap(Xtr, ytr, features)
        Wtr, Wte = woe_matrix(Xtr, bm, features), woe_matrix(Xte, bm, features)
        clf = LogisticRegression(C=0.5, penalty="l2", class_weight="balanced",
                                 max_iter=2000, solver="lbfgs")
        clf.fit(Wtr, ytr)
        p = clf.predict_proba(Wte)[:, 1]
        oof[te] = p
        aucs.append(roc_auc_score(y[te], p))
        kss.append(ks_stat(y[te], p))
        briers.append(brier_score_loss(y[te], p))
    return _summary(aucs, kss, briers, oof, y)


def cv_eval_gbm(X: pd.DataFrame, y: np.ndarray, features: list[str]) -> dict:
    skf = StratifiedKFold(n_splits=N_FOLDS, shuffle=True, random_state=RANDOM_STATE)
    aucs, kss, briers = [], [], []
    oof = np.zeros(len(y))
    for tr, te in skf.split(X, y):
        clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_depth=3,
                                             l2_regularization=1.0, class_weight="balanced",
                                             random_state=RANDOM_STATE)
        clf.fit(X.iloc[tr][features], y[tr])
        p = clf.predict_proba(X.iloc[te][features])[:, 1]
        oof[te] = p
        aucs.append(roc_auc_score(y[te], p))
        kss.append(ks_stat(y[te], p))
        briers.append(brier_score_loss(y[te], p))
    return _summary(aucs, kss, briers, oof, y)


def _summary(aucs, kss, briers, oof, y) -> dict:
    return {
        "auc_mean": float(np.mean(aucs)), "auc_std": float(np.std(aucs)),
        "auc_folds": [round(a, 4) for a in aucs],
        "ks_mean": float(np.mean(kss)), "gini_mean": float(2 * np.mean(aucs) - 1),
        "brier_mean": float(np.mean(briers)), "oof_auc": float(roc_auc_score(y, oof)),
        "oof": oof,
    }


# ---------------------------------------------------------------------------------------------
def pd_to_score(pd_arr: np.ndarray, lo=400.0, hi=800.0, pdo=20.0, offset=600.0) -> np.ndarray:
    pd_arr = np.clip(pd_arr, 1e-6, 1 - 1e-6)
    odds = (1 - pd_arr) / pd_arr
    factor = pdo / np.log(2)
    raw = offset + factor * np.log(odds)
    return np.clip((raw - lo) / (hi - lo) * 100, 0, 100)


def band_from_pd(p: float) -> str:
    if p < 0.02:
        return "A"
    if p < 0.05:
        return "B"
    if p < 0.15:
        return "C"
    return "D"


def main() -> None:
    df = pd.read_parquet(DATA / "risk_dataset_v3.parquet")
    y = df["label_sev180"].values.astype(int)
    n_pos = int(y.sum())
    print(f"rows={len(df)} pos={n_pos} ({n_pos/len(df):.2%})")

    X_all = df[TAUTOLOGICAL + PRIMARY_FEATURES].copy()
    X_primary = df[PRIMARY_FEATURES].copy()

    # give tautological cols a risk direction for the WITH variant
    RISK_DIRECTION.setdefault("overdue_eur_180plus", "up")
    RISK_DIRECTION.setdefault("pct_debt_180plus", "up")

    report: dict = {
        "dataset": {"rows": len(df), "positives": n_pos, "prevalence": float(y.mean())},
        "primary_features": PRIMARY_FEATURES,
        "tautological_excluded": TAUTOLOGICAL,
    }

    print("\n== WOE scorecard CV ==")
    woe_primary = cv_eval_woe(X_primary, y, PRIMARY_FEATURES)
    woe_with = cv_eval_woe(X_all, y, TAUTOLOGICAL + PRIMARY_FEATURES)
    print(f"  WITHOUT taut: AUC={woe_primary['auc_mean']:.4f} KS={woe_primary['ks_mean']:.4f} "
          f"Gini={woe_primary['gini_mean']:.4f} Brier={woe_primary['brier_mean']:.4f}")
    print(f"  WITH taut:    AUC={woe_with['auc_mean']:.4f}")

    print("\n== GBM challenger CV ==")
    gbm_primary = cv_eval_gbm(X_primary, y, PRIMARY_FEATURES)
    gbm_with = cv_eval_gbm(X_all, y, TAUTOLOGICAL + PRIMARY_FEATURES)
    print(f"  WITHOUT taut: AUC={gbm_primary['auc_mean']:.4f} KS={gbm_primary['ks_mean']:.4f} "
          f"Gini={gbm_primary['gini_mean']:.4f} Brier={gbm_primary['brier_mean']:.4f}")
    print(f"  WITH taut:    AUC={gbm_with['auc_mean']:.4f}")

    # ---- TIER ABLATION (GBM): expose mechanical coupling of debt-table features -------------
    # The label is sourced from the Debt/ClientInDebt table; debt-aging buckets and chronicity
    # features come from the SAME table and are near-mechanically coupled to it. We report AUC
    # for nested tiers so the reader sees how much skill is genuinely independent of that table.
    DEBT_AGING = ["overdue_eur_91_180", "overdue_eur_61_90", "overdue_eur_31_60",
                  "overdue_eur_1_30", "current_debt_eur", "total_debt_eur",
                  "max_overdue_days", "n_open_debt_lines"]
    DEBT_DERIVED = ["debt_growth_3mo", "months_with_debt_last12", "new_debt_eur_3mo"]
    NON_DEBT_TABLE = [f for f in PRIMARY_FEATURES if f not in DEBT_AGING + DEBT_DERIVED]
    tiers = {
        "non_debt_table_only": NON_DEBT_TABLE,
        "leading_no_aging_buckets": NON_DEBT_TABLE + DEBT_DERIVED,
        "primary_full": PRIMARY_FEATURES,
        "with_tautological": PRIMARY_FEATURES + TAUTOLOGICAL,
    }
    tier_auc = {}
    for name, feats in tiers.items():
        src = X_all if name == "with_tautological" else X_primary if name == "primary_full" else df
        e = cv_eval_gbm(df, y, feats)
        tier_auc[name] = {"n_features": len(feats), "auc_mean": round(e["auc_mean"], 4),
                          "ks_mean": round(e["ks_mean"], 4)}
    report["tier_ablation_gbm"] = {
        "note": ("Label is sourced from Debt/ClientInDebt; debt-aging & debt-derived features "
                 "share that table and are near-mechanically coupled to SEV180. non_debt_table_only "
                 "(credit terms/utilization, RFM, returns) is the genuinely independent signal."),
        "tiers": tier_auc,
        "groups": {"non_debt_table": NON_DEBT_TABLE, "debt_derived": DEBT_DERIVED,
                   "debt_aging": DEBT_AGING},
    }
    print("\n== Tier ablation (GBM AUC) ==")
    for k, v in tier_auc.items():
        print(f"  {k:28s} ({v['n_features']:2d}f): AUC={v['auc_mean']:.4f} KS={v['ks_mean']:.4f}")

    report["cv"] = {
        "woe_scorecard_primary": {k: v for k, v in woe_primary.items() if k != "oof"},
        "woe_scorecard_with_tautological": {k: v for k, v in woe_with.items() if k != "oof"},
        "gbm_primary": {k: v for k, v in gbm_primary.items() if k != "oof"},
        "gbm_with_tautological": {k: v for k, v in gbm_with.items() if k != "oof"},
        "ablation_auc_delta_woe": round(woe_with["auc_mean"] - woe_primary["auc_mean"], 4),
        "ablation_auc_delta_gbm": round(gbm_with["auc_mean"] - gbm_primary["auc_mean"], 4),
    }

    # ---- FINAL FIT (PRIMARY) -----------------------------------------------------------------
    binmap = fit_binmap(X_primary, y, PRIMARY_FEATURES)
    W = woe_matrix(X_primary, binmap, PRIMARY_FEATURES)
    final_lr = LogisticRegression(C=0.5, penalty="l2", class_weight="balanced",
                                  max_iter=2000, solver="lbfgs")
    final_lr.fit(W, y)
    lin = final_lr.decision_function(W)
    calib = LogisticRegression(C=1e6, max_iter=2000, solver="lbfgs")
    calib.fit(lin.reshape(-1, 1), y)
    pd_full = calib.predict_proba(lin.reshape(-1, 1))[:, 1]
    score_full = pd_to_score(pd_full)

    df_out = df[["client_id"]].copy()
    df_out["pd"] = pd_full
    df_out["score"] = score_full
    df_out["band"] = [band_from_pd(p) for p in pd_full]
    df_out["label"] = y
    df_out.to_parquet(DATA / "current_state_scores.parquet")

    band_stats = (
        df_out.groupby("band")
        .agg(n=("label", "size"), realized_pd=("label", "mean"),
             mean_score=("score", "mean"), min_score=("score", "min"),
             max_score=("score", "max"))
        .reset_index().to_dict(orient="records")
    )
    report["bands"] = {
        "definition": {"A": "PD<2%", "B": "2-5%", "C": "5-15%", "D": ">15%"},
        "score_mapping": "log-odds points (PDO=20), [400,800]->[0,100], higher=safer",
        "realized": band_stats,
    }

    import joblib

    gbm_final = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_depth=3,
                                              l2_regularization=1.0, class_weight="balanced",
                                              random_state=RANDOM_STATE)
    gbm_final.fit(X_primary, y)
    joblib.dump({"model": gbm_final, "features": PRIMARY_FEATURES}, ART / "gbm_model.joblib")

    scorecard = {
        "model": "woe_logistic_scorecard_current_state",
        "target": "label_sev180",
        "features": PRIMARY_FEATURES,
        "tautological_excluded": TAUTOLOGICAL,
        "risk_direction": {f: RISK_DIRECTION.get(f, "up") for f in PRIMARY_FEATURES},
        "woe_bins": binmap,
        "logistic": {
            "coef": {f: float(c) for f, c in zip(PRIMARY_FEATURES, final_lr.coef_[0])},
            "intercept": float(final_lr.intercept_[0]),
        },
        "calibration": {"a": float(calib.coef_[0][0]), "b": float(calib.intercept_[0])},
        "score_mapping": {"type": "log_odds_points", "pdo": 20.0, "anchor_offset": 600.0,
                          "score_range_lo": 400.0, "score_range_hi": 800.0},
        "bands": {"A": 0.02, "B": 0.05, "C": 0.15},
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "n_rows": len(df), "n_pos": n_pos,
    }
    (ART / "scorecard_coefficients.json").write_text(json.dumps(scorecard, indent=2))

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 6))
        for name, oof in [("WOE scorecard", woe_primary["oof"]), ("GBM", gbm_primary["oof"])]:
            ft, fp = calibration_curve(y, oof, n_bins=8, strategy="quantile")
            ax.plot(fp, ft, marker="o", label=name)
        ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="perfect")
        ax.set_xlabel("Mean predicted PD"); ax.set_ylabel("Observed fraction positive")
        ax.set_title("Current-state SEV180 calibration (OOF)")
        ax.legend(); fig.tight_layout()
        fig.savefig(ART / "calibration_current.png", dpi=110)
        report["calibration_plot"] = str(ART / "calibration_current.png")
    except Exception as e:  # noqa: BLE001
        report["calibration_plot_error"] = str(e)

    (ART / "cv_report.json").write_text(json.dumps(report, indent=2, default=float))
    print("\nartifacts written to", ART)
    print(json.dumps(report["cv"], indent=2, default=float))
    print("bands:", json.dumps(band_stats, indent=2, default=float))


if __name__ == "__main__":
    sys.exit(main())
