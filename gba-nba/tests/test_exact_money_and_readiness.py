from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import mongomock
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import main
from app.data import mongo
from app.data import signals_repository as signals
from app.domain.models import Outcome
from app.services import targets


def _headers() -> dict[str, str]:
    return (
        {"X-Internal-Api-Key": main.settings.internal_api_key}
        if main.settings.internal_api_key
        else {}
    )


def test_accounting_money_rounds_half_up():
    assert Outcome(sold=True, amount="1.005").amount == 1.01
    dashboard = signals._debt_dashboard_from_rows(
        [
            {"overdue_amount": "1.005", "max_overdue_days": 5},
            {"overdue_amount": "2.005", "max_overdue_days": 5},
        ]
    )
    assert dashboard["value_at_risk_eur"] == 3.01
    assert dashboard["debt_aging"][0]["amount_eur"] == 3.01


def test_value_at_risk_reconciles_displayed_bucket_cents_exactly():
    dashboard = signals._debt_dashboard_from_rows(
        [
            {"overdue_amount": "1.005", "max_overdue_days": 5},
            {"overdue_amount": "2.005", "max_overdue_days": 40},
        ]
    )
    bucket_total = sum(
        (Decimal(str(row["amount_eur"])) for row in dashboard["debt_aging"]),
        Decimal("0"),
    )
    assert dashboard["value_at_risk_eur"] == 3.02
    assert Decimal(str(dashboard["value_at_risk_eur"])) == bucket_total


def test_status_input_rejects_fractional_cents_and_negative_money():
    with pytest.raises(ValidationError):
        main.CockpitStatusRequest(task_key="task", to="done", amount="1.005")
    with pytest.raises(ValidationError):
        main.CockpitStatusRequest(task_key="task", to="done", amount="-0.01")


def test_target_math_preserves_decimal_until_half_up_boundary():
    metric = targets._metric(
        {"2026-03": "1.00", "2026-04": "1.01"},
        current_month="2026-05",
        mtd="0.00",
        wd=2,
        wd_elapsed=1,
        n=2,
    )
    assert metric["target"] == 1.01
    assert metric["daily_pace"] == 0.51
    assert metric["expected_to_date"] == 0.51


def test_generation_readiness_requires_recent_successful_full_run(monkeypatch):
    client = mongomock.MongoClient(tz_aware=True)
    db = client["nba"]
    monkeypatch.setattr(mongo, "get_db", lambda: db)
    db.tasks.insert_one(
        {
            "task_key": "one",
            "status": "open",
            "updated_at": datetime.now(UTC),
        }
    )
    stats = {"managers": 2, "ok": 2, "failed": 0}
    mongo.record_generation_run(stats, full_run=True)

    readiness = mongo.generation_readiness(36)

    assert readiness["generation_ready"] is True
    assert readiness["task_count"] == 1
    assert readiness["active_task_count"] == 1


def test_health_cannot_be_green_without_generation_proof(monkeypatch):
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
            "manager_count": 5,
            "synthetic_product_count": 1,
        },
    )
    monkeypatch.setattr(
        main.mongo,
        "generation_readiness",
        lambda max_lag_hours: {
            "generation_ready": False,
            "generation_reasons": ["generation_run_missing"],
            "last_generation_at": None,
            "last_generation_managers": 0,
            "last_generation_ok": 0,
            "last_generation_failed": 0,
            "task_count": 0,
            "active_task_count": 0,
            "latest_task_refresh_at": None,
        },
    )

    response = TestClient(main.app).get("/health", headers=_headers())

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["business_ready"] is False
    assert response.json()["source_history_start"] == "2025-01-01"
    assert response.json()["effective_start"] == "2025-01-01"
    assert response.json()["history_complete"] is True
    assert "model_compatible" in response.json()
    assert response.json()["source_history_contract_ready"] is True
