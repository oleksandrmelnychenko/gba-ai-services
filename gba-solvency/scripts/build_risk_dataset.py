"""Build the leakage-safe supervised modeling dataset (solvency v3) + sanity report.

Read-only against ConcordDb_V5. Materializes the dataset over ALL role-1 buyers as-of
FEATURE_DATE with the SEV180 label as-of LABEL_DATE (3-month gap = no leakage), saves a
parquet + 20-row CSV preview, and prints the numeric deliverable report.

Usage:
    .venv/bin/python scripts/build_risk_dataset.py
    .venv/bin/python scripts/build_risk_dataset.py --feature-date 2026-03-25 --label-date 2026-06-25
"""
from __future__ import annotations

import argparse
from pathlib import Path

from sklearn.metrics import roc_auc_score

from app.risk.dataset import FEATURE_COLUMNS, build_dataset

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
PARQUET_PATH = DATA_DIR / "risk_dataset_v3.parquet"
CSV_PREVIEW_PATH = DATA_DIR / "risk_dataset_v3_preview.csv"

DEFAULT_FEATURE_DATE = "2026-03-25"
DEFAULT_LABEL_DATE = "2026-06-25"

SANITY_CLIENTS = {
    411780: "ТРАМП ОЙЛ",
    411801: "АБРАМЧЕНКО О.Я.",
    416221: "(416221)",
    426447: "МАГТРАНС",
}


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--feature-date", default=DEFAULT_FEATURE_DATE)
    ap.add_argument("--label-date", default=DEFAULT_LABEL_DATE)
    ap.add_argument("--window-months", type=int, default=12)
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    _hr("BUILD")
    print(f"FEATURE_DATE = {args.feature_date}   LABEL_DATE = {args.label_date}   "
          f"window_months = {args.window_months}")
    df = build_dataset(args.feature_date, args.label_date, args.window_months)

    df.to_parquet(PARQUET_PATH, index=False)
    df.head(20).to_csv(CSV_PREVIEW_PATH, index=False)
    print(f"saved parquet  -> {PARQUET_PATH}")
    print(f"saved preview  -> {CSV_PREVIEW_PATH}")

    # -------------------------------------------------------------- dataset shape
    _hr("DATASET SHAPE")
    print(f"rows (buyers)   : {len(df)}")
    print(f"feature columns : {len(FEATURE_COLUMNS)}")
    print(f"total columns   : {df.shape[1]} (client_id + {len(FEATURE_COLUMNS)} feat + 2 label)")

    # -------------------------------------------------------------- label balance
    _hr("LABEL BALANCE")
    pos = int(df["label_sev180"].sum())
    n = len(df)
    print(f"(i) cross-sectional label_sev180 positives : {pos} / {n} = {100 * pos / n:.2f}%")

    at_risk = df[df["already_default_at_feature_date"] == 0]
    new_def = int(at_risk["label_sev180"].sum())
    n_risk = len(at_risk)
    print(f"(ii) FORWARD (at-risk = NOT default at FD)  : {n_risk} at-risk buyers")
    print(f"     new defaults FD->LD (become SEV180=1)  : {new_def} / {n_risk} = "
          f"{100 * new_def / n_risk:.2f}%  <-- this is the modeled target")
    already = int(df["already_default_at_feature_date"].sum())
    print(f"     (already default at FD, excluded)      : {already}")

    # -------------------------------------------------------------- per-feature coverage + AUC
    _hr("PER-FEATURE COVERAGE + UNIVARIATE SEPARATION (directed roc_auc, NaN->0)")
    print("  xsec_auc = vs cross-sectional label_sev180 (all 3006 buyers)")
    print("  fwd_auc  = vs label_sev180 on the at-risk subset (the modeled forward target);")
    print("             this is the leak-resistant one — already-defaulted buyers excluded.")

    def _auc(y_arr, x_arr) -> float:
        if len(set(x_arr.tolist())) <= 1:
            return float("nan")
        try:
            a = roc_auc_score(y_arr, x_arr)
        except ValueError:
            return float("nan")
        return max(a, 1.0 - a)  # directed separation: flip wrong-way features

    y_all = df["label_sev180"].to_numpy()
    y_fwd = at_risk["label_sev180"].to_numpy()
    rows: list[tuple[str, float, float, float]] = []
    for c in FEATURE_COLUMNS:
        coverage = 100.0 * (df[c].notna().mean())
        x_all = df[c].fillna(0.0).to_numpy(dtype=float)
        x_fwd = at_risk[c].fillna(0.0).to_numpy(dtype=float)
        rows.append((c, coverage, _auc(y_all, x_all), _auc(y_fwd, x_fwd)))

    rows.sort(key=lambda r: (r[3] if r[3] == r[3] else -1.0), reverse=True)  # by fwd_auc, NaN last
    print(f"\n{'feature':<26}{'coverage%':>11}{'xsec_auc':>10}{'fwd_auc':>10}")
    print("-" * 57)
    for name, cov, auc_all, auc_fwd in rows:
        sa = f"{auc_all:.4f}" if auc_all == auc_all else "  n/a "
        sf = f"{auc_fwd:.4f}" if auc_fwd == auc_fwd else "  n/a "
        print(f"{name:<26}{cov:>10.1f}%{sa:>10}{sf:>10}")

    # -------------------------------------------------------------- sanity rows
    _hr("SANITY (known overdue clients)")
    key_cols = [
        "label_sev180", "already_default_at_feature_date", "overdue_eur_180plus",
        "overdue_eur_91_180", "total_debt_eur", "pct_debt_180plus", "max_overdue_days",
        "n_open_debt_lines",
    ]
    for cid, name in SANITY_CLIENTS.items():
        sub = df[df["client_id"] == cid]
        if sub.empty:
            print(f"{cid} {name}: NOT in buyer universe (no role-1 buyer row)")
            continue
        r = sub.iloc[0]
        vals = "  ".join(
            f"{k}={r[k]:.1f}" if isinstance(r[k], float) else f"{k}={r[k]}"
            for k in key_cols
        )
        print(f"{cid} {name}:\n    {vals}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
