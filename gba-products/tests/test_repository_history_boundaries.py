from __future__ import annotations

import pytest

from app.data import signals_repository as sig


def test_rolling_day_sql_params_clamp_to_source_floor(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(sig, "query", lambda sql, params=None: captured.append((sql, params)) or [])
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    sig.sales_velocity("2025-01-11", 180)

    sql, params = captured[0]
    assert sig._SALES_HISTORY_WINDOW in sql
    assert params == {
        "asof": "2025-01-11",
        "source_history_start": "2025-01-01",
        "history_start": "2025-01-01",
        "synth": 999,
    }


def test_rolling_day_sql_params_use_requested_start_when_complete(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(sig, "query", lambda sql, params=None: captured.append(params) or [])

    sig.sold_product_ids("2026-07-25", 180)

    assert captured[0] == {
        "asof": "2026-07-25",
        "source_history_start": "2025-01-01",
        "history_start": "2026-01-26",
    }


def test_monthly_and_explicit_sales_queries_enforce_floor(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(sig, "query", lambda sql, params=None: captured.append((sql, params)) or [])
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    sig.monthly_units("2026-07-25", 24)
    sig.monthly_product_sales(42, "2024-08-01", "2026-07-25")

    assert captured[0][1]["history_start"] == "2025-01-01"
    assert captured[1][1] == {
        "asof": "2026-07-25",
        "source_history_start": "2025-01-01",
        "history_start": "2025-01-01",
        "product_id": 42,
        "synth": 999,
    }
    assert sig._SALES_HISTORY_WINDOW in captured[1][0]


def test_product_meta_is_point_in_time_and_floor_bounded(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(sig, "query", lambda sql, params=None: captured.append((sql, params)) or [])

    assert sig.product_meta([42], "2026-07-25") == {}

    sql, params = captured[0]
    assert "si.DateFrom >= :source_history_start" in sql
    assert "si.DateFrom < :asof" in sql
    assert "sou.FromDate >= :source_history_start" in sql
    assert "sou.FromDate < :asof" in sql
    assert params["source_history_start"] == "2025-01-01"
    assert params["asof"] == "2026-07-25"
    assert params["p0"] == 42


def test_repository_rejects_pre_floor_as_of_before_query(monkeypatch):
    monkeypatch.setattr(
        sig,
        "query",
        lambda *args: (_ for _ in ()).throw(AssertionError("query must not run")),
    )

    with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
        sig.regional_demand_summary("2024-12-31", 30)
    with pytest.raises(ValueError, match="as_of_date_before_source_history_start"):
        sig.product_meta([42], "2024-12-31")
