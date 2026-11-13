"""Gated, idempotent end-to-end retrain of the NBA propensity model with an atomic swap.

Pipeline (reuses the existing build/train logic verbatim — no duplicated modeling):

  1. BACK UP the current production artifacts to app/ml/artifacts/backup_<ts>/.
  2. REBUILD the vintaged backfill dataset, rolling the monthly snapshot window forward so the
     newest vintage tracks "now" (default: last 5 month-starts up to the last fully-labelled
     vintage, i.e. >= H_DAYS in the past so the (T, T+H] outcome window is complete).
  3. UNION LIVE LABELS: append manager-logged terminal-task outcomes from Mongo (live ground
     truth) onto the backfill. Live rows are upweighted (sample_weight) so a handful of real
     conversions count more than the backfill proxy. TODAY there are 0 live labels, so this is a
     no-op and the set is backfill-only — but the path is wired and tested.
  4. RETRAIN the calibrated HGB/logit propensity model (app.ml.train.main) in place.
  5. VALIDATE — the gate: pooled OOT AUC must be >= --auc-floor (default 0.62) AND
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
import math
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import history  # noqa: E402

ART = ROOT / "app" / "ml" / "artifacts"
DATA = ROOT / "data"
PARQUET = DATA / "nba_dataset.parquet"

# Artifacts the live scoring head loads — backed up / restored / swapped as a set.
PROD_ARTIFACTS = ["propensity_model.joblib", "model_meta.json", "metrics.json", "MODEL_CARD.md"]

# Absolute sanity floor: "this model is broken", not "this window is hard". The OOT window rolls
# forward every run and a harder window drags every model down (May-Jun 2026: old 0.661, new 0.668
# vs 0.713 on Feb-Apr), so the meaningful guard is the same-fold comparison against the model in
# production, not a high absolute number frozen from an easier window.
DEFAULT_AUC_FLOOR = 0.62
DEFAULT_AUC_EPSILON = 0.01
N_SNAPSHOTS = 5
BACKUPS_KEPT = 4
H_DAYS = 60  # outcome window; a vintage is only fully labelled once it is >= H_DAYS in the past
TRAINING_WINDOW_DAYS = 365
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
    if n <= 0:
        raise ValueError("n must be positive")
    latest = _month_start(today - dt.timedelta(days=h_days))
    out: list[dt.date] = []
    cur = latest
    for _ in range(n):
        out.append(cur)
        cur = _prev_month_start(cur)
    snapshots = [d.isoformat() for d in reversed(out)]
    for snapshot in snapshots:
        history.training_window(snapshot, TRAINING_WINDOW_DAYS)
    return snapshots


def _model_history_errors(meta_path: Path) -> list[str]:
    """Reject legacy models whose training boundary cannot be proven."""
    if not meta_path.exists():
        return [f"{meta_path.name} missing"]
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError) as exc:
        return [f"{meta_path.name} unreadable: {exc}"]
    errors: list[str] = []
    source_start = history.source_history_start().isoformat()
    if meta.get("source_history_start") != source_start:
        errors.append("source_history_start mismatch")
    if meta.get("training_window_days") != TRAINING_WINDOW_DAYS:
        errors.append("training_window_days mismatch")
    vintages = meta.get("training_vintages")
    if not isinstance(vintages, list) or not vintages:
        errors.append("training_vintages missing")
    else:
        try:
            canonical_vintages = [
                history.training_window(vintage, TRAINING_WINDOW_DAYS).as_of.isoformat()
                for vintage in vintages
            ]
            if canonical_vintages != sorted(set(canonical_vintages)):
                errors.append("training_vintages must be sorted and unique")
        except (TypeError, ValueError) as exc:
            errors.append(f"incomplete training vintage: {exc}")
    return errors


def _pooled_oot_auc(metrics_path: Path) -> float:
    """The gate metric: pooled OOT AUC of the chosen production model."""
    rep = json.loads(metrics_path.read_text())
    prod = rep.get("production_model", "hgb")
    return float(rep["oot"][prod]["auc"])


def _oot_split(metrics_path: Path) -> tuple[str, str, str] | None:
    """Which temporal window the pooled OOT AUC in this metrics file was measured on."""
    try:
        split = json.loads(metrics_path.read_text())["oot_split"]
        return str(split["train_max"]), str(split["test"][0]), str(split["test"][1])
    except Exception:  # noqa: BLE001
        return None


def _dataset_rule(meta_path: Path) -> str | None:
    try:
        return json.loads(meta_path.read_text()).get("dataset_rule")
    except (OSError, ValueError):
        return None


def _same_evaluation_rows(backup_dir: Path) -> bool:
    """True when the stored AUC of the previous model was measured on the rows the new one was:
    the same temporal fold of a dataset built by the same rule."""
    return (_oot_split(ART / "metrics.json") == _oot_split(backup_dir / "metrics.json")
            and _dataset_rule(ART / "model_meta.json") == _dataset_rule(backup_dir / "model_meta.json"))


def _old_quality_on_current_split(backup_dir: Path) -> tuple[dict | None, str]:
    """AUC, Brier and calibration of the PREVIOUS production model on the CURRENT test fold,
    pooled and per task type — the same rows the new model's oot_quality is reported on."""
    import joblib
    import pandas as pd

    from app.ml.train import FEATURES, quality_by_type, resolve_oot_split

    old_meta_path = backup_dir / "model_meta.json"
    if old_meta_path.exists():
        old_features = json.loads(old_meta_path.read_text()).get("features")
        if old_features and list(old_features) != list(FEATURES):
            return None, "набір ознак змінився — стару модель не можна оцінити на новому фолді"

    model_path = backup_dir / "propensity_model.joblib"
    if not model_path.exists() or not PARQUET.exists():
        return None, "немає старої моделі або свіжого датасету"

    try:
        frame = pd.read_parquet(PARQUET)
        frame["vd"] = pd.to_datetime(frame["vintage"])
        _, test_lo, test_hi = resolve_oot_split(frame["vd"].tolist())
        test = frame[(frame["vd"] >= test_lo) & (frame["vd"] <= test_hi)]
        if test.empty or test["label"].nunique() < 2:
            return None, "новий тестовий фолд порожній або без обох класів"
        probabilities = joblib.load(model_path).predict_proba(test[FEATURES].astype(float))[:, 1]
    except Exception as exc:  # noqa: BLE001
        return None, f"стару модель не вдалося оцінити на новому фолді ({type(exc).__name__})"
    return quality_by_type(test, probabilities), f"{test_lo}..{test_hi}"


def _print_comparison(old: dict, new: dict) -> None:
    print(f"{'rows':26s} {'n':>6s}  {'AUC old':>8s} {'AUC new':>8s}  {'Brier old':>9s} "
          f"{'Brier new':>9s}  {'cal.err old':>11s} {'cal.err new':>11s}  "
          f"{'mean p old':>10s} {'mean p new':>10s} {'realised':>9s}")
    for key in ("pooled", "reorder_due", "reorder_due_served_rows", "debt_followup",
                "churn_winback", "cross_sell"):
        a, b = old.get(key) or {}, new.get(key) or {}
        if not (a.get("n") or b.get("n")):
            continue

        def cell(row: dict, name: str, width: int) -> str:
            value = row.get(name)
            return f"{value:{width}.4f}" if isinstance(value, float) and not math.isnan(value) \
                else " " * (width - 3) + "n/a"

        print(f"{key:26s} {int(b.get('n') or a.get('n') or 0):6d}  "
              f"{cell(a, 'auc', 8)} {cell(b, 'auc', 8)}  {cell(a, 'brier', 9)} {cell(b, 'brier', 9)}  "
              f"{cell(a, 'calibration_error', 11)} {cell(b, 'calibration_error', 11)}  "
              f"{cell(a, 'mean_p', 10)} {cell(b, 'mean_p', 10)} {cell(b or a, 'base_rate', 9)}")


def _old_auc_on_current_split(backup_dir: Path) -> tuple[float | None, str]:
    """Score the PREVIOUS production model on the CURRENT test fold.

    The dataset window rolls forward every run, so the AUC stored in the old metrics.json was
    measured on an older, different test fold. Comparing it to the new number would flag a
    regression whenever the fresh window is simply harder — and would block every retrain
    forever. Re-scoring the old model on the new fold makes the delta gate an apples-to-apples
    comparison again.
    """
    quality, detail = _old_quality_on_current_split(backup_dir)
    if quality is None:
        return None, detail
    return float(quality["pooled"]["auc"]), detail


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


def _publish_heads(metrics_path: Path, meta_path: Path, min_rows: int, auc_floor: float) -> dict:
    """Per-type gate: a head with too few labelled rows or an OOT AUC below the floor is recorded
    as not published, so serving ranks that task type by the deterministic heuristic."""
    rep = json.loads(metrics_path.read_text())
    prod = rep.get("production_model", "hgb")
    heads = {}
    for task_type in rep.get("cv_per_type", {}):
        rows = int(rep["cv_per_type"][task_type].get("n") or 0)
        auc = (rep.get("oot_per_type", {}).get(prod, {}).get(task_type) or {}).get("auc")
        auc = float(auc) if isinstance(auc, (int, float)) and not math.isnan(auc) else None
        reasons = []
        if rows < min_rows:
            reasons.append(f"rows {rows} < {min_rows}")
        if auc is None:
            reasons.append("OOT AUC unavailable")
        elif auc < auc_floor:
            reasons.append(f"OOT AUC {auc:.4f} < floor {auc_floor:.2f}")
        heads[task_type] = {"rows": rows, "oot_auc": auc, "published": not reasons,
                            "reasons": reasons}
        print(f"head {task_type:16s} rows={rows:<6d} auc={auc if auc is not None else 'n/a'}  "
              f"-> {'PUBLISHED' if not reasons else 'WITHHELD (' + '; '.join(reasons) + ')'}")
    meta = json.loads(meta_path.read_text())
    meta["heads"] = heads
    meta["head_gate"] = {"min_rows": min_rows, "auc_floor": auc_floor}
    meta_path.write_text(json.dumps(meta, indent=2))
    return heads


def _prune_backups(keep: int = BACKUPS_KEPT) -> list[Path]:
    """Keep only the newest `keep` backup directories (their names sort by timestamp)."""
    backups = sorted(path for path in ART.glob("backup_*") if path.is_dir())
    removed = backups[:-keep] if keep > 0 else backups
    for path in removed:
        shutil.rmtree(path)
    if removed:
        print(f"pruned {len(removed)} old backup dirs; kept {min(keep, len(backups))}")
    return removed


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
    coverage = [ds.RECO_COVERAGE.get(snapshot, {}) for snapshot in snapshots]
    summary["reco_asked"] = sum(int(c.get("asked", 0)) for c in coverage)
    summary["reco_failed"] = sum(int(c.get("failed", 0)) for c in coverage)
    summary["reco_offline_vintages"] = sum(1 for c in coverage if c.get("offline"))
    print(f"reco: asked {summary['reco_asked']} clients, {summary['reco_failed']} calls failed, "
          f"{summary['reco_offline_vintages']} vintages with reco offline")
    if summary["reco_offline_vintages"] or summary["reco_failed"] > 0.2 * max(summary["reco_asked"], 1):
        print("WARNING: cross_sell rows are incomplete — reco did not answer for part of the pool; "
              "the cached answers are kept and the next run asks only for the missing ones")
    try:
        from app.data import signals_repository as sig
        removed = ds.prune_reco_cache(sig.client_net_uids())
        if removed:
            print(f"reco cache: removed {removed} entries no current client can be joined to")
    except Exception as exc:  # noqa: BLE001
        print(f"reco cache not pruned ({type(exc).__name__}: {exc})")

    frames = [bf]
    if include_live:
        live = ds.live_labels()
        if len(live):
            # align columns (live frame may lack derived cols present only in backfill)
            for c in bf.columns:
                if c not in live.columns:
                    live[c] = 0
            live["dataset_rule"] = ds.DATASET_RULE
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


def _use_work_dir(work_dir: Path, seed: Path) -> None:
    """Point every path this run writes at work_dir and copy the production artefacts in.

    The copy is what the run backs up, compares against and may overwrite; the directory it
    came from is never written.
    """
    global ART, DATA, PARQUET
    from app.ml import dataset as ds
    from app.ml import train

    work_dir = work_dir.resolve()
    artifacts = work_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (work_dir / "data").mkdir(parents=True, exist_ok=True)
    for name in PROD_ARTIFACTS:
        source = seed / name
        if source.exists() and source.resolve() != (artifacts / name).resolve():
            shutil.copy2(source, artifacts / name)
    ART = artifacts
    DATA = work_dir / "data"
    PARQUET = DATA / "nba_dataset.parquet"
    train.ART = artifacts
    train.DATA = PARQUET
    ds._RECO_CACHE_DIR = str(DATA / "reco_cache")
    print(f"work dir {work_dir}: artefacts seeded from {seed}; production is read-only")


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
    ap.add_argument("--min-head-rows", type=int, default=None,
                    help="per-type gate: labelled rows a head needs to be published "
                         "(default: min_model_training_rows from the service config)")
    ap.add_argument("--no-live-labels", action="store_true",
                    help="skip the Mongo live-label union (backfill only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="rebuild->train->validate, then restore production unconditionally")
    ap.add_argument("--work-dir", type=Path, default=None,
                    help="run entirely inside this directory: the dataset, the reco cache and the "
                         "artefacts are written there and production artefacts are only read")
    ap.add_argument("--seed-artifacts", type=Path, default=None,
                    help="with --work-dir: the production artefact directory to compare against "
                         "(default: this checkout's app/ml/artifacts)")
    args = ap.parse_args()
    if args.work_dir is not None:
        _use_work_dir(args.work_dir, args.seed_artifacts or ART)

    snapshots = rolling_snapshots(today, n=args.n_snapshots)
    ts = time.strftime("%Y%m%d_%H%M%S")
    include_live = not args.no_live_labels

    _hr(f"RETRAIN  today={today}  snapshots={snapshots[0]}..{snapshots[-1]}  "
        f"floor={args.auc_floor}  eps={args.auc_epsilon}  live={include_live}  dry_run={args.dry_run}")

    old_auc: float | None = None
    old_history_errors = _model_history_errors(ART / "model_meta.json")
    if old_history_errors:
        print("current production model is history-incompatible; regression comparison disabled:")
        for error in old_history_errors:
            print(f"  - {error}")
    elif (ART / "metrics.json").exists():
        try:
            old_auc = _pooled_oot_auc(ART / "metrics.json")
            print(f"current production pooled OOT AUC = {old_auc:.4f}")
        except Exception as exc:  # noqa: BLE001
            print(f"could not read current AUC: {exc}")

    backup_dir = _backup(ts)
    _prune_backups()

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

    # The regression gate only means something when both AUCs come from the same test fold.
    comparison_note = ""
    old_quality, old_quality_detail = (None, "")
    if old_auc is not None:
        old_quality, old_quality_detail = _old_quality_on_current_split(backup_dir)
    if old_auc is not None and not _same_evaluation_rows(backup_dir):
        if old_quality is None:
            old_auc = None
            comparison_note = ("regression gate disabled: тестові рядки змінились (вікно OOT або "
                               f"правило датасету), {old_quality_detail}")
        else:
            rescored = float(old_quality["pooled"]["auc"])
            comparison_note = (f"old AUC re-scored on the current fold {old_quality_detail}: "
                               f"{old_auc:.4f} -> {rescored:.4f}")
            old_auc = rescored
    new_quality = json.loads((ART / "metrics.json").read_text()).get("oot_quality")
    if old_quality is not None and new_quality:
        print(f"previous model against the new one on the same out-of-time rows {old_quality_detail} "
              "(the previous model was fitted on vintages that include this fold; the new one is "
              "scored before its final refit, so the comparison leans towards the previous model):")
        _print_comparison(old_quality, new_quality)
        metrics = json.loads((ART / "metrics.json").read_text())
        metrics["previous_model_on_current_fold"] = old_quality
        (ART / "metrics.json").write_text(json.dumps(metrics, indent=2, default=float))

    from app.core.config import get_settings
    min_head_rows = (args.min_head_rows if args.min_head_rows is not None
                     else get_settings().min_model_training_rows)
    _publish_heads(ART / "metrics.json", ART / "model_meta.json", min_head_rows, args.auc_floor)

    floor_ok = new_auc >= args.auc_floor
    regress_ok = old_auc is None or new_auc >= old_auc - args.auc_epsilon
    passed = floor_ok and regress_ok

    if comparison_note:
        print(comparison_note)
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
    model_history_errors = _model_history_errors(ART / "model_meta.json")
    card_ok = not card_errors
    if card_errors:
        print("MODEL_CARD consistency gate FAILED:")
        for err in card_errors:
            print(f"  - {err}")
    if model_history_errors:
        print("MODEL history compatibility gate FAILED:")
        for err in model_history_errors:
            print(f"  - {err}")
    history_ok = not model_history_errors
    passed = passed and card_ok and history_ok

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
        if not history_ok:
            why.append("model history metadata is incompatible")
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
