from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.services.forecasting import accuracy_gate, classify, demand, holdout, selection
from scripts import holdout_backtest


def test_holdout_scores_only_past_data_and_reports_wape_and_bias():
    series = [100.0] * 9 + [200.0]
    result = holdout.evaluate([series], origins=1, methods=("service", "mean_3", "naive_last"))
    block = result["by_class"]["overall"]
    assert result["scored_points"] == 1
    assert block["actual_eur"] == 200.0
    assert block["methods"]["naive_last"] == {"wape_pct": 50.0, "bias_pct": -50.0}
    assert block["methods"]["mean_3"] == {"wape_pct": 50.0, "bias_pct": -50.0}
    assert block["methods"]["service"] == {"wape_pct": 50.0, "bias_pct": -50.0}
    assert set(result["by_class"]) == {classify.SMOOTH, "overall"}


def test_holdout_counts_refused_series_revenue_separately():
    thin = [0.0, 0.0, 0.0, 50.0, 0.0, 150.0]
    rich = [100.0] * 6
    result = holdout.evaluate([thin, rich], origins=1)
    assert result["scored_points"] == 1
    assert result["total_actual_eur"] == 250.0
    assert result["refused_actual_eur"] == 150.0
    assert result["refused_actual_share_pct"] == 60.0


def test_holdout_service_matches_live_selection_and_legacy_matches_old_map():
    intermittent = [100.0, 0.0, 0.0, 100.0, 0.0, 0.0, 100.0]
    assert classify.classify(intermittent) == classify.INTERMITTENT
    assert holdout.service_forecast(intermittent, classify.INTERMITTENT) == demand.tsb_rate(
        intermittent, 0.5, 0.5
    )
    assert holdout.service_forecast(
        intermittent, classify.INTERMITTENT, scope=selection.SCOPE_PRODUCT
    ) == demand.tsb_rate(intermittent, 0.2, 0.3)
    assert holdout.service_forecast(
        intermittent, classify.INTERMITTENT, scope=selection.SCOPE_PAIR
    ) == demand.ewma_rate(intermittent, 0.4)
    assert holdout.tsb_rate is demand.tsb_rate
    assert holdout.legacy_forecast(intermittent, classify.INTERMITTENT) == demand.croston_rate(
        intermittent, 0.1, sba=True
    )
    erratic = [100.0, 800.0, 50.0, 900.0, 120.0, 700.0]
    assert holdout.service_forecast(erratic, classify.ERRATIC) == demand.ewma_rate(erratic, 0.3)
    assert holdout.legacy_forecast(erratic, classify.ERRATIC) == demand.moving_avg_rate(erratic)


def test_holdout_rejects_unknown_method():
    with pytest.raises(ValueError, match="unknown holdout methods"):
        holdout.evaluate([[1.0, 2.0]], methods=("nope",))


def test_tsb_rate_decays_demand_probability_through_zero_months():
    assert holdout.tsb_rate([100.0, 100.0, 100.0], 0.3, 0.2) == pytest.approx(100.0)
    assert holdout.tsb_rate([100.0, 0.0, 0.0], 0.3, 0.2) == pytest.approx(64.0)


def test_accuracy_gate_records_classes_and_never_raises(monkeypatch):
    months = [f"2025-{m:02d}" for m in range(1, 13)]
    client_rows = [{"cid": 1, "ym": ym, "eur": 100.0} for ym in months]
    product_rows = [{"pid": 7, "ym": ym, "eur": 40.0} for ym in months]
    monkeypatch.setattr(accuracy_gate.sig, "all_client_monthly_series", lambda as_of, months: client_rows)
    monkeypatch.setattr(accuracy_gate.sig, "all_product_monthly_series", lambda as_of, months: product_rows)

    result = accuracy_gate.run("2026-01-15")

    assert result["status"] == "ok"
    assert result["last_month"] == "2025-12"
    for scope in ("clients", "products"):
        overall = result[scope]["by_class"]["overall"]
        assert overall["methods"]["service"] == {"wape_pct": 0.0, "bias_pct": 0.0}
        assert result[scope]["beats_baseline"] is True
        assert result[scope]["series"] == 1

    def broken(as_of, months):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(accuracy_gate.sig, "all_client_monthly_series", broken)
    failed = accuracy_gate.run("2026-01-15")
    assert failed["status"] == "error"
    assert "db unavailable" in failed["error"]


def test_accuracy_gate_warns_when_service_loses_to_baseline():
    evaluation = {
        "by_class": {
            "overall": {
                "methods": {
                    "service": {"wape_pct": 60.0, "bias_pct": 1.0},
                    "mean_3": {"wape_pct": 50.0, "bias_pct": 1.0},
                }
            }
        }
    }
    assert accuracy_gate.summarize(evaluation)["beats_baseline"] is False


def test_accuracy_gate_names_the_classes_behind_the_baseline():
    def block(service: float, baseline: float) -> dict:
        return {
            "methods": {
                "service": {"wape_pct": service, "bias_pct": 0.0},
                "mean_3": {"wape_pct": baseline, "bias_pct": 0.0},
            }
        }

    evaluation = {
        "by_class": {
            "overall": block(50.0, 52.0),
            "smooth": block(40.0, 42.0),
            "intermittent": block(82.8, 82.1),
            "lumpy": block(118.2, 118.2),
        }
    }
    summary = accuracy_gate.summarize(evaluation)
    assert summary["beats_baseline"] is True
    assert summary["class_gap_points"] == {"smooth": -2.0, "intermittent": 0.7, "lumpy": 0.0}
    assert summary["classes_behind_baseline"] == ["intermittent"]
    empty = accuracy_gate.summarize({"by_class": {}})
    assert empty["class_gap_points"] == {} and empty["classes_behind_baseline"] == []


def test_holdout_script_loads_psv_and_month_range(tmp_path):
    path = tmp_path / "series.psv"
    path.write_text("C|1|2026-01|10.5|1\nP|2|2026-02|3|1\nC|1|2027-01|9|1\nX|1|2026-01|9|1\n")
    labels = holdout_backtest.month_range("2025-11", "2026-02")
    assert labels == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert holdout_backtest.load_psv(str(path), labels) == {
        "C": {1: {"2026-01": 10.5}},
        "P": {2: {"2026-02": 3.0}},
    }


@pytest.fixture
def _health_stubs(monkeypatch):
    calls = {"source": 0}

    def source(as_of, months):
        from datetime import UTC, datetime, timedelta

        calls["source"] += 1
        return {
            "source_schema_present": True,
            "canonical_row_count": 10,
            "history_row_count": 10,
            "history_product_count": 2,
            "history_client_count": 2,
            "latest_sale_at": datetime.now(UTC) - timedelta(hours=1),
            "invalid_value_row_count": 0,
        }

    monkeypatch.setattr(main, "_source_health_cached", None)
    monkeypatch.setattr(main, "_database_health", lambda: ("healthy", True))
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(main.sig, "sales_source_status", source)
    monkeypatch.setattr(
        main.sig,
        "synthetic_product_status",
        lambda: {"product_id": 999, "resolved": True, "source": "verified_database"},
    )
    return calls


def test_health_and_ready_share_one_source_aggregate_for_sixty_seconds(monkeypatch, _health_stubs):
    client = TestClient(main.app)
    headers = {"X-Internal-Api-Key": main.settings.internal_api_key}
    assert client.get("/health").json()["status"] == "healthy"
    assert client.get("/ready", headers=headers).status_code == 200
    assert client.get("/health").json()["data"]["source_ready"] is True
    assert _health_stubs["source"] == 1

    cached_until, data = main._source_health_cached
    monkeypatch.setattr(main, "_source_health_cached", (cached_until - 61.0, data))
    assert client.get("/health").json()["status"] == "healthy"
    assert _health_stubs["source"] == 2


def test_cached_source_health_does_not_hide_a_lost_database(monkeypatch, _health_stubs):
    assert main._health_snapshot()["status"] == "healthy"
    monkeypatch.setattr(main, "_database_health", lambda: ("degraded", False))
    snapshot = main._health_snapshot()
    assert snapshot["status"] == "degraded"
    assert snapshot["data"]["reason"] == "database_unavailable"


def test_failed_source_health_is_retried_after_ten_seconds(monkeypatch, _health_stubs):
    def failing(as_of, months):
        _health_stubs["source"] += 1
        raise RuntimeError("timeout")

    monkeypatch.setattr(main.sig, "sales_source_status", failing)
    assert main._health_snapshot()["data"]["reason"] == "source_query_failed"
    cached_until, _ = main._source_health_cached
    import time

    assert cached_until - time.monotonic() <= main._SOURCE_HEALTH_FAILURE_TTL_SECONDS


def test_health_fails_loudly_when_synthetic_product_is_unresolved(monkeypatch, _health_stubs):
    monkeypatch.setattr(
        main.sig,
        "synthetic_product_status",
        lambda: {"product_id": None, "resolved": False, "source": "unresolved"},
    )
    snapshot = main._health_snapshot()
    assert snapshot["status"] == "degraded"
    assert snapshot["data"]["reason"] == "synthetic_product_unresolved"
    assert snapshot["data"]["synthetic_product_id"] is None
    assert _health_stubs["source"] == 0


def test_holdout_lead_two_skips_the_open_month():
    series = [100.0] * 8 + [400.0, 200.0]
    one = holdout.evaluate([series], origins=1, methods=("naive_last",), lead=1)
    two = holdout.evaluate([series], origins=1, methods=("naive_last",), lead=2)
    assert one["by_class"]["overall"]["methods"]["naive_last"]["bias_pct"] == 100.0
    assert two["by_class"]["overall"]["methods"]["naive_last"]["bias_pct"] == -50.0
    assert two["lead_months"] == 2
    with pytest.raises(ValueError, match="lead must be at least one month"):
        holdout.evaluate([series], lead=0)


def test_holdout_service_uses_the_scope_alpha():
    smooth = [100.0, 120.0, 90.0, 110.0, 130.0, 95.0, 105.0, 500.0]
    assert classify.classify(smooth[:-1]) == classify.SMOOTH
    assert holdout.service_forecast(
        smooth, classify.SMOOTH, scope=selection.SCOPE_PRODUCT
    ) == demand.ewma_rate(smooth, 0.15)
    by_scope = {
        scope: holdout.evaluate([smooth + [100.0]], origins=1, scope=scope)["by_class"]["overall"][
            "methods"
        ]["service"]["wape_pct"]
        for scope in (selection.SCOPE_CLIENT, selection.SCOPE_PRODUCT)
    }
    assert by_scope[selection.SCOPE_PRODUCT] < by_scope[selection.SCOPE_CLIENT]
    assert accuracy_gate.SERVED_LEAD_MONTHS == 2
    assert accuracy_gate.SCOPE_BY_GATE == {
        "clients": selection.SCOPE_CLIENT,
        "products": selection.SCOPE_PRODUCT,
    }
