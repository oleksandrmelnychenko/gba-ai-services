"""Build the vintaged NBA propensity training set and emit the calibration report.

Replays the 4 generators' as-of signal SQL across monthly snapshots T in 2025-08..2026-04
(manager filter dropped), joins H=60d leak-safe outcome labels, pools to one-row-per-instance
with a vintage column, writes data/nba_dataset.parquet, and prints:
  * total + per-type rows and per-type H60 base rates;
  * feature list + non-null/non-zero coverage;
  * THE SMOKING GUN: roc_auc_score(label, old_priority) and roc_auc_score(label, each feature),
    ranked, to test whether the current expert-guessed priority predicts the outcome at all.

Run: cd /root/projects/gba-nba && DB_PASSWORD='...' .venv/bin/python -m scripts.build_nba_dataset
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from sklearn.metrics import roc_auc_score

from app.ml.dataset import build_dataset

logging.getLogger("httpx").setLevel(logging.WARNING)

SNAPSHOTS = [
    "2025-08-01", "2025-09-01", "2025-10-01", "2025-11-01", "2025-12-01",
    "2026-01-01", "2026-02-01", "2026-03-01", "2026-04-01",
]
OUT = Path(__file__).resolve().parents[1] / "data" / "nba_dataset.parquet"

FEATURE_COLS = [
    "monetary", "recency_days", "order_count",
    "is_reorder_due", "is_debt_followup", "is_churn_winback", "is_cross_sell",
    "sig_overdue_amount", "sig_days_past_terms", "sig_max_overdue_days", "sig_debt_lines",
    "sig_elapsed_days", "sig_cycle_days", "sig_overdue_ratio", "sig_n_orders",
    "sig_drop_ratio", "sig_silence_days", "sig_recent_orders", "sig_prior_orders",
    "sig_top_score", "sig_reco_candidates",
]


def safe_auc(y, x) -> float | None:
    y = np.asarray(y)
    x = np.asarray(x, dtype=float)
    m = ~np.isnan(x)
    if m.sum() < 2 or len(set(y[m].tolist())) < 2:
        return None
    try:
        return roc_auc_score(y[m], x[m])
    except Exception:
        return None


def main() -> None:
    df = build_dataset(SNAPSHOTS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)

    print("\n" + "=" * 78)
    print(f"DATASET  rows={len(df)}  ->  {OUT}")
    print("=" * 78)

    print("\n-- per-type rows & H60 base rate --")
    g = df.groupby("task_type")["label"].agg(["size", "sum", "mean"]).sort_index()
    for ttype, r in g.iterrows():
        print(f"  {ttype:16s}  n={int(r['size']):6d}  pos={int(r['sum']):5d}  base_rate={r['mean']:.1%}")
    print(
        f"  {'POOLED':16s}  n={len(df):6d}  "
        f"pos={int(df['label'].sum()):5d}  base_rate={df['label'].mean():.1%}"
    )

    print("\n-- per-vintage rows --")
    for t, sub in df.groupby("vintage"):
        print(f"  {t}  n={len(sub):5d}  pos={int(sub['label'].sum()):4d}  base={sub['label'].mean():.1%}")

    print("\n-- feature coverage (non-null & non-zero share over pooled rows) --")
    for c in FEATURE_COLS:
        col = df[c]
        nn = col.notna().mean()
        nz = (col.fillna(0) != 0).mean()
        print(f"  {c:22s}  non_null={nn:6.1%}  non_zero={nz:6.1%}")

    # --------------------------------------------------------------------------
    # SMOKING GUN: does the expert-guessed priority predict the outcome?
    # --------------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("SMOKING GUN  —  AUC of old_priority vs label (1.0=perfect, 0.5=coin flip)")
    print("=" * 78)

    print("\n  POOLED across all task types:")
    print(f"    old_priority           AUC = {fmt(safe_auc(df['label'], df['old_priority']))}")

    print("\n  WITHIN each task type (priority is only meant to rank inside a type's inbox):")
    for ttype, sub in df.groupby("task_type"):
        print(f"    {ttype:16s}  n={len(sub):5d}  base={sub['label'].mean():5.1%}  "
              f"old_priority AUC = {fmt(safe_auc(sub['label'], sub['old_priority']))}")

    print("\n" + "=" * 78)
    print("FEATURE AUCs (pooled) — single-feature ranking power vs the outcome")
    print("=" * 78)
    scored = []
    for c in FEATURE_COLS + ["old_priority"]:
        a = safe_auc(df["label"], df[c])
        if a is not None:
            scored.append((c, a, abs(a - 0.5)))
    scored.sort(key=lambda x: x[2], reverse=True)
    for c, a, lift in scored:
        print(f"  {c:22s}  AUC={a:5.3f}  |lift|={lift:5.3f}")

    print("\n" + "=" * 78)
    print("FEATURE AUCs WITHIN each task type (the rank that actually matters)")
    print("=" * 78)
    for ttype, sub in df.groupby("task_type"):
        rows = []
        for c in FEATURE_COLS + ["old_priority"]:
            if sub[c].nunique(dropna=True) < 2:
                continue
            a = safe_auc(sub["label"], sub[c])
            if a is not None:
                rows.append((c, a, abs(a - 0.5)))
        rows.sort(key=lambda x: x[2], reverse=True)
        print(f"\n  [{ttype}]  n={len(sub)}  base={sub['label'].mean():.1%}")
        for c, a, lift in rows[:8]:
            print(f"    {c:22s}  AUC={a:5.3f}  |lift|={lift:5.3f}")


def fmt(a: float | None) -> str:
    return "n/a (degenerate)" if a is None else f"{a:.3f}"


if __name__ == "__main__":
    main()
