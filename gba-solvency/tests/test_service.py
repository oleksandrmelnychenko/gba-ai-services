from __future__ import annotations

import pytest

from app.domain.models import (
    CapType,
    DebtLoadSource,
    Rating,
    SubFactor,
    SubFactors,
)
from app.services.solvency import score as scoring
from app.services.solvency import service


def _stub_computation():
    sf = SubFactors(
        discipline=SubFactor(value=1.0, points=35.0, weight=0.35),
        debt_load=SubFactor(value=1.0, points=25.0, weight=0.25),
        activity=SubFactor(value=1.0, points=20.0, weight=0.20),
        tenure=SubFactor(value=1.0, points=10.0, weight=0.10),
        return_quality=SubFactor(value=1.0, points=10.0, weight=0.10),
    )
    return scoring.ScoreComputation(
        sub_factors=sf, raw_score=100.0, score=100, rating=Rating.A,
        caps_applied=[], debt_load_source=DebtLoadSource.LIVE_PROXY,
    )


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(service.cache, "set", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _client_exists(monkeypatch):
    monkeypatch.setattr(service.repo, "client_exists", lambda cid: True)


def test_score_client_resolves_net_uid(monkeypatch):
    monkeypatch.setattr(service.repo, "resolve_client_id", lambda uid: 777)
    monkeypatch.setattr(service.scoring, "compute_score", lambda *a, **k: _stub_computation())
    monkeypatch.setattr(service.repo, "turnover_eur_by_currency", lambda *a, **k: [])
    out = service.score_client(None, "abc-uid", "2026-06-01", 12, use_cache=False)
    assert out.client_id == 777
    assert out.score == 100
    assert out.rating == Rating.A


def test_score_client_unknown_net_uid_raises_lookup(monkeypatch):
    monkeypatch.setattr(service.repo, "resolve_client_id", lambda uid: None)
    with pytest.raises(LookupError):
        service.score_client(None, "missing", None, 12, use_cache=False)


def test_score_client_nonexistent_client_id_raises_lookup(monkeypatch):
    monkeypatch.setattr(service.repo, "client_exists", lambda cid: False)

    def boom(*a, **k):
        raise AssertionError("must not compute a score for a nonexistent client")
    monkeypatch.setattr(service.scoring, "compute_score", boom)
    with pytest.raises(LookupError):
        service.score_client(999999999, None, None, 12, use_cache=False)


def test_build_charts_nonexistent_client_id_raises_lookup(monkeypatch):
    monkeypatch.setattr(service.repo, "client_exists", lambda cid: False)
    with pytest.raises(LookupError):
        service.build_charts(999999999, None, 12)


def test_score_client_currency_breakdown_only_when_multicurrency(monkeypatch):
    monkeypatch.setattr(service.scoring, "compute_score", lambda *a, **k: _stub_computation())
    monkeypatch.setattr(service.repo, "turnover_eur_by_currency", lambda *a, **k: [
        {"currency_id": 1, "turnover_eur": 100.0},
        {"currency_id": 2, "turnover_eur": 200.0},
    ])
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.currency_breakdown is not None
    assert len(out.currency_breakdown) == 2
    assert all(c.exposure_eur is None for c in out.currency_breakdown)
    assert {c.exposure_source.value for c in out.currency_breakdown} == {"unavailable"}


def test_score_client_no_currency_breakdown_single_currency(monkeypatch):
    monkeypatch.setattr(service.scoring, "compute_score", lambda *a, **k: _stub_computation())
    monkeypatch.setattr(service.repo, "turnover_eur_by_currency", lambda *a, **k: [
        {"currency_id": 1, "turnover_eur": 100.0}])
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.currency_breakdown is None


def test_score_client_cache_hit_hydrates(monkeypatch):
    out_first = SubFactors(
        discipline=SubFactor(value=0.5, points=17.5, weight=0.35),
        debt_load=SubFactor(value=0.5, points=12.5, weight=0.25),
        activity=SubFactor(value=0.5, points=10.0, weight=0.20),
        tenure=SubFactor(value=0.5, points=5.0, weight=0.10),
        return_quality=SubFactor(value=0.5, points=5.0, weight=0.10),
    )
    from app.domain.models import SolvencyScore
    cached_obj = SolvencyScore(
        client_id=9, score=50, rating=Rating.C, sub_factors=out_first,
        caps_applied=[CapType.UTILIZATION_SOFT_60], debt_load_source=DebtLoadSource.DEBT_TABLE,
        raw_score=50.0, as_of_date="2026-06-01", window_months=12,
    ).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached_obj)

    def boom(*a, **k):
        raise AssertionError("should not recompute on cache hit")
    monkeypatch.setattr(service.scoring, "compute_score", boom)

    out = service.score_client(9, None, "2026-06-01", 12, use_cache=True)
    assert out.score == 50
    assert out.rating == Rating.C
    assert out.caps_applied == [CapType.UTILIZATION_SOFT_60]
    assert out.debt_load_source == DebtLoadSource.DEBT_TABLE


def test_build_charts_assembles_from_repo(monkeypatch):
    monkeypatch.setattr(service.repo, "payment_status_counts", lambda *a, **k: {
        "paid": 5, "overpaid": 1, "partial": 2, "notpaid": 3, "refund": 4})
    monkeypatch.setattr(service.repo, "retail_payment_status_counts", lambda *a, **k: {
        "paid": 0, "partial": 0, "notpaid": 0})
    monkeypatch.setattr(service.repo, "credit_limit_utilization", lambda *a, **k: [
        {"is_control_amount": True, "amount_debt": 1000.0, "limit_utilization": 0.8}])
    monkeypatch.setattr(service.repo, "open_unpaid_aging_buckets", lambda *a, **k: [
        {"bucket": "0-30", "count": 2}, {"bucket": "90+", "count": 1}])
    monkeypatch.setattr(service.repo, "monthly_turnover_series", lambda *a, **k: [
        {"period": "2026-05", "turnover_eur": 100.0}, {"period": "2026-06", "turnover_eur": 200.0}])
    monkeypatch.setattr(service.repo, "debt_sync_is_live", lambda *a, **k: False)
    monkeypatch.setattr(service.repo, "open_unpaid_stats", lambda *a, **k: {
        "open_count": 1, "max_age_days": 10, "avg_age_days": 5.0})
    monkeypatch.setattr(service.repo, "total_sales_count", lambda *a, **k: 10)

    from app.services.solvency import charts as charts_builder
    monkeypatch.setattr(
        charts_builder, "_score_sparkline",
        lambda *a, **k: [],
    )

    out = service.build_charts(11, "2026-06-01", 12)
    assert out.limit_utilization_gauge.value == 0.8
    assert out.limit_utilization_gauge.has_controlled_limit is True
    labels = {d.label: d.count for d in out.payment_discipline_donut}
    assert labels["refund"] == 4
    assert labels["partial"] == 2
    assert [b.bucket for b in out.open_invoice_aging_bars] == ["0-30", "31-60", "61-90", "90+"]
    assert out.aging_over_time_heatmap == "pending"
    assert len(out.turnover_trend) == 2
    assert all(p.exposure_eur is None for p in out.turnover_vs_exposure)
    assert {p.exposure_source.value for p in out.turnover_vs_exposure} == {"unavailable"}


def test_build_charts_marks_missing_limit_and_live_debt_exposure(monkeypatch):
    monkeypatch.setattr(service.repo, "payment_status_counts", lambda *a, **k: {})
    monkeypatch.setattr(service.repo, "retail_payment_status_counts", lambda *a, **k: {})
    monkeypatch.setattr(service.repo, "credit_limit_utilization", lambda *a, **k: [])
    monkeypatch.setattr(service.repo, "open_unpaid_aging_buckets", lambda *a, **k: [])
    monkeypatch.setattr(service.repo, "monthly_turnover_series", lambda *a, **k: [
        {"period": "2026-06", "turnover_eur": 200.0}])
    monkeypatch.setattr(service.repo, "debt_sync_is_live", lambda *a, **k: True)
    monkeypatch.setattr(service.repo, "overdue_amount_eur", lambda *a, **k: 0.0)

    from app.services.solvency import charts as charts_builder
    monkeypatch.setattr(charts_builder, "_score_sparkline", lambda *a, **k: [])

    out = service.build_charts(11, "2026-06-01", 12)

    assert out.limit_utilization_gauge.value is None
    assert out.limit_utilization_gauge.has_controlled_limit is False
    assert out.turnover_vs_exposure[0].exposure_eur == 0.0
    assert out.turnover_vs_exposure[0].exposure_source.value == "debt_table"
