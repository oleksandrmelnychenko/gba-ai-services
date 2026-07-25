"""Source-history floor contracts for pricing behavioral data."""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime

import pytest

from app.core.config import Settings
from app.core.history import (
    history_contract_fingerprint,
    history_metadata,
    model_contract_fingerprint,
    subtract_calendar_months,
    trailing_month_history_window,
)
from app.data import pricing_repository as repo
from scripts import elasticity_backtest


def test_source_history_setting_defaults_and_accepts_iso_override():
    default = Settings(_env_file=None)
    overridden = Settings(_env_file=None, source_history_start_date="2025-02-03")
    assert default.source_history_start_date == date(2025, 1, 1)
    assert overridden.source_history_start_date == date(2025, 2, 3)


def test_trailing_window_clamps_and_reports_partial_coverage():
    window = trailing_month_history_window("2025-06-15", 12, "2025-01-01")
    metadata = history_metadata(
        window,
        model_version="pricing-ab-v2",
        trailing_window_months=12,
    )
    assert window.requested_start == date(2024, 6, 15)
    assert window.effective_start == date(2025, 1, 1)
    assert metadata["source_history_start"] == "2025-01-01"
    assert metadata["requested_start"] == "2024-06-15"
    assert metadata["effective_start"] == "2025-01-01"
    assert metadata["history_complete"] is False
    assert metadata["history_fingerprint"]
    assert metadata["model_fingerprint"].startswith("pricing-ab-v2-")


def test_trailing_window_preserves_sql_server_end_of_month_semantics():
    assert subtract_calendar_months("2025-03-31", 1) == date(2025, 2, 28)
    complete = trailing_month_history_window("2026-06-15", 12, "2025-01-01")
    assert complete.requested_start == date(2025, 6, 15)
    assert complete.effective_start == date(2025, 6, 15)
    assert complete.history_complete is True


def test_pre_floor_as_of_is_rejected_by_history_contract():
    with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
        trailing_month_history_window("2024-12-31", 12, "2025-01-01")


def test_history_and_model_fingerprints_change_with_floor_or_window():
    first_history = history_contract_fingerprint("2025-01-01")
    second_history = history_contract_fingerprint("2025-02-01")
    assert first_history != second_history
    assert model_contract_fingerprint("pricing-ab-v2", "2025-01-01", 12) != (
        model_contract_fingerprint("pricing-ab-v2", "2025-02-01", 12)
    )
    assert model_contract_fingerprint("pricing-ab-v2", "2025-01-01", 12) != (
        model_contract_fingerprint("pricing-ab-v2", "2025-01-01", 6)
    )


def test_all_behavioral_repository_sql_is_clamped(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_query(sql, params=None):
        captured.append((sql, params or {}))
        return []

    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 999)

    repo.client_world_fallback_baseline(7, 11, "2025-06-15", 12)
    repo.peer_band(7, "2025-06-15", 12, "2025-06-15")
    repo.product_line_count(7, "2025-06-15", 12)
    repo.product_panel(7, "2025-06-15", 12)
    repo.group_panel(106, "2025-06-15", 12)

    assert len(captured) == 5
    for sql, params in captured:
        assert "Created >= :source_history_start" in sql
        assert params["source_history_start"] == "2025-01-01"
        assert params["asof"] == "2025-06-15"
        assert params["neg_months"] == -12

    group_sql = captured[-1][0]
    assert "JOIN dbo.Sale s2 ON s2.OrderID = o2.ID" in group_sql
    assert "s2.Created >= :source_history_start" in group_sql
    assert group_sql.count(">= :source_history_start") == 2


def test_repository_rejects_pre_floor_as_of_before_query(monkeypatch):
    def unexpected_query(*_args, **_kwargs):
        raise AssertionError("pre-floor requests must fail before SQL")

    monkeypatch.setattr(repo, "query", unexpected_query)
    calls = (
        lambda: repo.client_world_fallback_baseline(7, 11, "2024-12-31", 12),
        lambda: repo.peer_band(7, "2024-12-31", 12, "2024-12-31"),
        lambda: repo.product_line_count(7, "2024-12-31", 12),
        lambda: repo.product_panel(7, "2024-12-31", 12),
        lambda: repo.group_panel(106, "2024-12-31", 12),
    )
    for call in calls:
        with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
            call()


def test_source_readiness_exposes_and_applies_history_floor(monkeypatch):
    captured = {}

    def fake_query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {
                "latest_sale_at": datetime.now(),
                "active_product_count": 1,
                "agreement_count": 1,
                "synthetic_product_count": 1,
            }
        ]

    monkeypatch.setattr(repo, "query", fake_query)
    monkeypatch.setattr(repo, "synthetic_product_id", lambda: 999)
    monkeypatch.setattr(repo, "_readiness_state", {})
    readiness = repo.source_readiness(31)

    assert "o.Created >= :source_history_start" in captured["sql"]
    assert captured["params"]["source_history_start"] == "2025-01-01"
    assert readiness["source_history_start"] == "2025-01-01"
    assert readiness["effective_start"]
    assert isinstance(readiness["history_complete"], bool)
    assert readiness["history_fingerprint"]
    assert readiness["model_fingerprint"]
    assert readiness["business_ready"] is True


def test_backtest_queries_obey_the_same_floor(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_query(sql, params=None):
        captured.append((sql, params or {}))
        return []

    monkeypatch.setattr(elasticity_backtest, "query", fake_query)
    assert elasticity_backtest._estimable_products("2025-06-15", 12, 100, 10) == []
    assert elasticity_backtest._pre_panel(7, "2025-06-15", 12) == []
    assert elasticity_backtest._period_aggregate(7, "2024-01-01", "2025-02-01") is None

    assert len(captured) == 3
    for sql, params in captured:
        assert "s.Created >= :source_history_start" in sql
        assert params["source_history_start"] == "2025-01-01"
    with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
        elasticity_backtest._history_query_params("2024-12-31", 12)


def test_backtest_rejects_pre_floor_as_of_before_data_access(monkeypatch, capsys):
    def unexpected_products(*_args, **_kwargs):
        raise AssertionError("pre-floor backtest must fail before SQL")

    monkeypatch.setattr(elasticity_backtest, "_estimable_products", unexpected_products)
    monkeypatch.setattr(
        elasticity_backtest.sys,
        "argv",
        ["elasticity_backtest.py", "2024-12-31"],
    )
    assert elasticity_backtest.main() == 2
    assert json.loads(capsys.readouterr().out)["error"] == ("as_of_date_before_source_history_start")


def test_backtest_report_discloses_effective_history(monkeypatch, capsys):
    monkeypatch.setattr(elasticity_backtest, "_estimable_products", lambda *_args: [])
    monkeypatch.setattr(
        elasticity_backtest.sys,
        "argv",
        ["elasticity_backtest.py", "2025-06-15", "12", "1", "1", "AB"],
    )
    assert elasticity_backtest.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source_history_start"] == "2025-01-01"
    assert report["requested_start"] == "2024-06-15"
    assert report["effective_start"] == "2025-01-01"
    assert report["history_complete"] is False


def test_current_state_pricing_queries_remain_explicitly_unclamped():
    current_state_functions = (
        repo.resolve_product,
        repo.resolve_client_agreement,
        repo.baseline_price,
        repo.base_list_price_and_markup,
        repo.is_promotional,
        repo.product_group_id,
        repo.active_group_discount,
        repo.unit_cost_eur,
        repo.segment_discount_distribution,
    )
    for function in current_state_functions:
        assert ":source_history_start" not in inspect.getsource(function)
