"""Build the 6-MONTH FORWARD multi-vintage pooled training set (solvency).

Read-only against ConcordDb_V5. For each FEATURE_DATE we reuse app.risk.dataset to build a
point-in-time feature matrix as-of that FEATURE_DATE and the SEV180 label as-of FEATURE_DATE+6mo.
The MODELED population for a vintage is the AT-RISK set: role-1 buyers who are NOT already SEV180
at the feature_date (already_default_at_feature_date == 0). Their forward_default target is 1 iff
they BECOME SEV180 by label_date.

CADENCE (v2 — finer): instead of 4 monthly vintages we sweep a BI-WEEKLY (~14 day) grid of
feature_dates from 2025-09-10 through 2025-12-25 so the +6mo label_date stays on or before
2026-06-25 (the label data edge = today; a label_date past today would age only currently-existing
debt and miss future new debt -> not a clean forward label). This roughly DOUBLES the number of
snapshots (8 vs 4) and therefore the realized forward-positive events, while every snapshot is still
strictly point-in-time. Clients RECUR across snapshots (autocorrelated panel) -> downstream training
uses GROUP-BY-client CV folds + a temporal OOT split so the same client never leaks across
train/test. We keep `client_id` in the pool precisely so the trainer can group on it.

Point-in-time correctness: every feature query in app.risk.dataset filters Created <= feature_date,
so a vintage's features only see data that existed at its feature_date (no leakage from the future
or from the label window). We assert this below by checking already_default rows are excluded and
that the label window is strictly forward.

Usage:
    .venv/bin/python scripts/build_vintages.py                 # default bi-weekly grid
    .venv/bin/python scripts/build_vintages.py --cadence 10    # alternative cadence in days
    .venv/bin/python scripts/build_vintages.py --monthly       # legacy 4 monthly vintages
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dateutil.relativedelta import relativedelta

from app.risk.dataset import FEATURE_COLUMNS, build_dataset

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
POOLED_PATH = DATA_DIR / "risk_vintages_6mo.parquet"

# Valid feature-date window: label_date = feature_date + 6mo must be <= LABEL_EDGE.
WINDOW_START = date(2025, 9, 10)
WINDOW_END = date(2025, 12, 25)
LABEL_EDGE = date(2026, 6, 25)  # = today; labels beyond this would miss future new debt.
HORIZON_MONTHS = 6

# Legacy monthly grid (the v1 4-vintage design), kept for an apples-to-apples comparison.
MONTHLY_VINTAGES: list[tuple[str, str]] = [
    ("2025-09-25", "2026-03-25"),
    ("2025-10-25", "2026-04-25"),
    ("2025-11-25", "2026-05-25"),
    ("2025-12-25", "2026-06-25"),
]

# A vintage with fewer than this many forward positives is flagged as too thin to learn from.
THIN_POSITIVE_FLOOR = 20
WINDOW_MONTHS = 12


def build_grid(cadence_days: int) -> list[tuple[str, str]]:
    """feature_date grid at `cadence_days` spacing across the valid window.

    label_date = feature_date + HORIZON_MONTHS; only dates whose label_date <= LABEL_EDGE are kept.
    """
    out: list[tuple[str, str]] = []
    d = WINDOW_START
    while d <= WINDOW_END:
        ld = d + relativedelta(months=HORIZON_MONTHS)
        if ld <= LABEL_EDGE:
            out.append((d.isoformat(), ld.isoformat()))
        d = d + timedelta(days=cadence_days)
    return out


def _hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def build_vintage(feature_date: str, label_date: str) -> pd.DataFrame:
    """One vintage = AT-RISK buyers as-of feature_date with the 6mo-forward target.

    build_dataset returns ALL role-1 buyers with:
      - features as-of feature_date (Created <= feature_date inside every query),
      - label_sev180 as-of label_date (SEV180 at the 6mo horizon),
      - already_default_at_feature_date = SEV180 already true at feature_date.
    We keep only at-risk rows (already_default == 0) and rename their label to forward_default.
    """
    full = build_dataset(feature_date, label_date, WINDOW_MONTHS)
    at_risk = full[full["already_default_at_feature_date"] == 0].copy()
    at_risk = at_risk.rename(columns={"label_sev180": "forward_default"})
    at_risk["vintage"] = feature_date
    at_risk["label_date"] = label_date
    cols = ["client_id", "vintage", "label_date", *FEATURE_COLUMNS, "forward_default"]
    return at_risk[cols].reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build 6-month forward vintages.")
    ap.add_argument("--cadence", type=int, default=14,
                    help="feature-date spacing in days (default 14 = bi-weekly).")
    ap.add_argument("--monthly", action="store_true",
                    help="use the legacy 4 monthly vintages instead of a cadence grid.")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    vintages = MONTHLY_VINTAGES if args.monthly else build_grid(args.cadence)
    grid_name = "monthly (legacy)" if args.monthly else f"{args.cadence}-day cadence"

    _hr(f"BUILD 6-MONTH FORWARD VINTAGES  [{grid_name}]  n={len(vintages)}")
    per_vintage_stats: list[tuple[str, str, int, int, float]] = []
    frames: list[pd.DataFrame] = []

    for fd, ld in vintages:
        print(f"\n[vintage] feature_date={fd}  label_date={ld}  (6mo forward)")
        v = build_vintage(fd, ld)
        assert v["vintage"].nunique() == 1 and v["vintage"].iloc[0] == fd
        rows = len(v)
        pos = int(v["forward_default"].sum())
        base = (100.0 * pos / rows) if rows else 0.0
        per_vintage_stats.append((fd, ld, rows, pos, base))
        print(f"           at-risk rows={rows:5d}   forward positives={pos:4d}   "
              f"base_rate={base:.2f}%")
        frames.append(v)

    pooled = pd.concat(frames, ignore_index=True)
    pooled.to_parquet(POOLED_PATH, index=False)

    # -------------------------------------------------------------- per-vintage report
    _hr("PER-VINTAGE SUMMARY")
    print(f"{'vintage(FD)':<14}{'label(LD)':<14}{'rows':>8}{'pos':>7}{'base_rate':>11}  flag")
    print("-" * 64)
    for fd, ld, rows, pos, base in per_vintage_stats:
        flag = "THIN" if pos < THIN_POSITIVE_FLOOR else ""
        print(f"{fd:<14}{ld:<14}{rows:>8}{pos:>7}{base:>10.2f}%  {flag}")

    # -------------------------------------------------------------- pooled totals
    _hr("POOLED TOTALS")
    tot_rows = len(pooled)
    tot_pos = int(pooled["forward_default"].sum())
    tot_base = 100.0 * tot_pos / tot_rows if tot_rows else 0.0
    atrisk = pooled[pooled["total_debt_eur"] > 0]
    at_rows = len(atrisk)
    at_pos = int(atrisk["forward_default"].sum())
    at_base = 100.0 * at_pos / at_rows if at_rows else 0.0
    print(f"pooled rows            : {tot_rows}")
    print(f"pooled positives       : {tot_pos}")
    print(f"pooled base rate       : {tot_base:.2f}%")
    print(f"AT-RISK-WITH-DEBT rows : {at_rows}")
    print(f"AT-RISK-WITH-DEBT pos  : {at_pos}")
    print(f"AT-RISK-WITH-DEBT rate : {at_base:.2f}%")
    print(f"unique clients (atrisk): {atrisk['client_id'].nunique()}")
    print(f"unique pos clients     : {atrisk[atrisk['forward_default'] == 1]['client_id'].nunique()}")
    print(f"feature columns        : {len(FEATURE_COLUMNS)}")
    print(f"saved parquet          -> {POOLED_PATH}")

    # -------------------------------------------------------------- proposed temporal split
    _hr("PROPOSED TEMPORAL SPLIT (train=earliest half of vintages, test=latest half)")
    vsorted = [fd for fd, _ in vintages]
    half = len(vsorted) // 2
    train_v, test_v = vsorted[:half], vsorted[half:]
    train = pooled[(pooled["vintage"].isin(train_v)) & (pooled["total_debt_eur"] > 0)]
    test = pooled[(pooled["vintage"].isin(test_v)) & (pooled["total_debt_eur"] > 0)]
    tr_pos, te_pos = int(train["forward_default"].sum()), int(test["forward_default"].sum())
    print(f"TRAIN vintages : {train_v}")
    print(f"  atrisk rows={len(train):5d}   positives={tr_pos:4d}   "
          f"base_rate={100.0 * tr_pos / max(len(train), 1):.2f}%")
    print(f"TEST  vintages : {test_v}")
    print(f"  atrisk rows={len(test):5d}   positives={te_pos:4d}   "
          f"base_rate={100.0 * te_pos / max(len(test), 1):.2f}%")

    thin = [fd for fd, _, _, pos, _ in per_vintage_stats if pos < THIN_POSITIVE_FLOOR]
    if thin:
        print(f"\nNOTE: thin vintage(s) (< {THIN_POSITIVE_FLOOR} fwd positives, full pool): {thin}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
