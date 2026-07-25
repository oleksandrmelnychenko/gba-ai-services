from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api import main


class _Connection:
    def __enter__(self):
        return self

    def exec_driver_sql(self, _sql: str) -> None:
        return None

    def __exit__(self, *_args) -> None:
        return None


def _wire_operational_dependencies(monkeypatch, *, business_ready: bool) -> None:
    monkeypatch.setattr(
        main,
        "get_engine",
        lambda: SimpleNamespace(connect=lambda: _Connection()),
    )
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(main, "_synthetic_drift_ok_cached", lambda: True)
    monkeypatch.setattr(main, "_drift_summary_cached", lambda: None)
    monkeypatch.setattr(
        main,
        "_model_readiness",
        lambda: {
            "current_state": {"ready": True, "training_run_id": "current-test"},
            "forward_6m": {
                "ready": False,
                "status": "unavailable",
                "reason": "insufficient_unique_positive_clients",
            },
        },
    )
    from app.data import solvency_repository as repo

    monkeypatch.setattr(
        repo,
        "source_readiness",
        lambda _max_lag: {
            "business_ready": business_ready,
            "reasons": [] if business_ready else ["canonical_sales_stale"],
        },
    )


def test_health_fails_closed_on_stale_business_source(monkeypatch):
    _wire_operational_dependencies(monkeypatch, business_ready=False)
    response = TestClient(main.app).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["business_ready"] is False


def test_ready_is_200_only_for_complete_business_readiness(monkeypatch):
    _wire_operational_dependencies(monkeypatch, business_ready=True)
    health = TestClient(main.app).get("/health")
    assert health.json()["status"] == "degraded"
    assert health.json()["serving_ready"] is True
    assert health.json()["model_readiness"]["forward_6m"]["ready"] is False

    response = TestClient(main.app).get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["source_history_start"] == "2025-01-01"
    assert response.json()["source_history_contract_ready"] is True
    assert response.json()["source"]["source_history_start"] == "2025-01-01"
