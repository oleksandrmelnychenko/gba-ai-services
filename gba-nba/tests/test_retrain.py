"""Tests for the gated retrain harness (scripts/retrain.py) and the live-label union path.

Covers, without touching the real DB / a multi-minute train:
  * rolling_snapshots — the as-of window only includes fully-labelled (>= H_DAYS old) vintages.
  * the AUC gate decision (floor AND no-regression-beyond-epsilon).
  * backup / restore round-trips the production artifact set.
  * live_labels() shapes manager-logged Mongo outcomes into the training-row schema (mongomock).
  * end-to-end retrain with the heavy steps mocked: PASS keeps artifacts, FAIL restores the backup.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path

import mongomock
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_retrain():
    spec = importlib.util.spec_from_file_location("_retrain_mod", ROOT / "scripts" / "retrain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


retrain = _load_retrain()


# ----------------------------------------------------------------------- rolling snapshots
def test_rolling_snapshots_only_fully_labelled_vintages():
    today = dt.date(2026, 6, 26)
    snaps = retrain.rolling_snapshots(today, n=9, h_days=60)
    assert len(snaps) == 9
    assert snaps == sorted(snaps)  # ascending, oldest first
    # latest usable vintage = most recent month-start >= 60d before today.
    # 2026-06-26 - 60d = 2026-04-27 -> month start 2026-04-01.
    assert snaps[-1] == "2026-04-01"
    assert snaps[0] == "2025-08-01"
    # every vintage is at least h_days in the past (label window complete)
    for s in snaps:
        assert dt.date.fromisoformat(s) <= today - dt.timedelta(days=60)


def test_rolling_snapshots_rolls_forward_with_today():
    a = retrain.rolling_snapshots(dt.date(2026, 6, 26), n=4, h_days=60)
    b = retrain.rolling_snapshots(dt.date(2026, 9, 26), n=4, h_days=60)
    assert b[-1] > a[-1]  # window advances as "now" advances


# ----------------------------------------------------------------------- AUC gate decision
def _gate(new_auc, old_auc, floor, eps):
    """Mirror the gate logic in retrain.main for a focused unit check."""
    floor_ok = new_auc >= floor
    regress_ok = old_auc is None or new_auc >= old_auc - eps
    return floor_ok and regress_ok


def test_gate_passes_when_above_floor_and_not_regressed():
    assert _gate(0.72, 0.73, floor=0.68, eps=0.01) is True   # tiny dip within eps
    assert _gate(0.75, 0.70, floor=0.68, eps=0.01) is True   # improvement
    assert _gate(0.68, None, floor=0.68, eps=0.01) is True   # exactly floor, no prior


def test_gate_fails_below_floor():
    assert _gate(0.65, 0.70, floor=0.68, eps=0.01) is False


def test_gate_fails_on_regression_beyond_epsilon():
    assert _gate(0.70, 0.73, floor=0.68, eps=0.01) is False  # 0.70 < 0.73-0.01


# ----------------------------------------------------------------------- backup / restore
def test_backup_restore_roundtrip(tmp_path, monkeypatch):
    art = tmp_path / "artifacts"
    art.mkdir()
    monkeypatch.setattr(retrain, "ART", art)
    monkeypatch.setattr(retrain, "PARQUET", tmp_path / "ds.parquet")
    (art / "propensity_model.joblib").write_bytes(b"MODEL_V1")
    (art / "metrics.json").write_text('{"production_model":"hgb","oot":{"hgb":{"auc":0.73}}}')
    (art / "model_meta.json").write_text("{}")
    (art / "MODEL_CARD.md").write_text("card")

    backup = retrain._backup("ts1")
    assert (backup / "propensity_model.joblib").read_bytes() == b"MODEL_V1"

    # simulate a bad retrain overwriting the live model, then restore
    (art / "propensity_model.joblib").write_bytes(b"MODEL_BROKEN")
    retrain._restore(backup)
    assert (art / "propensity_model.joblib").read_bytes() == b"MODEL_V1"


def test_pooled_oot_auc_reads_production_model(tmp_path):
    p = tmp_path / "metrics.json"
    p.write_text(json.dumps({"production_model": "logit",
                             "oot": {"hgb": {"auc": 0.9}, "logit": {"auc": 0.71}}}))
    assert retrain._pooled_oot_auc(p) == pytest.approx(0.71)


def test_model_card_consistency_guard_checks_headline_metrics(tmp_path):
    metrics = tmp_path / "metrics.json"
    card = tmp_path / "MODEL_CARD.md"
    metrics.write_text(json.dumps({
        "n_rows": 63378,
        "n_clients": 862,
        "base_rate": 0.29216762914576033,
        "production_model": "hgb",
        "cv": {"hgb": {"auc": 0.6976025109558861}},
        "oot": {"hgb": {"auc": 0.7044029846834037}},
        "benchmark": {"oot": {"auc_old": 0.5490260360611878}},
    }))

    card.write_text("rows 35,943 clients 725 base 26.6% AUC 0.727 old 0.566")
    assert retrain._model_card_consistency_errors(metrics, card)

    card.write_text("rows 63,378 clients 862 base 29.2% CV 0.698 OOT 0.704 old 0.549")
    assert retrain._model_card_consistency_errors(metrics, card) == []


# ----------------------------------------------------------------------- live-label union (Mongo)
@pytest.fixture
def patched_mongo(monkeypatch):
    client = mongomock.MongoClient()
    db = client["gba_nba_test"]
    from app.data import mongo as m
    monkeypatch.setattr(m, "tasks", lambda: db["tasks"])
    return db


def test_live_labels_empty_when_no_outcomes(patched_mongo):
    from app.ml import dataset as ds
    df = ds.live_labels()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0


def test_live_labels_shapes_terminal_outcomes(patched_mongo):
    from app.ml import dataset as ds
    patched_mongo["tasks"].insert_many([
        # a SOLD debt_followup terminal task -> label 1, sig_ columns mapped
        {"task_key": "k1", "status": "done", "task_type": "debt_followup", "client_id": 10,
         "priority": 80.0, "outcome": {"sold": True, "amount": 500.0},
         "signals": {"overdue_amount": 1200.0, "days_past_terms": 30, "max_overdue_days": 40,
                     "debt_lines": 2, "monetary": 9000.0, "recency_days": 5, "order_count": 12}},
        # a NOT-SOLD cross_sell -> label 0
        {"task_key": "k2", "status": "done", "task_type": "cross_sell", "client_id": 11,
         "priority": 30.0, "outcome": {"sold": False},
         "signals": {"top_score": 0.42, "candidates": 4, "monetary": 4000.0,
                     "recency_days": 8, "order_count": 6}},
        # dismissed with no outcome -> excluded (no label)
        {"task_key": "k3", "status": "dismissed", "task_type": "reorder_due", "client_id": 12,
         "signals": {"elapsed_days": 20}},
        # active task -> excluded
        {"task_key": "k4", "status": "open", "task_type": "reorder_due", "client_id": 13,
         "outcome": {"sold": True}, "signals": {"elapsed_days": 10}},
        # new_client_activation -> out of model scope, excluded
        {"task_key": "k5", "status": "done", "task_type": "new_client_activation", "client_id": 14,
         "outcome": {"sold": True}, "signals": {}},
    ])
    df = ds.live_labels()
    assert len(df) == 2
    by_client = {int(r.client_id): r for r in df.itertuples()}

    debt = by_client[10]
    assert debt.task_type == "debt_followup"
    assert debt.label == 1
    assert debt.is_debt_followup == 1 and debt.is_cross_sell == 0
    assert debt.sig_overdue_amount == 1200.0 and debt.sig_days_past_terms == 30.0
    assert debt.monetary == 9000.0 and debt.is_live == 1

    cross = by_client[11]
    assert cross.label == 0
    assert cross.is_cross_sell == 1
    assert cross.sig_top_score == pytest.approx(0.42)


def test_live_labels_unionable_with_backfill_schema(patched_mongo):
    """A live row must carry every column a backfill row has (so pd.concat aligns)."""
    from app.ml import dataset as ds
    patched_mongo["tasks"].insert_one(
        {"task_key": "k1", "status": "done", "task_type": "churn_winback", "client_id": 20,
         "priority": 50.0, "outcome": {"sold": True},
         "signals": {"drop_ratio": 0.2, "silence_days": 95, "recent_orders": 1, "prior_orders": 8,
                     "monetary": 7000.0, "recency_days": 95, "order_count": 8}})
    live = ds.live_labels()
    # the exact column set a backfill row carries (base() schema + label/old_priority/vintage),
    # so a live frame is row-for-row unionable via pd.concat.
    backfill_cols = {
        "vintage", "task_type", "client_id", "monetary", "recency_days", "order_count",
        "is_reorder_due", "is_debt_followup", "is_churn_winback", "is_cross_sell",
        "sig_overdue_amount", "sig_days_past_terms", "sig_max_overdue_days", "sig_debt_lines",
        "sig_elapsed_days", "sig_cycle_days", "sig_overdue_ratio", "sig_n_orders",
        "sig_drop_ratio", "sig_silence_days", "sig_recent_orders", "sig_prior_orders",
        "sig_top_score", "sig_reco_candidates", "label", "old_priority",
    }
    assert backfill_cols.issubset(set(live.columns))


# ----------------------------------------------------------------------- end-to-end (mocked heavy steps)
def test_main_swaps_on_pass_restores_on_fail(tmp_path, monkeypatch):
    """Drive main() with rebuild+train mocked: gate PASS keeps the new model, FAIL restores backup."""
    art = tmp_path / "artifacts"
    art.mkdir()
    monkeypatch.setattr(retrain, "ART", art)
    monkeypatch.setattr(retrain, "PARQUET", tmp_path / "ds.parquet")
    (retrain.PARQUET).write_bytes(b"OLD_DS")
    # the OLD production model + its metrics (old pooled OOT AUC = 0.73)
    (art / "propensity_model.joblib").write_bytes(b"MODEL_OLD")
    (art / "metrics.json").write_text(json.dumps(
        {"production_model": "hgb", "oot": {"hgb": {"auc": 0.73}},
         "oot_per_type": {"hgb": {"cross_sell": {"auc": 0.74}}}}))

    def fake_rebuild(snapshots, include_live):
        retrain.PARQUET.write_bytes(b"NEW_DS")
        return {"backfill_rows": 100, "live_rows": 0, "cross_sell_n": 40,
                "cross_sell_base_rate": 0.09}

    monkeypatch.setattr(retrain, "_rebuild_dataset", fake_rebuild)

    def fake_train_writing(new_auc):
        def _train():
            (art / "propensity_model.joblib").write_bytes(b"MODEL_NEW")
            (art / "metrics.json").write_text(json.dumps({
                "n_rows": 100,
                "n_clients": 10,
                "base_rate": 0.2,
                "production_model": "hgb",
                "cv": {"hgb": {"auc": 0.7}},
                "oot": {"hgb": {"auc": new_auc}},
                "benchmark": {"oot": {"auc_old": 0.5}},
                "oot_per_type": {"hgb": {"cross_sell": {"auc": 0.70}}},
            }))
            (art / "MODEL_CARD.md").write_text(
                f"rows 100 clients 10 base 20.0% CV 0.700 OOT {new_auc:.3f} old 0.500")
        return _train

    import sys
    # --- PASS: new AUC 0.74 >= floor 0.68 and >= 0.73 - 0.01 ---
    monkeypatch.setattr(retrain, "_retrain_in_place", fake_train_writing(0.74))
    monkeypatch.setattr(sys, "argv", ["retrain.py", "--auc-floor", "0.68"])
    rc = retrain.main()
    assert rc == 0
    assert (art / "propensity_model.joblib").read_bytes() == b"MODEL_NEW"

    # reset to OLD for the FAIL case
    (art / "propensity_model.joblib").write_bytes(b"MODEL_OLD")
    (art / "metrics.json").write_text(json.dumps(
        {"production_model": "hgb", "oot": {"hgb": {"auc": 0.73}},
         "oot_per_type": {"hgb": {"cross_sell": {"auc": 0.74}}}}))
    retrain.PARQUET.write_bytes(b"OLD_DS")

    # --- FAIL: new AUC 0.60 < floor 0.68 -> abort + restore the OLD model ---
    monkeypatch.setattr(retrain, "_retrain_in_place", fake_train_writing(0.60))
    monkeypatch.setattr(sys, "argv", ["retrain.py", "--auc-floor", "0.68"])
    rc = retrain.main()
    assert rc == 1
    assert (art / "propensity_model.joblib").read_bytes() == b"MODEL_OLD"  # restored
    # metrics restored to the old AUC too
    assert retrain._pooled_oot_auc(art / "metrics.json") == pytest.approx(0.73)
