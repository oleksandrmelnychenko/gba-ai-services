"""Serving contract regressions; every repository/cache dependency is replaced, never SQL."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import main
from app.core import serving_context
from app.core.config import Settings
from app.core.serving_context import ServingConfigurationError
from app.domain.models import BatchScoreRequest, ScoreRequest
from app.risk.dataset import FEATURE_COLUMNS
from app.services.solvency import charts, service

AS_OF = "2026-06-01"


@pytest.fixture(autouse=True)
def fixed_context(monkeypatch):
    monkeypatch.setattr(serving_context, "utc_now", lambda: datetime(2026, 6, 1, 9, tzinfo=timezone.utc))
    settings = Settings(_env_file=None)
    for module in (serving_context, service, charts):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "_service", lambda: service)
    return settings


def fail_io(*_args, **_kwargs):
    raise AssertionError("unsupported serving request reached business I/O")


@pytest.fixture
def no_io(monkeypatch):
    for method in ("client_exists", "resolve_client_id", "has_buyer_role"):
        monkeypatch.setattr(service.repo, method, fail_io)
    for method in ("get", "set", "make_key", "make_charts_key"):
        monkeypatch.setattr(service.cache, method, fail_io)
    monkeypatch.setattr(service.risk_dataset, "features_one", fail_io)
    monkeypatch.setattr(service.risk_dataset, "features_many", fail_io)


def invoke(kind, *, as_of=AS_OF, window=12):
    if kind == "single":
        return service.score_client(1, None, as_of, window, True)
    if kind == "batch":
        return service.score_batch([1, 2], as_of, window, True)
    return service.build_charts(1, as_of, window)


@pytest.mark.parametrize("kind", ["single", "batch"])
@pytest.mark.parametrize("window", [1, 11, 13, 60, 12.0, True])
def test_untrained_window_rejected_before_cache_and_database(no_io, kind, window):
    with pytest.raises(ValueError, match="must be 12"):
        invoke(kind, window=window)


@pytest.mark.parametrize("kind", ["single", "batch", "charts"])
@pytest.mark.parametrize("as_of", ["2026-05-31", "2026-06-02"])
def test_past_and_future_current_states_rejected_before_io(no_io, kind, as_of):
    with pytest.raises(ValueError, match="current Europe/Kyiv date"):
        invoke(kind, as_of=as_of)


@pytest.mark.parametrize("kind", ["single", "batch", "charts"])
def test_misaligned_fx_override_rejected_without_silently_changing_rate(no_io, fixed_context, kind):
    fixed_context.fx_snapshot_date = "2026-05-31"
    with pytest.raises(ServingConfigurationError, match="configured FX date"):
        invoke(kind)
    assert fixed_context.fx_snapshot_date == "2026-05-31"


@pytest.mark.parametrize("instant, expected", [
    ("2026-05-31T21:30:00+00:00", "2026-06-01"),
    ("2026-01-31T22:30:00+00:00", "2026-02-01"),
    ("2026-10-25T21:30:00+00:00", "2026-10-25"),
])
def test_business_day_uses_kyiv_timezone_including_dst(monkeypatch, instant, expected):
    calls = []
    def clock():
        calls.append(True)
        return datetime.fromisoformat(instant)
    monkeypatch.setattr(serving_context, "utc_now", clock)
    result = serving_context.resolve_current_context(None, 12, scoring=True)
    assert (result.as_of, result.fx_date) == (expected, expected)
    assert len(calls) == 1


@pytest.fixture
def empty_native_reads(monkeypatch):
    monkeypatch.setattr(service.repo, "client_exists", lambda _cid: True)
    monkeypatch.setattr(service.repo, "has_buyer_role", lambda _cid: True)
    monkeypatch.setattr(service.cache, "get", lambda *_a: None)
    monkeypatch.setattr(service.cache, "set", lambda *_a: None)
    for method in ("credit_limit_utilization", "debt_aging_buckets_truth", "monthly_turnover_series"):
        monkeypatch.setattr(service.repo, method, lambda *_a: [])
    monkeypatch.setattr(service.repo, "debt_exposure_donut_truth", lambda *_a: {})
    monkeypatch.setattr(service.repo, "debt_sync_is_live", lambda: False)
    monkeypatch.setattr(service.repo, "open_unpaid_stats", lambda *_a: {})
    monkeypatch.setattr(service.repo, "total_sales_count", lambda *_a: 0)
    monkeypatch.setattr(service, "_currency_breakdown", lambda *_a: None)
    monkeypatch.setattr(service.risk_dataset, "features_one", fail_io)
    monkeypatch.setattr(service.risk_dataset, "features_many", fail_io)
    monkeypatch.setattr(service, "score_current", fail_io)


@pytest.mark.parametrize("months", [1, 12, 60])
def test_chart_display_window_never_runs_historical_score(empty_native_reads, months):
    result = service.build_charts(1, None, months)
    assert result.window_months == months
    assert result.score_sparkline == []
    assert result.score_sparkline_status == "unavailable"
    assert result.score_sparkline_reason_code == "historical_state_not_recorded"
    assert result.score_sparkline_reason.strip()
    assert result.state_basis == "current_observation"
    assert result.business_timezone == "Europe/Kyiv"
    assert result.as_of_date == result.fx_date == AS_OF
    assert result.model_version == "creditscore-v3"


def test_no_data_headline_and_chart_cannot_invent_a_perfect_score(empty_native_reads, monkeypatch):
    features = dict.fromkeys(FEATURE_COLUMNS, 0.0)
    features["recency_days"] = 3000.0
    monkeypatch.setattr(service.risk_dataset, "features_one", lambda *_a: features)
    score = service.score_client(1, None, None, 12, False)
    chart = service.build_charts(1, None, 12)
    assert score.score is None and score.data_sufficiency == "insufficient"
    assert chart.score_sparkline == [] and chart.score_sparkline_status == "unavailable"


def test_nonbuyer_has_explicit_not_applicable_history(empty_native_reads, monkeypatch):
    monkeypatch.setattr(service.repo, "has_buyer_role", lambda _cid: False)
    score = service.score_client(1, None, None, 12, True)
    result = service.build_charts(1, None, 12)
    assert score.applicable is False and score.score is None
    assert result.applicable is False and result.score_sparkline == []
    assert result.score_sparkline_status == "not_applicable"
    assert result.score_sparkline_reason_code == "client_not_buyer"
    assert result.as_of_date == result.fx_date == AS_OF


@pytest.mark.parametrize("field", ["state_basis", "business_timezone", "fx_date",
    "score_sparkline_status", "score_sparkline_reason_code", "score_sparkline_reason"])
def test_chart_cache_requires_every_new_proof_field(empty_native_reads, field):
    cached = service.build_charts(1, None, 12).model_dump(mode="json")
    cached.pop(field)
    assert service._hydrate_charts(cached, 1, AS_OF, 12) is None


def test_legacy_numeric_sparkline_cache_is_recomputed_as_unavailable(empty_native_reads, monkeypatch):
    cached = service.build_charts(1, None, 12).model_dump(mode="json")
    cached["score_sparkline"] = [{"period": "2026-05", "score": 100}]
    monkeypatch.setattr(service.cache, "get", lambda *_a: cached)
    result = service.build_charts(1, None, 12)
    assert result.score_sparkline == []


def test_current_empty_history_cache_is_accepted_without_rebuild(empty_native_reads, monkeypatch):
    cached = service.build_charts(1, None, 12).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *_a: cached)
    monkeypatch.setattr(charts, "build_charts", fail_io)
    assert service.build_charts(1, None, 12).score_sparkline == []


def test_single_and_batch_insufficient_cache_requires_current_date_window_and_proof(empty_native_reads, monkeypatch):
    features = dict.fromkeys(FEATURE_COLUMNS, 0.0)
    features["recency_days"] = 3000.0
    monkeypatch.setattr(service.risk_dataset, "features_one", lambda *_a: features)
    valid = service.score_client(1, None, None, 12, False).model_dump(mode="json")
    assert service._hydrate_score(valid, 1, AS_OF, 12) is not None
    for field, value in [("window_months", 1), ("fx_date", "2026-05-31"),
                         ("as_of_date", "2026-05-31"), ("score", 100)]:
        cached = {**valid, field: value}
        assert service._hydrate_score(cached, 1, AS_OF, 12) is None
    for field in ("state_basis", "business_timezone", "fx_date"):
        cached = deepcopy(valid)
        cached.pop(field)
        assert service._hydrate_score(cached, 1, AS_OF, 12) is None
    monkeypatch.setattr(service.risk_dataset, "features_many", lambda ids, *_a: {cid: features for cid in ids})
    monkeypatch.setattr(service.cache, "get", lambda *_a: {**valid, "window_months": 1})
    results, errors = service.score_batch([1], None, 12, True)
    assert errors == [] and results[0].score is None and results[0].window_months == 12


def test_request_crossing_midnight_uses_one_context_for_chart_queries(empty_native_reads, monkeypatch):
    reads = []
    def clock():
        reads.append(True)
        if len(reads) > 1:
            raise AssertionError("chart re-read the clock after request validation")
        return datetime(2026, 5, 31, 21, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(serving_context, "utc_now", clock)
    result = service.build_charts(1, None, 12)
    assert result.as_of_date == AS_OF and len(reads) == 1


@pytest.mark.parametrize("request_type, identity", [(ScoreRequest, {"client_id": 1}),
                                                   (BatchScoreRequest, {"client_ids": [1]})])
def test_wire_scoring_window_is_exact_integer_twelve(request_type, identity):
    assert request_type(**identity).window_months == 12
    for value in (1, 60, 12.0, "12", True):
        with pytest.raises(ValidationError):
            request_type(**identity, window_months=value)


@pytest.mark.parametrize("endpoint", ["/score", "/score/batch", "/charts/1"])
def test_http_rejects_unsupported_current_date_before_io(no_io, endpoint):
    headers = {"X-Internal-Api-Key": main.settings.internal_api_key} if main.settings.internal_api_key else {}
    client = TestClient(main.app)
    if endpoint == "/charts/1":
        response = client.get(endpoint, params={"as_of_date": "2026-05-31"}, headers=headers)
    else:
        identity = {"client_ids": [1]} if endpoint.endswith("batch") else {"client_id": 1}
        response = client.post(endpoint, json={**identity, "as_of_date": "2026-05-31"}, headers=headers)
    assert response.status_code == 422


def test_http_reports_misaligned_fx_as_service_unavailable(no_io, fixed_context):
    fixed_context.fx_snapshot_date = "2026-05-31"
    headers = {"X-Internal-Api-Key": main.settings.internal_api_key} if main.settings.internal_api_key else {}
    response = TestClient(main.app).post("/score", json={"client_id": 1}, headers=headers)
    assert response.status_code == 503
    assert "configured FX date" in response.json()["detail"]


def test_explicit_blank_as_of_is_not_silently_reinterpreted(no_io):
    with pytest.raises(ValueError):
        invoke("single", as_of="")
