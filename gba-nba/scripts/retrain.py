"""Gated, idempotent end-to-end retrain of the NBA propensity model with an atomic swap.

Pipeline (reuses the existing build/train logic verbatim — no duplicated modeling):

  1. BACK UP the current production artifacts to app/ml/artifacts/backup_<ts>/.
  2. REBUILD the vintaged backfill dataset, rolling the monthly snapshot window forward so the
     newest vintage tracks "now" (default: last 9 month-starts up to the last fully-labelled
     vintage, i.e. >= H_DAYS in the past so the (T, T+H] outcome window is complete).
  3. UNION LIVE LABELS: append manager-logged terminal-task outcomes from Mongo (live ground
     truth) onto the backfill. Live rows are upweighted (sample_weight) so a handful of real
     conversions count more than the backfill proxy. TODAY there are 0 live labels, so this is a
     no-op and the set is backfill-only — but the path is wired and tested.
  4. RETRAIN the calibrated HGB/logit propensity model (app.ml.train.main) in place.
  5. VALIDATE — the gate: pooled OOT AUC must be >= --auc-floor (default 0.68) AND
     >= old_pooled_OOT_AUC - --auc-epsilon (default 0.01).
       PASS -> keep the freshly trained artifacts; print summary.
       FAIL -> RESTORE the backed-up artifacts (the service never sees a regressed model).

Because train.py writes directly into app/ml/artifacts/, the "atomic swap" is: back up first,
train in place, and on a failed gate restore the backup. A mid-run crash leaves the backup intact.

Usage:
    .venv/bin/python scripts/retrain.py                       # full as-of-today retrain + swap
    .venv/bin/python scripts/retrain.py --dry-run             # rebuild->train->validate, then
                                                              #   restore prod unconditionally
    .venv/bin/python scripts/retrain.py --auc-floor 0.70 --auc-epsilon 0.005
    .venv/bin/python scripts/retrain.py --no-live-labels      # backfill only (skip the Mongo union)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ART = ROOT / "app" / "ml" / "artifacts"
DATA = ROOT / "data"
PARQUET = DATA / "nba_dataset.parquet"

# Artifacts the live scoring head loads — backed up / restored / swapped as a set.
PROD_ARTIFACTS = ["propensity_model.joblib", "model_meta.json", "metrics.json", "MODEL_CARD.md"]

DEFAULT_AUC_FLOOR = 0.68
DEFAULT_AUC_EPSILON = 0.01
N_SNAPSHOTS = 9
H_DAYS = 60  # outcome window; a vintage is only fully labelled once it is >= H_DAYS in the past
# Live (manager-logged) rows are ground truth; weight them up relative to the backfill proxy.
LIVE_SAMPLE_WEIGHT = 5.0


def _hr(title: str) -> None:
    print("\n" + "#" * 78)
    print("# " + title)
    print("#" * 78, flush=True)


def _month_start(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _prev_month_start(d: dt.date) -> dt.date:
    first = _month_start(d)
    return _month_start(first - dt.timedelta(days=1))


def rolling_snapshots(today: dt.date, n: int = N_SNAPSHOTS, h_days: int = H_DAYS) -> list[str]:
    """The newest n month-start vintages whose (T, T+H] label window has fully closed.

    The latest usable vintage is the most recent month-start that is at least h_days before today
    (so every candidate has a complete outcome window). Then walk back n-1 more months.
    """
    latest = _month_start(today - dt.timedelta(days=h_days))
    out: list[dt.date] = []
    cur = latest
    for _ in range(n):
        out.append(cur)
        cur = _prev_month_start(cur)
    return [d.isoformat() for d in reversed(out)]


def _pooled_oot_auc(metrics_path: Path) -> float:
    """The gate metric: pooled OOT AUC of the chosen production model."""
    rep = json.loads(metrics_path.read_text())
    prod = rep.get("production_model", "hgb")
    return float(rep["oot"][prod]["auc"])


def _crosssell_oot_auc(metrics_path: Path) -> float | None:
    try:
        rep = json.loads(metrics_path.read_text())
        prod = rep.get("production_model", "hgb")
        return float(rep["oot_per_type"][prod]["cross_sell"]["auc"])
    except Exception:  # noqa: BLE001
        return None


def _model_card_consistency_errors(metrics_path: Path, card_path: Path) -> list[str]:
    """Lightweight stale-card guard: make sure the card names the current headline metrics."""
    if not metrics_path.exists():
        return [f"{metrics_path.name} missing"]
    if not card_path.exists():
        return [f"{card_path.name} missing"]

    rep = json.loads(metrics_path.read_text())
    prod = rep.get("production_model", "hgb")
    card = card_path.read_text()
    expected = {
        "row count": f"{int(rep['n_rows']):,}",
        "client count": f"{int(rep['n_clients']):,}",
        "base rate": f"{float(rep['base_rate']) * 100:.1f}%",
        "production CV AUC": f"{float(rep['cv'][prod]['auc']):.3f}",
        "production OOT AUC": f"{float(rep['oot'][prod]['auc']):.3f}",
        "old OOT AUC": f"{float(rep['benchmark']['oot']['auc_old']):.3f}",
    }
    return [f"{label} {value} not found in {card_path.name}"
            for label, value in expected.items() if value not in card]


def _backup(ts: str) -> Path:
    dst = ART / f"backup_{ts}"
    dst.mkdir(parents=True, exist_ok=True)
    for name in PROD_ARTIFACTS:
        src = ART / name
        if src.exists():
            shutil.copy2(src, dst / name)
    if PARQUET.exists():
        shutil.copy2(PARQUET, dst / PARQUET.name)
    print(f"backed up {sum((ART / n).exists() for n in PROD_ARTIFACTS)} artifacts + dataset -> {dst}")
    return dst


def _restore(backup_dir: Path) -> None:
    for name in PROD_ARTIFACTS:
        src = backup_dir / name
        if src.exists():
            shutil.copy2(src, ART / name)
    src_pq = backup_dir / PARQUET.name
    if src_pq.exists():
        shutil.copy2(src_pq, PARQUET)
    print(f"RESTORED previous artifacts + dataset from {backup_dir}")


def _rebuild_dataset(snapshots: list[str], include_live: bool) -> dict:
    """Rebuild the backfill parquet over `snapshots`, optionally union live Mongo labels.

    Reuses app.ml.dataset verbatim. Writes data/nba_dataset.parquet (what train.main reads).
    Returns a small summary {backfill_rows, live_rows, cross_sell_n, cross_sell_base_rate}.
    """
    import pandas as pd

    from app.ml import dataset as ds

    print(f"snapshots: {snapshots}")
    bf = ds.build_dataset(snapshots)
    bf["is_live"] = 0
    summary = {"backfill_rows": int(len(bf)), "live_rows": 0}

    frames = [bf]
    if include_live:
        live = ds.live_labels()
        if len(live):
            # align columns (live frame may lack derived cols present only in backfill)
            for c in bf.columns:
                if c not in live.columns:
                    live[c] = 0
            live = live[bf.columns]
            frames.append(live)
            summary["live_rows"] = int(len(live))

    full = pd.concat(frames, ignore_index=True) if len(frames) > 1 else bf
    full.to_parquet(PARQUET, index=False)

    cs = full[full["task_type"] == "cross_sell"]
    summary["cross_sell_n"] = int(len(cs))
    summary["cross_sell_base_rate"] = float(cs["label"].mean()) if len(cs) else float("nan")
    print(f"\ndataset written: {len(full)} rows "
          f"(backfill={summary['backfill_rows']}, live={summary['live_rows']}); "
          f"cross_sell n={summary['cross_sell_n']} base={summary['cross_sell_base_rate']:.1%}")
    return summary


def _retrain_in_place() -> None:
    """Run app.ml.train.main verbatim — it reads the parquet and writes artifacts in place."""
    from app.ml import train
    train.main()


def main() -> int:
    ap = argparse.ArgumentParser(description="Gated NBA propensity retrain with atomic swap.")
    today = dt.date.today()
    ap.add_argument("--auc-floor", type=float, default=DEFAULT_AUC_FLOOR,
                    help=f"abort + restore if pooled OOT AUC < this (default {DEFAULT_AUC_FLOOR})")
    ap.add_argument("--auc-epsilon", type=float, default=DEFAULT_AUC_EPSILON,
                    help="also abort if new pooled OOT AUC < old - epsilon (default 0.01)")
    ap.add_argument("--n-snapshots", type=int, default=N_SNAPSHOTS,
                    help=f"monthly vintages to backfill (default {N_SNAPSHOTS})")
    ap.add_argument("--no-live-labels", action="store_true",
                    help="skip the Mongo live-label union (backfill only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="rebuild->train->validate, then restore production unconditionally")
    args = ap.parse_args()

    snapshots = rolling_snapshots(today, n=args.n_snapshots)
    ts = time.strftime("%Y%m%d_%H%M%S")
    include_live = not args.no_live_labels

    _hr(f"RETRAIN  today={today}  snapshots={snapshots[0]}..{snapshots[-1]}  "
        f"floor={args.auc_floor}  eps={args.auc_epsilon}  live={include_live}  dry_run={args.dry_run}")

    old_auc: float | None = None
    if (ART / "metrics.json").exists():
        try:
            old_auc = _pooled_oot_auc(ART / "metrics.json")
            print(f"current production pooled OOT AUC = {old_auc:.4f}")
        except Exception as exc:  # noqa: BLE001
            print(f"could not read current AUC: {exc}")

    backup_dir = _backup(ts)

    try:
        _hr("STEP 1/2 — rebuild dataset (backfill + optional live-label union)")
        ds_summary = _rebuild_dataset(snapshots, include_live)
        _hr("STEP 2/2 — retrain calibrated propensity model (in place)")
        _retrain_in_place()
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR during retrain: {exc}")
        _restore(backup_dir)
        _hr("RETRAIN ABORTED (exception) — old artifacts restored")
        return 2

    # ------------------------------------------------------------------- VALIDATE (the gate)
    _hr("VALIDATE")
    new_auc = _pooled_oot_auc(ART / "metrics.json")
    cs_auc = _crosssell_oot_auc(ART / "metrics.json")
    floor_ok = new_auc >= args.auc_floor
    regress_ok = old_auc is None or new_auc >= old_auc - args.auc_epsilon
    passed = floor_ok and regress_ok

    print(f"new pooled OOT AUC   = {new_auc:.4f}   (floor {args.auc_floor:.2f} -> "
          f"{'OK' if floor_ok else 'FAIL'})")
    if old_auc is not None:
        print(f"old pooled OOT AUC   = {old_auc:.4f}   (delta {new_auc - old_auc:+.4f}, "
              f"eps {args.auc_epsilon} -> {'OK' if regress_ok else 'FAIL'})")
    if cs_auc is not None:
        print(f"cross_sell OOT AUC   = {cs_auc:.4f}   (reported, not gated)")
    print(f"dataset: backfill={ds_summary['backfill_rows']} live={ds_summary['live_rows']}  "
          f"cross_sell n={ds_summary['cross_sell_n']} base={ds_summary['cross_sell_base_rate']:.1%}")
    card_errors = _model_card_consistency_errors(ART / "metrics.json", ART / "MODEL_CARD.md")
    card_ok = not card_errors
    if card_errors:
        print("MODEL_CARD consistency gate FAILED:")
        for err in card_errors:
            print(f"  - {err}")
    passed = passed and card_ok

    if args.dry_run:
        _restore(backup_dir)
        _hr("DRY-RUN COMPLETE — production artifacts UNCHANGED")
        print(f"gate {'PASS' if passed else 'FAIL'}; backup at {backup_dir}")
        return 0 if passed else 1

    if not passed:
        why = []
        if not floor_ok:
            why.append(f"{new_auc:.4f} < floor {args.auc_floor:.2f}")
        if not regress_ok:
            why.append(f"{new_auc:.4f} < old {old_auc:.4f} - eps {args.auc_epsilon}")
        if not card_ok:
            why.append("MODEL_CARD.md is stale or missing")
        print(f"\nGATE FAILED ({'; '.join(why)}) -> ABORTING swap.")
        _restore(backup_dir)
        _hr("RETRAIN ABORTED — old artifacts kept")
        return 1

    _hr("RETRAIN COMPLETE — new artifacts live")
    print(f"AUC {old_auc if old_auc is not None else float('nan'):.4f} -> {new_auc:.4f}   "
          f"backup kept at {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
