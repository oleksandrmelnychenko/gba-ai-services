from __future__ import annotations

import pytest
from datetime import datetime, timezone
from app.core import serving_context
from app.core.serving_context import observation_fields

from app.domain.models import (
    ClientIdentityMismatchError,
    DataSufficiency,
    ForwardRiskBand,
    ForwardRiskStatus,
    Rating,
    Risk90dBand,
    Risk90dReason,
)
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


@pytest.fixture(autouse=True)
def _fixed_serving_day(monkeypatch):
    # Existing fixtures intentionally describe this day; the production clock is never bypassed.
    monkeypatch.setattr(serving_context, "utc_now", lambda: datetime(2026, 6, 1, 9, tzinfo=timezone.utc))


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


def _wire_v3(monkeypatch, *, current=None, currency=None, features=None):
    feature_values = _stub_features() if features is None else features
    monkeypatch.setattr(
        service.risk_dataset,
        "features_one",
        lambda *a, **k: feature_values,
    )
    monkeypatch.setattr(service, "score_current", lambda f: current or _stub_current())
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


def test_score_client_both_identities_must_resolve_to_same_client(monkeypatch):
    monkeypatch.setattr(service.repo, "resolve_client_id", lambda uid: 777)
    _wire_v3(monkeypatch)

    out = service.score_client(777, "matching-uid", "2026-06-01", 12, use_cache=False)

    assert out.client_id == 777


def test_score_client_rejects_mismatched_dual_identity_before_compute(monkeypatch):
    monkeypatch.setattr(service.repo, "resolve_client_id", lambda uid: 778)

    def boom(*_args, **_kwargs):
        raise AssertionError("must not score a mismatched client identity")

    monkeypatch.setattr(service.risk_dataset, "features_one", boom)
    with pytest.raises(ClientIdentityMismatchError, match="do not match"):
        service.score_client(777, "different-uid", "2026-06-01", 12, use_cache=False)


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
    """v3 shape includes the operational 90-day control; legacy forward risk is retired."""
    features = _stub_features()
    features["total_debt_eur"] = 100.0
    _wire_v3(monkeypatch, current=_stub_current(band="D", score=46.0, pd=0.65),
             features=features)
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.applicable is True
    assert out.score == 46
    assert out.rating == Rating.D
    assert out.pd == 0.65
    assert out.sub_factors is None  # DEPRECATED, kept null for back-compat
    assert out.contributions is not None and len(out.contributions) == 2
    assert out.contributions[0].feature == "n_open_debt_lines"
    assert out.contributions[0].points == -5.62
    assert out.risk_90d is not None
    assert out.risk_90d.band == Risk90dBand.MEDIUM
    assert out.risk_90d.exposure_eur == 100.0
    assert out.risk_90d.reason_code == Risk90dReason.CURRENT_DEBT
    assert out.forward_risk is None
    assert out.forward_risk_status == ForwardRiskStatus.NOT_APPLICABLE
    assert out.forward_risk_reason == "replaced_by_operational_90d"
    assert out.model_version == "creditscore-v3"


def test_score_client_no_debt_has_low_operational_risk(monkeypatch):
    _wire_v3(monkeypatch, current=_stub_current(band="A", score=100.0))
    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)
    assert out.risk_90d is not None
    assert out.risk_90d.band == Risk90dBand.LOW
    assert out.risk_90d.exposure_eur == 0.0
    assert out.risk_90d.reason_code == Risk90dReason.NO_DEBT
    assert out.forward_risk is None
    assert out.forward_risk_status == ForwardRiskStatus.NOT_APPLICABLE


def test_operational_90_day_control_replaces_retired_forward_model(monkeypatch):
    features = _stub_features()
    features["total_debt_eur"] = 100.0
    _wire_v3(monkeypatch, current=_stub_current(score=82.0, pd=0.01), features=features)

    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)

    assert out.score == 82
    assert out.pd == 0.01
    assert out.risk_90d is not None
    assert out.risk_90d.band == Risk90dBand.MEDIUM
    assert out.forward_risk is None
    assert out.forward_risk_status == ForwardRiskStatus.NOT_APPLICABLE
    assert out.forward_risk_reason == "replaced_by_operational_90d"


def test_score_client_empty_zero_terms_is_insufficient_with_control_flag(monkeypatch):
    """A sync-created zero agreement must not manufacture enough evidence for an A rating."""
    features = _stub_features()
    features["recency_days"] = 3000.0
    features["has_credit_control"] = 1.0
    _wire_v3(
        monkeypatch,
        current=_stub_current(band="A", score=100.0),
        features=features,
    )

    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)

    assert out.data_sufficiency == DataSufficiency.INSUFFICIENT
    assert out.data_sufficiency_reason
    assert out.score is None
    assert out.rating is None
    assert out.pd is None
    assert out.contributions is None
    assert out.risk_90d is None
    assert out.forward_risk is None


def test_score_client_positive_credit_terms_are_a_real_sufficiency_signal(monkeypatch):
    features = _stub_features()
    features["recency_days"] = 3000.0
    features["credit_limit_eur"] = 1000.0
    features["has_credit_control"] = 1.0
    _wire_v3(monkeypatch, features=features)

    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)

    assert out.data_sufficiency == DataSufficiency.OK


def test_score_client_already_sev180_has_no_out_of_population_forward_risk(monkeypatch):
    features = _stub_features()
    features["overdue_eur_180plus"] = 100.0
    features["total_debt_eur"] = 100.0
    monkeypatch.setattr(service.risk_dataset, "features_one", lambda *a, **k: features)
    monkeypatch.setattr(service, "score_current", lambda f: _stub_current(band="D", score=55.0))
    monkeypatch.setattr(service.repo, "turnover_eur_by_currency", lambda *a, **k: [])

    out = service.score_client(5, None, "2026-06-01", 12, use_cache=False)

    assert out.rating == Rating.D
    assert out.risk_90d is not None
    assert out.risk_90d.band == Risk90dBand.CRITICAL
    assert out.risk_90d.exposure_eur == 100.0
    assert out.risk_90d.reason_code == Risk90dReason.ALREADY_90_PLUS
    assert out.forward_risk is None


def test_risk_90d_high_is_exact_overdue_exposure_rounded_to_cents():
    risk = service._risk_90d(
        {
            "overdue_eur_1_30": 40.004,
            "overdue_eur_31_60": 60.001,
            "total_debt_eur": 150.005,
        }
    )

    assert risk.band == Risk90dBand.HIGH
    assert risk.exposure_eur == 100.01
    assert risk.reason_code == Risk90dReason.WILL_CROSS_90_DAYS


@pytest.mark.parametrize(
    ("total_debt_eur", "expected_band", "expected_exposure"),
    [
        (0.004, Risk90dBand.LOW, 0.0),
        (0.005, Risk90dBand.MEDIUM, 0.01),
    ],
)
def test_risk_90d_classifies_after_cent_rounding(
    total_debt_eur,
    expected_band,
    expected_exposure,
):
    risk = service._risk_90d({"total_debt_eur": total_debt_eur})

    assert risk.band == expected_band
    assert risk.exposure_eur == expected_exposure


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
    from app.domain.models import Contribution, Risk90d, SolvencyScore

    cached_obj = SolvencyScore(
        **observation_fields("2026-06-01"),
        client_id=9, applicable=True, score=46, rating=Rating.D, pd=0.65,
        contributions=[Contribution(feature="n_open_debt_lines", value=2.0, points=3.7)],
        risk_90d=Risk90d(
            band=Risk90dBand.MEDIUM,
            exposure_eur=250.25,
            reason_code=Risk90dReason.CURRENT_DEBT,
        ),
        forward_risk_status=ForwardRiskStatus.NOT_APPLICABLE,
        forward_risk_reason="replaced_by_operational_90d",
        sub_factors=None, as_of_date="2026-06-01", window_months=12,
        source_history_start="2025-01-01", effective_start="2025-06-01",
        history_complete=True, current_model_run_id="sev180-current-v1",
    ).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached_obj)

    def boom(*a, **k):
        raise AssertionError("should not recompute on cache hit")
    monkeypatch.setattr(service.risk_dataset, "features_one", boom)

    out = service.score_client(9, None, "2026-06-01", 12, use_cache=True)
    assert out.score == 46
    assert out.rating == Rating.D
    assert out.pd == 0.65
    assert out.risk_90d.band == Risk90dBand.MEDIUM
    assert out.forward_risk is None
    assert out.contributions[0].feature == "n_open_debt_lines"


def test_score_client_rejects_legacy_forward_risk_cache(monkeypatch):
    from app.domain.models import Contribution, ForwardRisk, SolvencyScore

    cached_obj = SolvencyScore(
        **observation_fields("2026-06-01"),
        client_id=9,
        applicable=True,
        score=46,
        rating=Rating.D,
        pd=0.65,
        contributions=[
            Contribution(
                feature="n_open_debt_lines",
                value=2.0,
                points=3.7,
            )
        ],
        risk_90d=None,
        forward_risk=ForwardRisk(
            band=ForwardRiskBand.HIGH,
            pd=0.8,
        ),
        forward_risk_status=ForwardRiskStatus.AVAILABLE,
        sub_factors=None,
        as_of_date="2026-06-01",
        window_months=12,
        source_history_start="2025-01-01",
        effective_start="2025-06-01",
        history_complete=True,
        current_model_run_id="sev180-current-v1",
    ).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached_obj)
    _wire_v3(monkeypatch)

    out = service.score_client(9, None, "2026-06-01", 12, use_cache=True)

    assert out.score == 100
    assert out.risk_90d is not None
    assert out.risk_90d.band == Risk90dBand.LOW
    assert out.forward_risk is None


def test_score_client_rejects_internally_inconsistent_risk_cache(monkeypatch):
    from app.domain.models import Contribution, Risk90d, SolvencyScore

    cached_obj = SolvencyScore(
        **observation_fields("2026-06-01"),
        client_id=9,
        applicable=True,
        score=46,
        rating=Rating.D,
        pd=0.65,
        contributions=[
            Contribution(
                feature="n_open_debt_lines",
                value=2.0,
                points=3.7,
            )
        ],
        risk_90d=Risk90d(
            band=Risk90dBand.MEDIUM,
            exposure_eur=0.01,
            reason_code=Risk90dReason.CURRENT_DEBT,
        ),
        forward_risk_status=ForwardRiskStatus.NOT_APPLICABLE,
        forward_risk_reason="replaced_by_operational_90d",
        sub_factors=None,
        as_of_date="2026-06-01",
        window_months=12,
        source_history_start="2025-01-01",
        effective_start="2025-06-01",
        history_complete=True,
        current_model_run_id="sev180-current-v1",
    ).model_dump(mode="json")
    cached_obj["risk_90d"]["exposure_eur"] = 0.0
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached_obj)
    _wire_v3(monkeypatch)

    out = service.score_client(9, None, "2026-06-01", 12, use_cache=True)

    assert out.score == 100
    assert out.risk_90d is not None
    assert out.risk_90d.band == Risk90dBand.LOW


def test_score_client_ignores_cache_entry_for_another_client(monkeypatch):
    from app.domain.models import Contribution, Risk90d, SolvencyScore

    cached_obj = SolvencyScore(
        **observation_fields("2026-06-01"),
        client_id=99, applicable=True, score=46, rating=Rating.D, pd=0.65,
        contributions=[Contribution(feature="n_open_debt_lines", value=2.0, points=3.7)],
        risk_90d=Risk90d(
            band=Risk90dBand.MEDIUM,
            exposure_eur=250.25,
            reason_code=Risk90dReason.CURRENT_DEBT,
        ),
        forward_risk_status=ForwardRiskStatus.NOT_APPLICABLE,
        forward_risk_reason="replaced_by_operational_90d",
        sub_factors=None, as_of_date="2026-06-01", window_months=12,
        source_history_start="2025-01-01", effective_start="2025-06-01",
        history_complete=True, current_model_run_id="sev180-current-v1",
    ).model_dump(mode="json")
    monkeypatch.setattr(service.cache, "get", lambda *a, **k: cached_obj)
    _wire_v3(monkeypatch)

    out = service.score_client(9, None, "2026-06-01", 12, use_cache=True)

    assert out.client_id == 9
    assert out.score == 100


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

    def no_historical_features(*_args, **_kwargs):
        raise AssertionError("charts must not recompute current features as historical scores")
    monkeypatch.setattr(service.risk_dataset, "features_one", no_historical_features)
    monkeypatch.setattr(service, "score_current", no_historical_features)

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

    assert out.score_sparkline == []
    assert out.score_sparkline_status == "unavailable"
    assert out.score_sparkline_reason_code == "historical_state_not_recorded"
    assert out.score_sparkline_reason
    assert out.fx_date == out.as_of_date
