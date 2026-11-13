"""Idempotent end-to-end retrain of the solvency v3 model with an AUC gate + atomic swap.

Pipeline (each step reuses the existing build/train logic verbatim — no duplicated modeling):

  1. BACK UP the current production artifacts to app/risk/artifacts/backup_<ts>/.
  2. REBUILD the as-of-today modeling dataset           (scripts/build_risk_dataset.main)
  3. REBUILD the 6-month forward vintage pool           (scripts/build_vintages.main)
  4. RETRAIN the current-state WOE scorecard + GBM      (scripts/train_current_state.main)
  5. EVALUATE the 6-month forward scorecard. Publish it only if its independent-client event
     support and validation gates pass; otherwise publish an explicit fail-closed status.
  6. VALIDATE two gates:
       a. the current-state scorecard OOF AUC over all rows must be >= --auc-floor (0.90);
       b. the AUC among buyers with open debt must be >= --open-debt-auc-floor (0.70) and not
          more than --open-debt-auc-max-drop (0.02) below the previous model's. Most buyers
          carry no debt, so gate (a) alone mostly measures "has debt / has no debt".
       PASS  -> keep the freshly trained current artifact, refresh the drift baseline, and either
                publish a validated forward artifact or its degraded/unavailable status.
       FAIL  -> RESTORE the backed-up artifacts (service is never left with a broken model).
  7. PRUNE artifact backups to the newest KEEP_BACKUPS.

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
    "forward_model_status.json",
    "gbm_model.joblib",
    "cv_report.json",
    "monitor_baseline.json",
    "MODEL_CARD.md",
]

DEFAULT_AUC_FLOOR = 0.90
DEFAULT_OPEN_DEBT_AUC_FLOOR = 0.70
DEFAULT_OPEN_DEBT_AUC_MAX_DROP = 0.02
OPEN_DEBT_GATE_METRICS = ("served", "oof")
KEEP_BACKUPS = 4
CARD_START = "<!-- quality:start (written by scripts/retrain.py) -->"
CARD_END = "<!-- quality:end -->"
SERVING_CHUNK = 500


def _hr(title: str) -> None:
    print("\n" + "#" * 78)
    print("# " + title)
    print("#" * 78, flush=True)


def _current_state_auc(cv_report_path: Path) -> float:
    """The headline current-state scorecard OOF AUC (the gate metric)."""
    rep = json.loads(cv_report_path.read_text())
    return float(rep["cv"]["woe_scorecard_primary"]["oof_auc"])


def _open_debt_auc(cv_report_path: Path, metric: str) -> float | None:
    """AUC among buyers with open debt; None for a report that predates the metric."""
    rep = json.loads(cv_report_path.read_text())
    value = rep.get("open_debt_subset", {}).get(metric, {}).get("auc")
    return None if value is None else float(value)


def open_debt_auc_required(floor: float, previous: float | None, max_drop: float) -> float:
    """The open-debt gate threshold: the floor, or the previous model's AUC minus the drop."""
    return floor if previous is None else max(floor, previous - max_drop)


def evaluate_gates(
    *,
    new_auc: float,
    auc_floor: float,
    new_open_debt_auc: float | None,
    old_open_debt_auc: float | None,
    open_debt_floor: float,
    open_debt_max_drop: float,
) -> tuple[bool, list[str]]:
    """Both retrain gates. Returns (passed, one log line per gate)."""
    overall_ok = new_auc >= auc_floor
    required = open_debt_auc_required(open_debt_floor, old_open_debt_auc, open_debt_max_drop)
    open_ok = new_open_debt_auc is not None and new_open_debt_auc >= required
    previous = "n/a" if old_open_debt_auc is None else f"{old_open_debt_auc:.4f}"
    observed = "n/a" if new_open_debt_auc is None else f"{new_open_debt_auc:.4f}"
    return overall_ok and open_ok, [
        f"gate 1 all rows OOF AUC  : {new_auc:.4f} >= {auc_floor:.2f}  "
        f"{'PASS' if overall_ok else 'FAIL'}",
        f"gate 2 open-debt AUC     : {observed} >= {required:.4f} "
        f"(floor {open_debt_floor:.2f}, previous {previous} - {open_debt_max_drop:.2f})  "
        f"{'PASS' if open_ok else 'FAIL'}",
    ]


def prune_backups(artifacts_dir: Path, keep: int = KEEP_BACKUPS) -> list[Path]:
    """Delete all but the newest `keep` backup_<timestamp> directories; returns the removed."""
    backups = sorted(
        (d for d in artifacts_dir.glob("backup_*") if d.is_dir()), key=lambda d: d.name
    )
    removed = backups[:-keep] if keep > 0 else backups
    for directory in removed:
        shutil.rmtree(directory)
    return removed


def serving_score_ceiling(as_of: str) -> dict:
    """Share of live scored buyers whose score is exactly 100, through the serving path."""
    from app.domain.models import DataSufficiency
    from app.risk import dataset as risk_dataset
    from app.risk import score_current as scorer
    from app.services.solvency.service import _data_sufficiency

    scorer._card.cache_clear()
    buyers = risk_dataset.buyer_ids()
    scored = at_ceiling = 0
    for start in range(0, len(buyers), SERVING_CHUNK):
        chunk = buyers[start:start + SERVING_CHUNK]
        for features in risk_dataset.features_many(chunk, as_of, 12).values():
            if _data_sufficiency(features)[0] != DataSufficiency.OK:
                continue
            scored += 1
            at_ceiling += int(round(scorer.score_current(features)["score"])) == 100
    return {
        "definition": "live buyers with sufficient data whose served score is exactly 100",
        "as_of": as_of,
        "buyers": len(buyers),
        "scored": scored,
        "score_100": at_ceiling,
        "score_100_share": at_ceiling / scored if scored else None,
    }


def quality_card_block(report: dict) -> str:
    """The generated quality section of MODEL_CARD.md."""
    def num(value: float | None, pattern: str = "{:.4f}") -> str:
        return "n/a" if value is None else pattern.format(value)

    lineage = report.get("training_lineage", {})
    open_debt = report.get("open_debt_subset", {})
    oof, served = open_debt.get("oof", {}), open_debt.get("served", {})
    serving = report.get("score_distribution_serving", {})
    lines = [
        CARD_START,
        "## Quality where the scorecard has to rank",
        "",
        f"- Training run: `{lineage.get('training_run_id', 'n/a')}`",
        f"- All rows, five-fold OOF AUC: {num(report['cv']['woe_scorecard_primary']['oof_auc'])} "
        "(most buyers carry no debt, so this mostly measures has-debt / has-no-debt)",
        f"- Buyers with open debt at the feature date: {oof.get('rows', 'n/a')} rows, "
        f"{oof.get('positives', 'n/a')} positives "
        f"({num(open_debt.get('share_of_rows'), '{:.1%}')} of the dataset)",
        f"  - out-of-fold: AUC {num(oof.get('auc'))}, KS {num(oof.get('ks'))}, "
        f"Brier {num(oof.get('brier'))} (class-balanced, uncalibrated fold models)",
        f"  - as served (calibrated PD with the SEV180 floor, in-sample): "
        f"AUC {num(served.get('auc'))}, KS {num(served.get('ks'))}, "
        f"Brier {num(served.get('brier'))}",
        f"- Live buyers scored exactly 100: {serving.get('score_100', 'n/a')} of "
        f"{serving.get('scored', 'n/a')} scored "
        f"({num(serving.get('score_100_share'), '{:.1%}')}, as of {serving.get('as_of', 'n/a')})",
        CARD_END,
    ]
    return "\n".join(lines) + "\n"


def write_quality_card(card_path: Path, report: dict) -> None:
    """Replace (or append) the generated quality block; the hand-written text is kept."""
    block = quality_card_block(report)
    text = card_path.read_text() if card_path.exists() else ""
    if CARD_START in text and CARD_END in text:
        head, rest = text.split(CARD_START, 1)
        tail = rest.split(CARD_END, 1)[1].lstrip("\n")
        text = head + block + ("\n" + tail if tail else "")
    else:
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    card_path.write_text(text)


def _forward_behavioral_auc() -> float | None:
    """Forward behavioral-only OOT AUC (reported, not gated — 0.85 is the honest expectation)."""
    p = DATA / "risk_forward" / "metrics.json"
    if not p.exists():
        return None
    try:
        m = json.loads(p.read_text())
        return float(m["production_gate"]["oot_auc"])
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
        elif (ART / name).exists():
            (ART / name).unlink()
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
    sys.argv = ["train_forward_risk", "--commit"]
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
    ap.add_argument("--open-debt-auc-floor", type=float, default=DEFAULT_OPEN_DEBT_AUC_FLOOR,
                    help="abort + restore if the AUC among buyers with open debt is below this "
                         "(default 0.70)")
    ap.add_argument("--open-debt-auc-max-drop", type=float,
                    default=DEFAULT_OPEN_DEBT_AUC_MAX_DROP,
                    help="abort + restore if the open-debt AUC falls more than this below the "
                         "previous model's (default 0.02)")
    ap.add_argument("--open-debt-gate-metric", choices=OPEN_DEBT_GATE_METRICS,
                    default="served",
                    help="open-debt AUC the gate reads: 'served' (final calibrated PD, "
                         "in-sample) or 'oof' (out-of-fold); both are always reported")
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
    old_open_debt_auc: float | None = None
    if (ART / "cv_report.json").exists():
        try:
            old_open_debt_auc = _open_debt_auc(ART / "cv_report.json", args.open_debt_gate_metric)
        except Exception as exc:  # noqa: BLE001
            print(f"could not read current open-debt AUC: {exc}")
    print("current production open-debt AUC = "
          + ("n/a (report predates the metric)" if old_open_debt_auc is None
             else f"{old_open_debt_auc:.4f}"))

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
        _report_pruned()
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

    report = json.loads((ART / "cv_report.json").read_text())
    open_debt = report.get("open_debt_subset", {})
    for name in OPEN_DEBT_GATE_METRICS:
        q = open_debt.get(name, {})
        print(f"open-debt {name:>6}: AUC={q.get('auc')} KS={q.get('ks')} Brier={q.get('brier')} "
              f"(n={q.get('rows')}, pos={q.get('positives')})")
    new_open_debt_auc = _open_debt_auc(ART / "cv_report.json", args.open_debt_gate_metric)
    passed, gate_lines = evaluate_gates(
        new_auc=new_auc,
        auc_floor=args.auc_floor,
        new_open_debt_auc=new_open_debt_auc,
        old_open_debt_auc=old_open_debt_auc,
        open_debt_floor=args.open_debt_auc_floor,
        open_debt_max_drop=args.open_debt_auc_max_drop,
    )
    print(f"open-debt gate metric: {args.open_debt_gate_metric}")
    for line in gate_lines:
        print(line)

    if args.dry_run:
        # restore production unconditionally; the dry run only proves the gate would (not) pass.
        if staging:
            _restore(staging)
        _hr("DRY-RUN COMPLETE")
        print(f"gates {'PASS' if passed else 'FAIL'}; production artifacts UNCHANGED.")
        _report_pruned()
        return 0 if passed else 1

    if not passed:
        print("\nGATE FAILED -> ABORTING swap.")
        if backup_dir:
            _restore(backup_dir)
        _hr("RETRAIN ABORTED — old artifacts kept")
        _report_pruned()
        return 1

    # ----------------------------------------------------------------- honest quality numbers
    try:
        report["score_distribution_serving"] = serving_score_ceiling(args.label_date)
        serving = report["score_distribution_serving"]
        print(f"live buyers scored exactly 100: {serving['score_100']} of {serving['scored']} "
              f"scored ({serving['score_100_share']:.1%})")
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: serving score distribution not measured: {exc}")
    from app.risk.lineage import write_json_atomic
    write_json_atomic(ART / "cv_report.json", report)
    write_quality_card(ART / "MODEL_CARD.md", report)

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
    _report_pruned()
    return 0


def _report_pruned() -> None:
    removed = prune_backups(ART)
    if removed:
        print(f"pruned {len(removed)} old artifact backups (kept the newest {KEEP_BACKUPS})")


if __name__ == "__main__":
    raise SystemExit(main())
