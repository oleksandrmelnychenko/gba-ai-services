from __future__ import annotations

import importlib.util
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"_test_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def retrain():
    return _script("retrain")


@pytest.fixture(scope="module")
def train_current():
    return _script("train_current_state")


def test_subset_quality_reports_auc_ks_and_brier(train_current):
    y = np.array([0, 0, 0, 1, 1])
    perfect = train_current.subset_quality(y, np.array([0.1, 0.2, 0.3, 0.8, 0.9]))
    assert perfect == {
        "rows": 5, "positives": 2, "auc": 1.0, "ks": 1.0,
        "brier": pytest.approx((0.01 + 0.04 + 0.09 + 0.04 + 0.01) / 5),
    }
    inverted = train_current.subset_quality(y, np.array([0.9, 0.8, 0.7, 0.2, 0.1]))
    assert inverted["auc"] == 0.0


def test_subset_quality_without_both_classes_has_no_auc(train_current):
    only_negatives = train_current.subset_quality(np.zeros(4), np.full(4, 0.25))
    assert (only_negatives["auc"], only_negatives["ks"]) == (None, None)
    assert only_negatives["brier"] == pytest.approx(0.0625)
    empty = train_current.subset_quality(np.array([]), np.array([]))
    assert empty == {"rows": 0, "positives": 0, "auc": None, "ks": None, "brier": None}


def test_open_debt_quality_ignores_the_debt_free_majority(train_current):
    debt = np.array([0, 0, 0, 0, 0, 0, 100.0, 50.0, 10.0, 5.0])
    y = np.array([0, 0, 0, 0, 0, 0, 1, 0, 1, 0])
    oof = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.6, 0.7, 0.8, 0.9])
    served = np.array([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.9, 0.2, 0.8, 0.1])

    all_rows = train_current.subset_quality(y, oof)
    quality = train_current.open_debt_quality(debt, y, oof, served)

    assert all_rows["auc"] > 0.8
    assert quality["share_of_rows"] == pytest.approx(0.4)
    assert (quality["oof"]["rows"], quality["oof"]["positives"]) == (4, 2)
    assert quality["oof"]["auc"] == pytest.approx(0.25)
    assert quality["served"]["auc"] == 1.0


def test_score_ceiling_share_counts_scores_that_round_to_100(train_current):
    share = train_current.score_ceiling_share(np.array([100.0, 99.6, 99.4, 85.0]))
    assert (share["rows"], share["score_100_rows"], share["score_100_share"]) == (4, 2, 0.5)


GATE = dict(auc_floor=0.90, open_debt_floor=0.70, open_debt_max_drop=0.02)


@pytest.mark.parametrize(
    ("new_auc", "new_open", "old_open", "expected"),
    [
        (0.98, 0.77, None, True),
        (0.98, 0.77, 0.78, True),
        (0.98, 0.70, 0.71, True),
        (0.89, 0.77, 0.77, False),
        (0.98, 0.69, None, False),
        (0.98, 0.75, 0.78, False),
        (0.98, None, None, False),
        (0.98, 0.71, 0.60, True),
    ],
)
def test_retrain_needs_both_gates(retrain, new_auc, new_open, old_open, expected):
    passed, lines = retrain.evaluate_gates(
        new_auc=new_auc, new_open_debt_auc=new_open, old_open_debt_auc=old_open, **GATE
    )
    assert passed is expected
    assert len(lines) == 2
    assert lines[0].startswith("gate 1 all rows OOF AUC") and "0.90" in lines[0]
    assert lines[1].startswith("gate 2 open-debt AUC") and "floor 0.70" in lines[1]
    assert ("FAIL" in lines[0] or "FAIL" in lines[1]) is not expected


def test_open_debt_threshold_is_the_floor_or_previous_minus_drop(retrain):
    assert retrain.open_debt_auc_required(0.70, None, 0.02) == 0.70
    assert retrain.open_debt_auc_required(0.70, 0.60, 0.02) == 0.70
    assert retrain.open_debt_auc_required(0.70, 0.80, 0.02) == pytest.approx(0.78)
    assert retrain.DEFAULT_OPEN_DEBT_AUC_FLOOR == 0.70
    assert retrain.DEFAULT_OPEN_DEBT_AUC_MAX_DROP == 0.02


def test_open_debt_auc_is_read_from_the_report(retrain, tmp_path):
    path = tmp_path / "cv_report.json"
    path.write_text(json.dumps({
        "open_debt_subset": {"oof": {"auc": 0.63}, "served": {"auc": 0.77}},
    }))
    assert retrain._open_debt_auc(path, "served") == 0.77
    assert retrain._open_debt_auc(path, "oof") == 0.63
    path.write_text(json.dumps({"cv": {}}))
    assert retrain._open_debt_auc(path, "served") is None


def test_prune_backups_keeps_the_newest_four(retrain, tmp_path):
    names = [f"backup_202611{day:02d}_033000" for day in range(1, 8)]
    for name in names:
        (tmp_path / name).mkdir()
        (tmp_path / name / "cv_report.json").write_text("{}")
    (tmp_path / "scorecard_coefficients.json").write_text("{}")
    (tmp_path / "backup_notes.txt").write_text("not a directory")
    (tmp_path / "other").mkdir()

    removed = retrain.prune_backups(tmp_path)

    assert [d.name for d in removed] == names[:3]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        [*names[3:], "scorecard_coefficients.json", "backup_notes.txt", "other"]
    )
    assert retrain.prune_backups(tmp_path) == []
    assert retrain.KEEP_BACKUPS == 4


def _report(open_auc: float) -> dict:
    return {
        "training_lineage": {"training_run_id": "current-test"},
        "cv": {"woe_scorecard_primary": {"oof_auc": 0.9786}},
        "open_debt_subset": {
            "share_of_rows": 0.054,
            "oof": {"rows": 177, "positives": 25, "auc": 0.6254, "ks": 0.3061, "brier": 0.6033},
            "served": {"rows": 177, "positives": 25, "auc": open_auc, "ks": 0.66, "brier": 0.11},
        },
        "score_distribution_serving": {
            "as_of": "2026-11-12", "scored": 1000, "score_100": 720, "score_100_share": 0.72,
        },
    }


def test_quality_card_block_is_appended_then_replaced_in_place(retrain, tmp_path):
    card = tmp_path / "MODEL_CARD.md"
    card.write_text("# Solvency v3 model card\n\nHand-written text.\n")

    retrain.write_quality_card(card, _report(0.7709))
    first = card.read_text()
    assert first.startswith("# Solvency v3 model card\n\nHand-written text.\n\n")
    assert "AUC 0.7709" in first and "AUC 0.6254" in first
    assert "720 of 1000 scored (72.0%, as of 2026-11-12)" in first
    assert "177 rows, 25 positives (5.4% of the dataset)" in first

    card.write_text(first + "\n## Later section\n")
    retrain.write_quality_card(card, _report(0.8001))
    second = card.read_text()
    assert second.count(retrain.CARD_START) == 1 and second.count(retrain.CARD_END) == 1
    assert "AUC 0.8001" in second and "AUC 0.7709" not in second
    assert "Hand-written text." in second and second.rstrip().endswith("## Later section")


def test_dead_forward_gbm_artifact_is_no_longer_managed(retrain):
    assert "forward_gbm.pkl" not in retrain.PROD_ARTIFACTS
    assert "cv_report.json" in retrain.PROD_ARTIFACTS
    assert not (ROOT / "app" / "risk" / "artifacts" / "forward_gbm.pkl").exists()


def test_sanity_block_has_no_hard_coded_clients():
    source = (ROOT / "scripts" / "build_risk_dataset.py").read_text()
    assert "SANITY_CLIENTS" not in source
    assert 'nlargest(SANITY_ROWS, "overdue_eur_180plus")' in source


@pytest.mark.parametrize("penalty", ["l2", "l1"])
def test_scorecard_fits_without_the_deprecated_penalty_argument(penalty):
    forward = _script("train_forward_risk")
    rng = np.random.default_rng(0)
    frame = pd.DataFrame({"a": rng.random(400), "b": rng.random(400)})
    target = (frame["a"] + 0.2 * rng.random(400) > 0.6).astype(int).to_numpy()
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        fitted = forward.fit_scorecard(frame, target, ["a", "b"], C=0.5, penalty=penalty)
    assert fitted["penalty"] == penalty
    assert fitted["lr"].coef_.shape == (1, 2)


def test_training_scripts_do_not_pass_penalty_to_sklearn():
    for name in ("train_current_state.py", "train_forward_risk.py"):
        source = (ROOT / "scripts" / name).read_text()
        assert "penalty=penalty, solver" not in source and 'penalty="l2", class_weight' not in source
        assert "l1_ratio=" in source
