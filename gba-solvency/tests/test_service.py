from __future__ import annotations

import pytest

from app.domain.models import ForwardRiskBand, Rating
from app.services.solvency import service


def _stub_features() -> dict[str, float]:
    """A flat feature dict (all FEATURE_COLUMNS) — values are irrelevant when score_current is
    stubbed; only the key set must exist so forward/current stubs receive a real mapping."""
    from app.risk.dataset import FEATURE_COLUMNS

    return {c: 0.0 for c in FEATURE_COLUMNS}


def _stub_current(band: str = "A", score: float = 100.0, pd: float = 0.001) -> dict:
    return {
        "score": score,
        "pd": pd,
        "band": band,
        "rating": "low risk",
        "linear_predictor": -2.0,
        "contributions": [
            {"feature": "n_open_debt_lines", "value": 0.0, "woe": -4.0, "points": -5.62},
            {"feature": "grace_days", "value": 7.0, "woe": 0.98, "points": 0.36},
        ],
        "model": "woe_logistic_scorecard_current_state",
        "model_version": "sev180-current-v1",
    }


def _stub_forward(band: str = "low", pd_beh: float = 0.05) -> dict:
    if band == "none":
        return {"score": 0.0, "pd_behavioral": 0.0, "pd_with_aging": 0.0,
                "band": "none", "already_rolling": False, "note": "no debt"}
    return {"score": round(100 * pd_beh, 1), "pd_behavioral": pd_beh,
            "pd_with_aging": 0.1, "band": band, "already_rolling": False}


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(service.cache, "set", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _client_exists(monkeypatch):
    monkeypatch.setattr(service.repo, "client_exists", lambda cid: True)


@pytest.fixture(autouse=True)
def _has_buyer_role(monkeypatch):
    monkeypatch.setattr(service.repo, "has_buyer_role", lambda cid: True)


def _wire_v3(monkeypatch, *, current=None, forward=None, currency=None):
    monkeypatch.setattr(service.risk_dataset, "features_one", lambda *a, **k: _stub_features())
    monkeypatch.setattr(service, "score_current", lambda f: current or _stub_current())
    monkeypatch.setattr(service, "score_forward", lambda f: forward or _stub_forward())
    monkeypatch.setattr(
        service.repo, "turnover_eur_by_currency", lambda *a, **k: currency or []
    )


def test_score_client_resolves_net_uid(monkeypatch):
    monkeypatch.setattr(service.repo, "resolve_client_id", lambda uid: 777)
    _wire_v3(monkeypatch)
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
    monkeypatch.setattr(service.risk_dataset, "features_one", boom)
    with pytest.raises(LookupError):
        service.score_client(999999999, None, None, 12, use_cache=False)


def test_build_charts_nonexistent_client_id_raises_lookup(monkeypatch):
    monkeypatch.setattr(service.repo, "client_exists", lambda cid: False)
    with pytest.raises(LookupError):
        service.build_charts(999999999, None, 12)


def test_score_client_v3_shape_applicable_buyer(monkeypatch):
    """v3 shape: score/pd/rating/contributions/forward_risk populated; sub_factors null."""
    _wire_v3(monkeypatch, current=_stub_current(band="D", score=46.0, pd=0.65),
             forward=_stub_forward(band="very_high", pd_beh=0.99))
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.applicable is True
    assert out.score == 46
    assert out.rating == Rating.D
    assert out.pd == 0.65
    assert out.sub_factors is None  # DEPRECATED, kept null for back-compat
    assert out.contributions is not None and len(out.contributions) == 2
    assert out.contributions[0].feature == "n_open_debt_lines"
    assert out.contributions[0].points == -5.62
    assert out.forward_risk is not None
    assert out.forward_risk.band == ForwardRiskBand.VERY_HIGH
    assert out.forward_risk.pd == 0.99
    assert out.model_version == "creditscore-v3"


def test_score_client_no_debt_forward_low_band(monkeypatch):
    """A buyer with no debt -> forward score_forward returns 'none' -> mapped to band 'low'."""
    _wire_v3(monkeypatch, current=_stub_current(band="A", score=100.0),
             forward=_stub_forward(band="none"))
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.forward_risk is not None
    assert out.forward_risk.band == ForwardRiskBand.LOW
    assert out.forward_risk.pd > 0  # ~ forward base rate


def test_score_client_provider_only_not_applicable_no_compute(monkeypatch):
    monkeypatch.setattr(service.repo, "has_buyer_role", lambda cid: False)

    def boom_features(*a, **k):
        raise AssertionError("must not build features for a provider-only entity")

    def boom_signal(*a, **k):
        raise AssertionError("must not pull any DB signal for a provider-only entity")

    monkeypatch.setattr(service.risk_dataset, "features_one", boom_features)
    monkeypatch.setattr(service.repo, "turnover_eur_by_currency", boom_signal)

    out = service.score_client(410170, None, "2026-06-01", 12, use_cache=True)
    assert out.applicable is False
    assert out.score is None
    assert out.rating is None
    assert out.pd is None
    assert out.contributions is None
    assert out.forward_risk is None
    assert out.sub_factors is None
    assert out.raw_score is None
    assert out.debt_load_source is None
    assert out.caps_applied == []
    assert out.client_id == 410170
    assert out.as_of_date == "2026-06-01"
    assert out.window_months == 12
    assert out.model_version


def test_score_client_not_applicable_does_not_cache(monkeypatch):
    monkeypatch.setattr(service.repo, "has_buyer_role", lambda cid: False)

    def boom_set(*a, **k):
        raise AssertionError("must not cache a meaningless n/a score")

    monkeypatch.setattr(service.cache, "set", boom_set)
    out = service.score_client(410170, None, "2026-06-01", 12, use_cache=True)
    assert out.applicable is False


def test_score_client_currency_breakdown_only_when_multicurrency(monkeypatch):
    _wire_v3(monkeypatch, currency=[
        {"currency_id": 1, "turnover_eur": 100.0},
        {"currency_id": 2, "turnover_eur": 200.0},
    ])
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.currency_breakdown is not None
    assert len(out.currency_breakdown) == 2


def test_score_client_no_currency_breakdown_single_currency(monkeypatch):
    _wire_v3(monkeypatch, currency=[{"currency_id": 1, "turnover_eur": 100.0}])
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.currency_breakdown is None


def test_score_client_cache_hit_hydrates(monkeypatch):
    from app.domain.models import Contribution, ForwardRisk, SolvencyScore

    cached_obj = SolvencyScore(
        client_id=9, applicable=True, score=46, rating=Rating.D, pd=0.65,
        contributions=[Contribution(feature="n_open_debt_lines", value=2.0, points=3.7)],
        forward_risk=ForwardRisk(band=ForwardRiskBand.HIGH, pd=0.8),
        sub_factors=None, as_of_date="2026-06-01", window_months=12,
    ).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached_obj)

    def boom(*a, **k):
        raise AssertionError("should not recompute on cache hit")
    monkeypatch.setattr(service.risk_dataset, "features_one", boom)

    out = service.score_client(9, None, "2026-06-01", 12, use_cache=True)
    assert out.score == 46
    assert out.rating == Rating.D
    assert out.pd == 0.65
    assert out.forward_risk.band == ForwardRiskBand.HIGH
    assert out.contributions[0].feature == "n_open_debt_lines"


def test_build_charts_assembles_from_repo(monkeypatch):
    # TRUTH-based donut + aging: sourced from Debt/ClientInDebt settlement reality, NOT the stale
    # BaseSalePaymentStatus.NotPaid enum (which read ~93% unpaid for nearly every client).
    monkeypatch.setattr(service.repo, "debt_exposure_donut_truth", lambda *a, **k: {
        "settled": 7, "current": 2, "overdue": 1, "current_eur": 50.0, "overdue_eur": 30.0})
    monkeypatch.setattr(service.repo, "credit_limit_utilization", lambda *a, **k: [
        {"is_control_amount": True, "amount_debt": 1000.0, "limit_utilization": 0.8}])
    monkeypatch.setattr(service.repo, "debt_aging_buckets_truth", lambda *a, **k: [
        {"bucket": "0-30", "count": 2, "amount_eur": 12.5},
        {"bucket": "90+", "count": 1, "amount_eur": 30.0}])
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
    labels = {d.label: d.count for d in out.payment_discipline_donut}
    assert labels == {"settled": 7, "current": 2, "overdue": 1}
    assert [b.bucket for b in out.open_invoice_aging_bars] == ["0-30", "31-60", "61-90", "90+"]
    amounts = {b.bucket: b.amount_eur for b in out.open_invoice_aging_bars}
    assert amounts["0-30"] == 12.5 and amounts["90+"] == 30.0
    assert amounts["31-60"] == 0.0  # absent bucket fills to zero count + zero EUR
    assert out.aging_over_time_heatmap == "pending"
    assert len(out.turnover_trend) == 2


def test_score_sparkline_uses_v3_score_current_not_legacy(monkeypatch):
    """The score sparkline is on the v3 model: each month's point is features_one -> score_current
    (the SAME supervised scorecard the headline /score uses), NOT the legacy stale-discipline
    compute_score. We stub the v3 path to a per-month score keyed by the point-in-time as-of and
    assert the points reflect score_current, that compute_score is never called, and that the
    final point's as-of equals the chart as_of (so the last point matches the live /score).
    """
    from app.services.solvency import charts as charts_builder

    seen_as_of: list[str] = []

    def fake_features_one(client_id, as_of, window_months):
        seen_as_of.append(as_of)
        return {"as_of": as_of}  # carry the as-of through to the score stub

    def fake_score_current(feats):
        # deterministic, distinct per month so a wrong source/order is visible
        return {"score": float(70 + int(feats["as_of"][8:10]) % 30)}

    def _boom_legacy(*a, **k):  # the legacy path must NOT be touched anymore
        raise AssertionError("legacy compute_score must not be called by the sparkline")

    monkeypatch.setattr(charts_builder.risk_dataset, "features_one", fake_features_one)
    monkeypatch.setattr(charts_builder, "score_current", fake_score_current)
    monkeypatch.setattr(charts_builder.scoring, "compute_score", _boom_legacy)

    as_of = "2026-06-26"
    points = charts_builder._score_sparkline(411780, as_of, 12)

    assert len(points) == 12
    # contract shape is preserved: ScorePoint(period="YYYY-MM", score=int)
    assert all(isinstance(p.score, int) for p in points)
    assert points[-1].period == "2026-06"
    # the LAST point is computed as-of the chart's own as_of (clamped) — matching the live /score.
    # (months are fanned out, so seen_as_of order is non-deterministic; assert membership + the
    # emitted point, which pool.map keeps in input order.)
    assert as_of in seen_as_of
    assert points[-1].score == int(round(fake_score_current({"as_of": as_of})["score"]))
