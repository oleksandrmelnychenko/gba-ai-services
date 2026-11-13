"""Source-history boundary contract across API, SQL signals, caches, and model training."""
from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.core import history
from app.core.config import Settings
from app.data import signals_repository as sig
from app.ml import dataset as ds
from app.ml import score_task, train


def test_source_history_default_and_boundary_windows():
    assert Settings.model_fields["source_history_start_date"].default == date(2025, 1, 1)
    assert history.require_as_of("2025-01-01") == date(2025, 1, 1)

    with pytest.raises(history.SourceHistoryBoundaryError):
        history.require_as_of("2024-12-31")

    partial = history.rolling_days("2025-06-01", 365)
    assert partial.metadata() == {
        "source_history_start": "2025-01-01",
        "effective_start": "2025-01-01",
        "history_complete": False,
    }

    complete = history.training_window("2026-01-01", 365)
    assert complete.effective_start == date(2025, 1, 1)
    assert complete.history_complete is True

    with pytest.raises(history.SourceHistoryBoundaryError, match="partial history"):
        history.training_window("2025-12-31", 365)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/generate/manager/1"),
        ("post", "/cockpit/generate"),
        ("get", "/cockpit/target"),
    ],
)
def test_api_rejects_pre_floor_as_of_with_canonical_422(monkeypatch, method, path):
    monkeypatch.setattr(main, "_resolve_manager", lambda manager_net_uid: 1)
    client = TestClient(main.app)
    request = getattr(client, method)
    response = request(
        path,
        params={
            "as_of_date": "2024-12-31",
            **({"manager_net_uid": "11111111-1111-1111-1111-111111111111"}
               if path.startswith("/cockpit/") else {}),
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "as_of_before_source_history_start"


def test_ubiquity_is_deterministic_and_clamped(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sig,
        "query",
        lambda sql, params=None: calls.append((sql, dict(params or {}))) or [],
    )
    sig.ubiquitous_product_ids.cache_clear()

    assert sig.ubiquitous_product_ids(0.2, "2025-06-01") == frozenset()
    assert sig.ubiquitous_product_ids(0.2, "2025-06-01") == frozenset()
    assert len(calls) == 1
    sql, params = calls[0]
    assert "GETDATE()" not in sql
    assert "DATEADD(month" not in sql
    assert params == {"pct": 0.2, "start": "2025-01-01", "asof": "2025-06-01"}

    sig.ubiquitous_product_ids.cache_clear()


def test_source_readiness_exposes_canonical_history_metadata(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(sig, "synthetic_product_ids", lambda: frozenset({1}))
    monkeypatch.setattr(
        sig,
        "_source_readiness_state",
        {
            "at": 0.0,
            "max_lag_days": None,
            "source_history_start": None,
            "value": None,
        },
    )
    monkeypatch.setattr(
        sig,
        "query",
        lambda sql, params=None: calls.append((sql, dict(params or {})))
        or [{"latest_sale_at": datetime.now(), "manager_count": 1}],
    )

    result = sig.source_readiness(7)

    assert result["source_history_start"] == "2025-01-01"
    assert result["effective_start"] == "2025-01-01"
    assert result["history_complete"] is True
    assert calls[0][1]["source_start"] == "2025-01-01"
    assert "o.Created >= :source_start AND o.Created < :asof" in calls[0][0]


def test_repository_rolling_and_factual_sql_windows(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sig,
        "query",
        lambda sql, params=None: calls.append((sql, dict(params or {}))) or [],
    )
    monkeypatch.setattr(sig, "_excluded", lambda as_of: frozenset())

    sig.new_clients_for_manager(7, "2025-02-01")
    sig.active_clients_for_manager(7, "2025-02-01")
    sig.reorder_candidates_for_manager(7, "2025-02-01")
    sig.churn_candidates_for_manager(7, "2025-06-01")
    sig.client_features([11], "2025-02-01")

    by_marker = {
        "new": calls[0],
        "active": calls[1],
        "reorder": calls[2],
        "churn": calls[3],
        "features": calls[4],
    }
    assert by_marker["new"][1]["created_start"] == "2025-01-01"
    assert by_marker["new"][1]["source_start"] == "2025-01-01"
    assert by_marker["active"][1]["start"] == "2025-01-01"
    assert by_marker["reorder"][1]["source_start"] == "2025-01-01"
    assert by_marker["churn"][1]["start"] == "2025-01-01"
    assert by_marker["features"][1]["start"] == "2025-01-01"
    for sql, params in calls:
        assert params["asof"] >= "2025-01-01"
        assert "GETDATE()" not in sql


def test_monthly_series_clamp_since_without_inventing_dense_months(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def _query(sql, params=None):
        calls.append((sql, dict(params or {})))
        return [{"ym": "2025-01", "registered_at": "2025-01-14T22:00:00", "amt": "12.34",
                 "missing_conversion": 0, "missing_date": 0}]

    monkeypatch.setattr(sig, "query", _query)
    monkeypatch.setattr(sig, "_excluded", lambda as_of: frozenset())

    shipped = sig.monthly_shipped(7, "2024-01-01", "2025-03-01")
    paid = sig.monthly_paid(7, "2024-01-01", "2025-03-01")

    assert list(shipped) == ["2025-01"]
    assert list(paid) == ["2025-01"]
    assert calls[0][1]["source_start"] == "2025-01-01"
    assert calls[1][1]["since_utc"] == "2024-12-31T22:00:00"
    assert calls[0][1]["asof"] == "2025-03-01"
    assert calls[1][1]["asof_utc"] == "2025-02-28T22:00:00"


def test_dataset_mirrors_rolling_and_factual_windows(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sig,
        "query",
        lambda sql, params=None: calls.append((sql, dict(params or {}))) or [],
    )
    monkeypatch.setattr(ds, "_excluded_pids", lambda asof: set())

    ds.reorder_candidates("2025-02-01")
    ds.churn_candidates("2025-06-01")
    ds.active_clients("2025-02-01")

    assert calls[0][1]["source_start"] == "2025-01-01"
    assert calls[1][1]["start"] == "2025-01-01"
    assert calls[2][1]["start"] == "2025-01-01"
    for sql, params in calls:
        assert params["asof"] >= "2025-01-01"
        assert "GETDATE()" not in sql
        assert "c.MainManagerID IS NOT NULL" in sql and "mid" not in params


_CLIENT = {"client_id": 77, "client_net_uid": "AB-77"}


def _reco(monkeypatch, tmp_path, answers):
    monkeypatch.setattr(ds, "_RECO_CACHE_DIR", str(tmp_path))
    calls: list[tuple] = []
    failures = {"n": 0}

    def recommend(*args, **kwargs):
        calls.append((args, kwargs))
        answer = answers[min(len(calls), len(answers)) - 1]
        if answer is None:
            failures["n"] += 1
            return []
        return answer

    monkeypatch.setattr(ds.reco_client, "recommend", recommend)
    monkeypatch.setattr(ds.reco_client, "recommend_failures", lambda: failures["n"])
    return calls, tmp_path / "source_2025-01-01"


def test_reco_cache_rejects_legacy_payload(monkeypatch, tmp_path):
    fresh = [{"product_id": 2, "source": "discovery"}]
    calls, cache_dir = _reco(monkeypatch, tmp_path, [fresh])
    cache_dir.mkdir()
    (cache_dir / "2026-01-01_77.json").write_text(
        json.dumps({"source_history_start": "2025-01-01", "as_of": "2026-01-01",
                    "recommendations": [{"product_id": 1, "source": "discovery"}]}))

    assert ds._reco_raw_cached(_CLIENT, "2026-01-01", 8) == (fresh, True)
    assert len(calls) == 1
    assert json.loads((cache_dir / "2026-01-01_ab-77.json").read_text()) == {
        "source_history_start": "2025-01-01",
        "as_of": "2026-01-01",
        "client_id": 77,
        "client_net_uid": "ab-77",
        "recommendations": fresh,
    }
    assert ds._reco_raw_cached(_CLIENT, "2026-01-01", 8) == (fresh, True)
    assert len(calls) == 1


def test_reco_cache_entry_of_a_reminted_client_id_is_not_joined(monkeypatch, tmp_path):
    fresh = [{"product_id": 2, "source": "discovery"}]
    calls, cache_dir = _reco(monkeypatch, tmp_path, [fresh])
    cache_dir.mkdir()
    (cache_dir / "2026-01-01_ab-77.json").write_text(
        json.dumps({"source_history_start": "2025-01-01", "as_of": "2026-01-01",
                    "client_id": 5, "client_net_uid": "ab-77",
                    "recommendations": [{"product_id": 1, "source": "discovery"}]}))
    assert ds._reco_raw_cached(_CLIENT, "2026-01-01", 8) == (fresh, True)
    assert len(calls) == 1


def test_reco_empty_answer_is_cached_and_a_failed_call_is_retried_then_left_uncached(
        monkeypatch, tmp_path):
    calls, cache_dir = _reco(monkeypatch, tmp_path, [[]])
    assert ds._reco_raw_cached(_CLIENT, "2026-01-01", 8) == ([], True)
    assert ds._reco_raw_cached(_CLIENT, "2026-01-01", 8) == ([], True)
    assert len(calls) == 1

    calls, cache_dir = _reco(monkeypatch, tmp_path / "failed", [None, None])
    assert ds._reco_raw_cached(_CLIENT, "2026-01-01", 8) == ([], False)
    assert len(calls) == 2
    assert not list(cache_dir.glob("*.json"))

    recovered = [{"product_id": 3, "source": "discovery"}]
    calls, cache_dir = _reco(monkeypatch, tmp_path / "flaky", [None, recovered])
    assert ds._reco_raw_cached(_CLIENT, "2026-01-01", 8) == (recovered, True)
    assert len(calls) == 2


def test_reco_cache_prune_drops_entries_no_current_client_can_join(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "_RECO_CACHE_DIR", str(tmp_path))
    cache_dir = tmp_path / "source_2025-01-01"
    cache_dir.mkdir()
    for name in ("2026-01-01_511776.json", "2026-01-01_ab-77.json", "2026-02-01_gone-1.json"):
        (cache_dir / name).write_text("{}")
    assert ds.prune_reco_cache({"ab-77"}) == 2
    assert [path.name for path in cache_dir.glob("*.json")] == ["2026-01-01_ab-77.json"]


def test_cross_sell_candidates_report_reco_coverage(monkeypatch, tmp_path):
    answers = {1: [{"product_id": 9, "source": "discovery", "score": 0.4567}], 2: None, 3: []}
    monkeypatch.setattr(ds.reco_client, "is_healthy", lambda: True)
    monkeypatch.setattr(ds, "active_clients", lambda asof: [
        {"client_id": cid, "client_net_uid": f"u-{cid}"} for cid in answers])
    monkeypatch.setattr(ds, "_excluded_pids", lambda asof: set())
    monkeypatch.setattr(
        ds, "_reco_raw_cached",
        lambda client, asof, timeout: (answers[client["client_id"]] or [],
                                       answers[client["client_id"]] is not None))
    rows = ds.cross_sell_candidates("2026-01-01", max_workers=1)
    assert rows == [{"client_id": 1, "reco_product_ids": [9], "top_score": 0.457, "candidates": 1}]
    assert ds.RECO_COVERAGE["2026-01-01"] == {"asked": 3, "failed": 1, "offline": False}
    monkeypatch.setattr(ds.reco_client, "is_healthy", lambda: False)
    assert ds.cross_sell_candidates("2026-02-01") == []
    assert ds.RECO_COVERAGE["2026-02-01"]["offline"] is True


def _history_frame(**overrides) -> pd.DataFrame:
    row = {
        "vintage": "2026-01-01",
        "source_history_start": "2025-01-01",
        "effective_start": "2025-01-01",
        "history_complete": True,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_training_dataset_requires_complete_floor_aware_vintages():
    assert train.validate_dataset_history(_history_frame()) == ["2026-01-01"]

    with pytest.raises(ValueError, match="partial-history"):
        train.validate_dataset_history(_history_frame(history_complete=False))
    with pytest.raises(ValueError, match="partial-history"):
        train.validate_dataset_history(_history_frame(history_complete=1))
    with pytest.raises(ValueError, match="source_history_start"):
        train.validate_dataset_history(_history_frame(source_history_start="2024-01-01"))
    with pytest.raises(ValueError, match="effective_start mismatch"):
        train.validate_dataset_history(_history_frame(effective_start="2025-01-02"))
    with pytest.raises(history.SourceHistoryBoundaryError):
        train.validate_dataset_history(
            _history_frame(vintage="2025-12-31", effective_start="2025-01-01")
        )


def test_model_metadata_rejects_legacy_partial_and_duplicate_vintages(monkeypatch, tmp_path):
    (tmp_path / "propensity_model.joblib").write_bytes(b"")
    monkeypatch.setattr(score_task, "ART", tmp_path)
    monkeypatch.setattr(score_task, "_meta", lambda: {})
    legacy = score_task.model_compatibility()
    assert legacy["model_compatible"] is False
    assert "model_source_history_mismatch" in legacy["model_reasons"]

    valid_meta = {
        "source_history_start": "2025-01-01",
        "training_window_days": 365,
        "training_vintages": ["2026-01-01", "2026-02-01"],
    }
    monkeypatch.setattr(score_task, "_meta", lambda: valid_meta)
    assert score_task.model_compatibility()["model_compatible"] is True

    monkeypatch.setattr(
        score_task,
        "_meta",
        lambda: {**valid_meta, "training_vintages": ["2026-01-01", "2026-01-01"]},
    )
    duplicate = score_task.model_compatibility()
    assert duplicate["model_compatible"] is False
    assert "model_training_vintages_invalid" in duplicate["model_reasons"]


def test_model_compatibility_rejects_unreadable_metadata_and_missing_artifact(
    monkeypatch, tmp_path
):
    def _unreadable():
        raise ValueError("broken json")

    monkeypatch.setattr(score_task, "_meta", _unreadable)
    unreadable = score_task.model_compatibility()
    assert unreadable["model_compatible"] is False
    assert "model_metadata_unreadable" in unreadable["model_reasons"]

    monkeypatch.setattr(
        score_task,
        "_meta",
        lambda: {
            "source_history_start": "2025-01-01",
            "training_window_days": 365,
            "training_vintages": ["2026-01-01"],
        },
    )
    monkeypatch.setattr(score_task, "ART", tmp_path)
    missing = score_task.model_compatibility()
    assert missing["model_compatible"] is False
    assert "model_artifact_missing" in missing["model_reasons"]


def test_health_degrades_when_only_model_is_history_incompatible(monkeypatch):
    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def exec_driver_sql(self, sql):
            return 1

    class _Engine:
        def connect(self):
            return _Connection()

    monkeypatch.setattr(main, "get_engine", lambda: _Engine())
    monkeypatch.setattr(main.mongo, "ping", lambda: True)
    monkeypatch.setattr(
        main.signals_repository,
        "source_readiness",
        lambda max_lag_days: {
            "source_ready": True,
            "source_reasons": [],
            "latest_sale_at": "2026-07-25T10:00:00",
            "manager_count": 1,
            "synthetic_product_count": 1,
        },
    )
    monkeypatch.setattr(
        main.mongo,
        "generation_readiness",
        lambda max_lag_hours: {
            "generation_ready": True,
            "generation_reasons": [],
            "last_generation_at": "2026-07-25T10:00:00",
            "last_generation_managers": 1,
            "last_generation_ok": 1,
            "last_generation_failed": 0,
            "task_count": 1,
            "active_task_count": 1,
            "latest_task_refresh_at": "2026-07-25T10:00:00",
        },
    )
    monkeypatch.setattr(
        score_task,
        "model_compatibility",
        lambda: {
            "model_compatible": False,
            "model_reasons": ["model_source_history_mismatch"],
            "model_source_history_start": None,
            "model_training_window_days": None,
            "model_training_vintages": [],
        },
    )

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["business_ready"] is False
    assert response.json()["model_compatible"] is False
    assert response.json()["model_reasons"] == ["model_source_history_mismatch"]
