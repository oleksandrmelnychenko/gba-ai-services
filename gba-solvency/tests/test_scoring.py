from __future__ import annotations

import pytest

from app.domain.models import CapType, DebtLoadSource, Rating
from app.services.solvency import score as scoring


def test_discipline_excludes_refund_and_halves_partial():
    regular = {"paid": 6, "overpaid": 2, "partial": 4, "notpaid": 0, "refund": 100}
    retail = {"paid": 0, "partial": 0, "notpaid": 0}
    v = scoring.discipline_value(regular, retail)
    assert v == pytest.approx((6 + 2 + 0.5 * 4) / (6 + 2 + 4 + 0))
    assert v == pytest.approx(10.0 / 12.0)


def test_discipline_all_bad_is_zero_and_all_good_is_one():
    assert scoring.discipline_value(
        {"paid": 0, "overpaid": 0, "partial": 0, "notpaid": 5, "refund": 0},
        {"paid": 0, "partial": 0, "notpaid": 0},
    ) == 0.0
    assert scoring.discipline_value(
        {"paid": 3, "overpaid": 1, "partial": 0, "notpaid": 0, "refund": 9},
        {"paid": 0, "partial": 0, "notpaid": 0},
    ) == 1.0


def test_discipline_merges_retail_with_distinct_enum_mapping():
    regular = {"paid": 0, "overpaid": 0, "partial": 0, "notpaid": 2, "refund": 0}
    retail = {"paid": 2, "partial": 0, "notpaid": 0}
    v = scoring.discipline_value(regular, retail)
    assert v == pytest.approx(2 / 4)


def test_discipline_no_sales_defaults_to_one():
    assert scoring.discipline_value(
        {"paid": 0, "overpaid": 0, "partial": 0, "notpaid": 0, "refund": 0},
        {"paid": 0, "partial": 0, "notpaid": 0},
    ) == 1.0


def test_debt_load_picks_live_proxy_when_sync_blocked():
    v = scoring.debt_load_value(
        sync_live=False, overdue_eur=99999.0, turnover_eur=0.0,
        open_unpaid_count=3, total_sales_count=12,
    )
    assert v == pytest.approx(1 - 3 / 12)


def test_debt_load_uses_overdue_when_sync_live():
    v = scoring.debt_load_value(
        sync_live=True, overdue_eur=5000.0, turnover_eur=10000.0,
        open_unpaid_count=999, total_sales_count=1,
    )
    expected = 1.0 - max(0.0, 0.5 - scoring.DEBT_LOAD_HEALTHY_FLOOR) / scoring.DEBT_LOAD_TAIL_SPAN
    assert v == pytest.approx(expected)


def test_debt_load_healthy_floor_exempts_low_overdue_ratio():
    """Ratio at or below the healthy floor scores a full 1.0 (carried trade credit, not distress)."""
    floor = scoring.DEBT_LOAD_HEALTHY_FLOOR
    assert scoring.debt_load_value(True, floor * 10000.0, 10000.0, 0, 0) == 1.0
    assert scoring.debt_load_value(True, (floor - 0.05) * 10000.0, 10000.0, 0, 0) == 1.0


def test_debt_load_decays_linearly_and_clips_at_floor_plus_span():
    """Above the floor, debt_load = 1 - (ratio-floor)/span, hitting 0 exactly at floor+span."""
    floor, span = scoring.DEBT_LOAD_HEALTHY_FLOOR, scoring.DEBT_LOAD_TAIL_SPAN
    mid_ratio = floor + span / 2.0
    assert scoring.debt_load_value(True, mid_ratio * 1000.0, 1000.0, 0, 0) == pytest.approx(0.5)
    clip_ratio = floor + span
    assert scoring.debt_load_value(True, clip_ratio * 1000.0, 1000.0, 0, 0) == pytest.approx(0.0)


def test_debt_load_ge_one_tail_keeps_spread_not_collapsed():
    """Two clients with ratio>=1 but below the clip point get DISTINCT (non-zero) scores."""
    floor, span = scoring.DEBT_LOAD_HEALTHY_FLOOR, scoring.DEBT_LOAD_TAIL_SPAN
    assert floor + span > 1.0  # the clip point must exceed 1.0 for the tail to retain spread
    near = scoring.debt_load_value(True, 1.05 * 1000.0, 1000.0, 0, 0)
    far = scoring.debt_load_value(True, 1.40 * 1000.0, 1000.0, 0, 0)
    assert near > far > 0.0
    deep = scoring.debt_load_value(True, 26.0 * 1000.0, 1000.0, 0, 0)
    assert deep == 0.0


def test_debt_load_clamps_and_handles_zero_denominators():
    assert scoring.debt_load_value(True, 50000.0, 10000.0, 0, 0) == 0.0
    assert scoring.debt_load_value(True, 0.0, 0.0, 0, 0) == 1.0
    assert scoring.debt_load_value(False, 0.0, 0.0, 0, 0) == 1.0


def test_activity_recency_none_is_handled():
    v = scoring.activity_value(order_count=24, recency_days=None)
    assert v == pytest.approx(0.5 * 0.0 + 0.5 * 1.0)


def test_activity_full():
    v = scoring.activity_value(order_count=24, recency_days=0)
    assert v == pytest.approx(1.0)


def test_tenure_and_return_quality_clamp():
    assert scoring.tenure_value(48) == 1.0
    assert scoring.tenure_value(12) == pytest.approx(0.5)
    assert scoring.return_quality_value(0.0) == 1.0
    assert scoring.return_quality_value(0.25) == pytest.approx(0.5)
    assert scoring.return_quality_value(0.9) == 0.0


def test_band_boundaries():
    assert scoring.band_for(100) == Rating.A
    assert scoring.band_for(80) == Rating.A
    assert scoring.band_for(79) == Rating.B
    assert scoring.band_for(65) == Rating.B
    assert scoring.band_for(64) == Rating.C
    assert scoring.band_for(45) == Rating.C
    assert scoring.band_for(44) == Rating.D
    assert scoring.band_for(0) == Rating.D


def test_caps_hard_40_when_over_limit():
    capped, caps = scoring.apply_caps(95.0, max_utilization=1.5, is_blocked=False)
    assert capped == 40.0
    assert caps == [CapType.UTILIZATION_HARD_40]


def test_caps_soft_60_when_above_90pct():
    capped, caps = scoring.apply_caps(95.0, max_utilization=0.95, is_blocked=False)
    assert capped == 60.0
    assert caps == [CapType.UTILIZATION_SOFT_60]


def test_caps_no_utilization_cap_below_threshold():
    capped, caps = scoring.apply_caps(95.0, max_utilization=0.5, is_blocked=False)
    assert capped == 95.0
    assert caps == []


def test_caps_blocked_halves_after_utilization_cap():
    capped, caps = scoring.apply_caps(95.0, max_utilization=1.5, is_blocked=True)
    assert capped == 20.0
    assert caps == [CapType.UTILIZATION_HARD_40, CapType.BLOCKED_HALF]


def test_caps_cap_only_lowers_never_raises():
    capped, caps = scoring.apply_caps(30.0, max_utilization=1.5, is_blocked=False)
    assert capped == 30.0
    assert caps == [CapType.UTILIZATION_HARD_40]


def test_max_limit_utilization_ignores_uncontrolled():
    agreements = [
        {"is_control_amount": False, "amount_debt": 1000.0, "limit_utilization": 5.0},
        {"is_control_amount": True, "amount_debt": 0.0, "limit_utilization": None},
        {"is_control_amount": True, "amount_debt": 1000.0, "limit_utilization": 0.7},
        {"is_control_amount": True, "amount_debt": 1000.0, "limit_utilization": 0.95},
    ]
    assert scoring.max_limit_utilization(agreements) == 0.95
    assert scoring.max_limit_utilization([]) is None


def test_sub_factor_points_are_weight_times_value_times_100():
    sf = scoring._sub_factor(0.5, scoring.W_DISCIPLINE)
    assert sf.value == 0.5
    assert sf.points == pytest.approx(0.35 * 0.5 * 100)


def _full_repo_stub(monkeypatch, *, sync_live: bool, synthetic_calls: list,
                    is_blocked: bool = False, utilization: float | None = None,
                    open_count: int = 1):
    import app.services.solvency.score as s

    monkeypatch.setattr(s.repo, "payment_status_counts", lambda *a, **k: {
        "paid": 10, "overpaid": 0, "partial": 0, "notpaid": 0, "refund": 0})
    monkeypatch.setattr(s.repo, "retail_payment_status_counts", lambda *a, **k: {
        "paid": 0, "partial": 0, "notpaid": 0})
    monkeypatch.setattr(s.repo, "debt_sync_is_live", lambda *a, **k: sync_live)

    def turnover(client_id, as_of, window, fx):
        synthetic_calls.append(("turnover", fx))
        return 10000.0
    monkeypatch.setattr(s.repo, "turnover_eur", turnover)
    monkeypatch.setattr(s.repo, "open_unpaid_stats", lambda *a, **k: {
        "open_count": open_count, "max_age_days": 10, "avg_age_days": 5.0})
    monkeypatch.setattr(s.repo, "total_sales_count", lambda *a, **k: 10)

    def overdue(client_id, as_of, fx):
        synthetic_calls.append(("overdue", fx))
        return 500.0
    monkeypatch.setattr(s.repo, "overdue_amount_eur", overdue)
    monkeypatch.setattr(s.repo, "activity_stats", lambda *a, **k: {
        "order_count": 24, "tenure_months": 36, "recency_days": 0})
    monkeypatch.setattr(s.repo, "return_qty_rate", lambda *a, **k: 0.0)
    monkeypatch.setattr(s.repo, "credit_limit_utilization", lambda *a, **k: (
        [] if utilization is None else
        [{"is_control_amount": True, "amount_debt": 1000.0, "limit_utilization": utilization}]))
    monkeypatch.setattr(s.repo, "client_flags", lambda *a, **k: {"is_blocked": is_blocked})


def test_compute_score_picks_proxy_source_when_blocked(monkeypatch):
    calls: list = []
    _full_repo_stub(monkeypatch, sync_live=False, synthetic_calls=calls)
    comp = scoring.compute_score(42, "2026-06-01", 12)
    assert comp.debt_load_source == DebtLoadSource.LIVE_PROXY
    assert not any(c[0] == "overdue" for c in calls)


def test_compute_score_uses_debt_table_when_live(monkeypatch):
    calls: list = []
    _full_repo_stub(monkeypatch, sync_live=True, synthetic_calls=calls)
    comp = scoring.compute_score(42, "2026-06-01", 12)
    assert comp.debt_load_source == DebtLoadSource.DEBT_TABLE
    assert any(c[0] == "overdue" for c in calls)


def test_compute_score_pins_fx_date(monkeypatch):
    from app.core import config
    config.get_settings.cache_clear()
    monkeypatch.setenv("FX_SNAPSHOT_DATE", "2025-01-01")
    monkeypatch.setenv("DB_PASSWORD", "x")
    calls: list = []
    _full_repo_stub(monkeypatch, sync_live=True, synthetic_calls=calls)
    scoring.compute_score(42, "2026-06-01", 12)
    assert all(fx == "2025-01-01" for _, fx in calls)
    config.get_settings.cache_clear()


def test_compute_score_hard_cap_and_blocked(monkeypatch):
    calls: list = []
    _full_repo_stub(monkeypatch, sync_live=False, synthetic_calls=calls,
                    is_blocked=True, utilization=1.5)
    comp = scoring.compute_score(42, "2026-06-01", 12)
    assert CapType.UTILIZATION_HARD_40 in comp.caps_applied
    assert CapType.BLOCKED_HALF in comp.caps_applied
    assert comp.score == 20
    assert comp.rating == Rating.D


def test_compute_score_clean_client_is_band_a(monkeypatch):
    calls: list = []
    _full_repo_stub(monkeypatch, sync_live=False, synthetic_calls=calls, open_count=0)
    comp = scoring.compute_score(42, "2026-06-01", 12)
    assert comp.score == 100
    assert comp.rating == Rating.A
    assert comp.caps_applied == []


def test_synthetic_line_excluded_from_turnover_repository():
    import inspect

    from app.data import solvency_repository
    src = inspect.getsource(solvency_repository.turnover_eur)
    assert "oi.ProductID NOT IN {ph}" in src
    assert "**syn" in src
    by_ccy = inspect.getsource(solvency_repository.turnover_eur_by_currency)
    assert "oi.ProductID NOT IN {ph}" in by_ccy
    assert "**syn" in by_ccy


def test_synthetic_not_in_clause_is_parameterized_set():
    """The synthetic exclusion is a parameterized NOT IN over the full configured set."""
    from app.data import solvency_repository as repo
    ph, params = repo._synthetic_not_in()
    assert ph.startswith("(:synthetic")
    assert set(params.values()) == repo.get_settings().synthetic_line_product_ids
