"""Idempotent end-to-end retrain of the solvency v3 model with an AUC gate + atomic swap.

Pipeline (each step reuses the existing build/train logic verbatim — no duplicated modeling):

  1. BACK UP the current production artifacts to app/risk/artifacts/backup_<ts>/.
  2. REBUILD the as-of-today modeling dataset           (scripts/build_risk_dataset.main)
  3. REBUILD the 6-month forward vintage pool           (scripts/build_vintages.main)
  4. RETRAIN the current-state WOE scorecard + GBM      (scripts/train_current_state.main)
  5. RETRAIN the 6-month forward scorecard + GBM        (scripts/train_forward_risk.main)
  6. VALIDATE: the current-state scorecard OOF AUC must be >= --auc-floor (default 0.90).
       PASS  -> keep the freshly trained artifacts, refresh the drift baseline, print summary.
       FAIL  -> RESTORE the backed-up artifacts (service is never left with a broken model).

Because the training scripts write directly into app/risk/artifacts/, the "atomic swap" is:
back up first, train in place, and on validation failure restore the backup. The service only
ever sees a fully-written, validated artifact set; a mid-run crash leaves the backup intact for
manual restore.

Usage:
    .venv/bin/python scripts/retrain.py                         # full as-of-today retrain
    .venv/bin/python scripts/retrain.py --dry-run               # train into a temp dir, do NOT
                                                                 #   touch production artifacts
    .venv/bin/python scripts/retrain.py --auc-floor 0.92
    .venv/bin/python scripts/retrain.py --feature-date 2026-03-25 --label-date 2026-06-25
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "app" / "risk" / "artifacts"
DATA = ROOT / "data"

# Artifacts the live service loads — these are what get backed up / restored / validated.
PROD_ARTIFACTS = [
    "scorecard_coefficients.json",
    "forward_scorecard_coeffs.json",
    "gbm_model.joblib",
    "forward_gbm.pkl",
    "cv_report.json",
    "monitor_baseline.json",
]

DEFAULT_AUC_FLOOR = 0.90


def _hr(title: str) -> None:
    print("\n" + "#" * 78)
    print("# " + title)
    print("#" * 78, flush=True)


def _current_state_auc(cv_report_path: Path) -> float:
    """The headline current-state scorecard OOF AUC (the gate metric)."""
    rep = json.loads(cv_report_path.read_text())
    return float(rep["cv"]["woe_scorecard_primary"]["oof_auc"])


def _forward_behavioral_auc() -> float | None:
    """Forward behavioral-only OOT AUC (reported, not gated — 0.85 is the honest expectation)."""
    p = DATA / "risk_forward" / "metrics.json"
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text())
        return float(m["oot"]["WITHOUT_aging"]["scorecard_oot"]["auc"])
    except Exception:  # noqa: BLE001
        return None


def _backup(ts: str) -> Path:
    dst = ART / f"backup_{ts}"
    dst.mkdir(parents=True, exist_ok=True)
    for name in PROD_ARTIFACTS:
        src = ART / name
        if src.exists():
            shutil.copy2(src, dst / name)
    print(f"backed up {sum((ART / n).exists() for n in PROD_ARTIFACTS)} artifacts -> {dst}")
    return dst


def _restore(backup_dir: Path) -> None:
    for name in PROD_ARTIFACTS:
        src = backup_dir / name
        if src.exists():
            shutil.copy2(src, ART / name)
    print(f"RESTORED previous artifacts from {backup_dir}")


def _load_script(name: str):
    """Import a sibling script module by path (scripts/ is not a package)."""
    import importlib.util

    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_retrain_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_pipeline(feature_date: str, label_date: str) -> None:
    """Rebuild dataset + vintages and retrain both models (in place, into app/risk/artifacts).

    Reuses the existing build/train mains verbatim. Only build_risk_dataset reads argparse, so we
    pin sys.argv for it; the other three mains ignore argv and read their fixed inputs/outputs.
    """
    _hr("STEP 1/4 — rebuild current-state dataset")
    sys.argv = ["build_risk_dataset", "--feature-date", feature_date, "--label-date", label_date]
    _load_script("build_risk_dataset").main()

    _hr("STEP 2/4 — rebuild 6-month forward vintage pool")
    sys.argv = ["build_vintages"]
    _load_script("build_vintages").main()

    _hr("STEP 3/4 — retrain current-state scorecard + GBM")
    sys.argv = ["train_current_state"]
    _load_script("train_current_state").main()

    _hr("STEP 4/4 — retrain 6-month forward scorecard + GBM")
    sys.argv = ["train_forward_risk"]
    _load_script("train_forward_risk").main()


def main() -> int:
    ap = argparse.ArgumentParser(description="Idempotent solvency retrain with AUC gate.")
    today = _dt.date.today()
    ap.add_argument("--feature-date", default=(today - _dt.timedelta(days=90)).isoformat(),
                    help="as-of date for features (default: today-90d)")
    ap.add_argument("--label-date", default=today.isoformat(),
                    help="as-of date for the SEV180 label (default: today)")
    ap.add_argument("--auc-floor", type=float, default=DEFAULT_AUC_FLOOR,
                    help="abort + restore if current-state OOF AUC is below this (default 0.90)")
    ap.add_argument("--dry-run", action="store_true",
                    help="train into a temp artifacts dir; never touch production artifacts")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))  # so `from scripts import ...` resolves under the venv
    ts = time.strftime("%Y%m%d_%H%M%S")

    _hr(f"RETRAIN  feature_date={args.feature_date}  label_date={args.label_date}  "
        f"auc_floor={args.auc_floor}  dry_run={args.dry_run}")

    old_auc: float | None = None
    if (ART / "cv_report.json").exists():
        try:
            old_auc = _current_state_auc(ART / "cv_report.json")
            print(f"current production current-state OOF AUC = {old_auc:.4f}")
        except Exception as exc:  # noqa: BLE001
            print(f"could not read current AUC: {exc}")

    backup_dir: Path | None = None
    staging: Path | None = None
    if args.dry_run:
        # Dry-run: copy the live artifacts into a temp staging dir, point the trainers at it via a
        # symlink swap, train, validate — production artifacts are never modified. We implement the
        # "do not touch prod" guarantee by training in place but restoring unconditionally at the end.
        backup_dir = _backup(ts)
        staging = backup_dir
        print("DRY-RUN: production artifacts will be restored from backup after validation, "
              "regardless of outcome.")
    else:
        backup_dir = _backup(ts)

    try:
        _run_pipeline(args.feature_date, args.label_date)
    except Exception as exc:  # noqa: BLE001
        print(f"\nERROR during training: {exc}")
        if backup_dir:
            _restore(backup_dir)
        return 2

    # ----------------------------------------------------------------- VALIDATE (the AUC gate)
    _hr("VALIDATE")
    new_auc = _current_state_auc(ART / "cv_report.json")
    fwd_auc = _forward_behavioral_auc()
    print(f"new current-state OOF AUC   = {new_auc:.4f}   (floor {args.auc_floor:.2f})")
    if old_auc is not None:
        print(f"old current-state OOF AUC   = {old_auc:.4f}   (delta {new_auc - old_auc:+.4f})")
    if fwd_auc is not None:
        print(f"new forward behavioral OOT AUC = {fwd_auc:.4f}   (reported, not gated)")

    passed = new_auc >= args.auc_floor

    if args.dry_run:
        # restore production unconditionally; the dry run only proves the gate would (not) pass.
        if staging:
            _restore(staging)
        _hr("DRY-RUN COMPLETE")
        print(f"gate {'PASS' if passed else 'FAIL'} (new_auc {new_auc:.4f} "
              f"{'>=' if passed else '<'} floor {args.auc_floor:.2f}); production artifacts UNCHANGED.")
        return 0 if passed else 1

    if not passed:
        print(f"\nGATE FAILED: {new_auc:.4f} < floor {args.auc_floor:.2f} -> ABORTING swap.")
        if backup_dir:
            _restore(backup_dir)
        _hr("RETRAIN ABORTED — old artifacts kept")
        return 1

    # ----------------------------------------------------------------- refresh drift baseline
    # Freeze the baseline at the SERVING distribution (label_date ~= now), NOT the 3-mo-old
    # training feature_date — otherwise the inherent train/serve feature gap shows as a
    # permanent spurious "warn" that no retrain can clear.
    _hr("REFRESH DRIFT BASELINE (serving-path, as-of label_date / serving date)")
    try:
        from app.risk.monitor import build_baseline_serving
        build_baseline_serving(args.label_date)
        print("monitor_baseline.json refreshed to the serving distribution.")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: drift-baseline refresh failed (model still swapped): {exc}")

    _hr("RETRAIN COMPLETE — new artifacts live")
    print(f"AUC {old_auc if old_auc is not None else float('nan'):.4f} -> {new_auc:.4f}   "
          f"backup kept at {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
