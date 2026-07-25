"""Source-history floor, effective denominator and API contract tests."""
from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.core.config import get_settings
from app.core.history import rolling_coverage
from app.data import cache
from app.data import cost_repository as cost_repo
from app.data import supply_repository as supply_repo
from app.domain.models import MODEL_VERSION
from app.services.classify import segmentation
from app.services.forecasting import demand
from app.services.replenishment import policy, worker


def _src(fn) -> str:
    return inspect.getsource(fn)


def test_configured_source_floor_and_partial_effective_windows():
    assert get_settings().source_history_start_date.isoformat() == "2025-01-01"

    boundary = rolling_coverage("2025-01-01", 120)
    assert boundary.effective_start.isoformat() == "2025-01-01"
    assert boundary.effective_history_days == 0
    assert boundary.history_complete is False

    partial = rolling_coverage("2025-01-11", 120)
    assert partial.effective_start.isoformat() == "2025-01-01"
    assert partial.effective_history_days == 10
    assert partial.history_complete is False

    complete = rolling_coverage("2025-05-01", 120)
    assert complete.effective_start.isoformat() == "2025-01-01"
    assert complete.effective_history_days == 120
    assert complete.history_complete is True


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/plan/producer",
            {"producer_id": 1, "as_of_date": "2024-12-31"},
        ),
        ("/plan/cart", {"as_of_date": "2024-12-31"}),
        ("/plan/charts", {"as_of_date": "2024-12-31"}),
    ],
)
def test_api_rejects_pre_floor_as_of_with_422(path, payload):
    client = TestClient(app)
    response = client.post(
        path,
        headers={"X-Internal-Api-Key": get_settings().internal_api_key},
        json=payload,
    )
    assert response.status_code == 422
    assert "2025-01-01" in response.text


def test_forecast_uses_effective_days_without_pre_floor_zero_days():
    rows = [
        {"d": "2025-01-02", "units": 4},
        {"d": "2025-01-06", "units": 6},
    ]

    result = demand.forecast_from_rows(
        1,
        rows,
        horizon_days=30,
        method="moving_avg",
        effective_history_days=10,
    )

    assert result.mean_daily == pytest.approx(1.0)
    assert result.forecast_units == pytest.approx(30.0)
    assert result.std_daily == pytest.approx(4.2**0.5)


def test_croston_first_interval_starts_at_the_effective_floor():
    rows = [
        {"d": "2025-01-02", "units": 1},
        {"d": "2025-01-06", "units": 1},
    ]

    result = demand.forecast_from_rows(
        1,
        rows,
        horizon_days=30,
        method="croston",
        effective_history_days=10,
        effective_start="2025-01-01",
    )

    # First interval is one real day (Jan 1 -> Jan 2), then four days.
    assert result.mean_daily == pytest.approx(1 / 1.3)


def test_xyz_dense_window_and_adi_use_only_effective_days():
    rows = [
        {"d": "2025-01-02", "units": 4},
        {"d": "2025-01-06", "units": 6},
    ]

    _xyz, _cv, adi = segmentation.xyz_from_daily(
        rows,
        "2025-01-11",
        effective_history_days=10,
    )

    assert adi == 5.0
    assert segmentation._window_months("2025-03-01", 59) == [
        "2025-01",
        "2025-02",
        "2025-03",
    ]


def test_policy_passes_effective_days_to_xyz_and_forecasting():
    source = _src(policy.build_plan)
    assert "segmentation.xyz_from_daily" in source
    assert "coverage.effective_history_days" in source
    assert "effective_history_days=coverage.effective_history_days" in source
    assert "effective_start=coverage.effective_start" in source


def test_rolling_and_full_history_sql_are_floor_bounded():
    rolling_sources = (
        supply_repo.product_daily_demand,
        supply_repo.product_daily_demand_bulk,
        supply_repo.products_for_producer,
        supply_repo.all_producers,
        supply_repo.procurement_source_readiness,
        supply_repo.all_products_revenue_eur,
        cost_repo._fetch_cost_rows,
        cost_repo.sale_prices_eur,
    )
    for fn in rolling_sources:
        source = _src(fn)
        assert "DATEADD(day, -" in source, fn.__name__
        assert ":history_start" in source, fn.__name__

    full_history_sources = (
        supply_repo.producer_lead_times,
        supply_repo.producer_agreement_currency,
        supply_repo.derive_moq_terms,
        supply_repo._on_order_chunk,
    )
    for fn in full_history_sources:
        source = _src(fn)
        assert ":history_start" in source, fn.__name__
        assert ":asof" in source, fn.__name__


def test_current_inventory_and_reservations_are_explicit_history_na():
    for fn in (supply_repo.on_hand, supply_repo.reserved):
        source = _src(fn)
        normalized = " ".join(source.split())
        assert "history-window filtering is not applicable" in normalized
        assert ":history_start" not in source
        assert ":asof" not in source


def test_readiness_metadata_and_fingerprint_include_effective_window(monkeypatch):
    captured = []

    def fake_query(statement, params):
        captured.append((statement, params))
        return [{}]

    monkeypatch.setattr(supply_repo, "query", fake_query)
    monkeypatch.setattr(supply_repo, "synthetic_product_id", lambda: 1)

    first = supply_repo.procurement_source_readiness("2025-01-11", 120)
    second = supply_repo.procurement_source_readiness("2025-01-12", 120)

    assert first["source_history_start"] == "2025-01-01"
    assert first["effective_start"] == "2025-01-01"
    assert first["effective_history_days"] == 10
    assert first["history_complete"] is False
    assert first["history_not_applicable"] == ["inventory", "reservations"]
    assert first["source_fingerprint"] != second["source_fingerprint"]
    assert all(params["history_start"] == "2025-01-01" for _, params in captured)


def test_canonical_cache_rejects_history_metadata_drift():
    coverage = rolling_coverage("2026-06-15", get_settings().history_days)
    payload = {
        "as_of_date": "2026-06-15",
        **coverage.as_metadata(),
        "history_not_applicable": ["inventory", "reservations"],
        "model_version": MODEL_VERSION,
        "item_count": 0,
        "total_item_count": 0,
        "is_truncated": False,
        "duplicate_supplier_options_removed": 0,
        "total_suggested_qty": 0.0,
        "total_cost_eur": 0.0,
        "priced_cost_eur": 0.0,
        "unpriced_item_count": 0,
        "items": [],
    }
    assert worker.canonical_cart_payload_is_ready(payload)
    assert not worker.canonical_cart_payload_is_ready(
        {**payload, "effective_history_days": 119}
    )


def test_cache_namespace_and_model_version_identify_the_floor():
    assert "v8-h20250101" in cache.make_key("cart", "all", "2026-06-15")
    assert "floor20250101" in MODEL_VERSION
