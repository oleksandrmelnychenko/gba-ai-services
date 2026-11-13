from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.core import serving_context
from app.domain.models import CapType, Rating
from app.risk import payment_behaviour as pb
from app.risk import score_index as si
from app.services.solvency import service

CARD_PATH = Path(si.__file__).resolve().parent / "artifacts" / "index_card.json"


def _invoice(sale_id: int, day: date, amount: float, grace: int = 7, legacy: bool = False) -> dict:
    return {
        "sale_id": sale_id,
        "date": day,
        "due": date.fromordinal(day.toordinal() + grace),
        "amount": amount,
        "legacy": legacy,
    }


def _credit(day: date, eur: float) -> dict:
    return {"date": day, "eur": eur}


def test_fifo_pays_oldest_invoice_first_and_keeps_the_rest_open():
    invoices = [_invoice(1, date(2026, 1, 1), 100.0), _invoice(2, date(2026, 1, 10), 100.0)]
    settled = pb.settle_fifo(invoices, [_credit(date(2026, 1, 20), 150.0)])
    assert settled[0]["open"] == pytest.approx(0.0)
    assert settled[0]["settled"] == date(2026, 1, 20)
    assert settled[1]["open"] == pytest.approx(50.0)
    assert settled[1]["settled"] is None


def test_fifo_prepayment_settles_on_the_invoice_date():
    settled = pb.settle_fifo(
        [_invoice(1, date(2026, 2, 1), 80.0)], [_credit(date(2026, 1, 5), 200.0)]
    )
    assert settled[0]["settled"] == date(2026, 2, 1)


def test_fifo_treats_fx_residue_within_tolerance_as_settled():
    settled = pb.settle_fifo(
        [_invoice(1, date(2026, 1, 1), 100.0)], [_credit(date(2026, 1, 3), 99.6)]
    )
    assert settled[0]["settled"] == date(2026, 1, 3)
    assert settled[0]["open"] == pytest.approx(0.4)


def test_behaviour_measures_delay_past_due_date():
    invoices = [
        _invoice(1, date(2026, 3, 1), 100.0),
        _invoice(2, date(2026, 4, 1), 100.0),
        _invoice(3, date(2026, 5, 1), 100.0),
    ]
    credits = [
        _credit(date(2026, 3, 5), 100.0),
        _credit(date(2026, 4, 28), 100.0),
        _credit(date(2026, 5, 8), 100.0),
    ]
    out = pb.behaviour_features(invoices, credits, date(2026, 6, 1))
    assert out["worst_delay_12m_days"] == 20.0
    assert out["share_late_invoices_12m"] == pytest.approx(1 / 3)
    assert out["matured_invoices_12m"] == 3.0


def test_behaviour_ignores_everything_after_the_observation_date():
    invoices = [_invoice(1, date(2026, 3, 1), 100.0), _invoice(2, date(2026, 7, 1), 500.0)]
    credits = [_credit(date(2026, 6, 20), 100.0)]
    out = pb.behaviour_features(invoices, credits, date(2026, 6, 1))
    assert out["worst_delay_12m_days"] == float((date(2026, 6, 1) - date(2026, 3, 8)).days)
    assert out["matured_invoices_12m"] == 1.0


def test_behaviour_excludes_legacy_and_small_invoices():
    invoices = [
        _invoice(1, date(2024, 12, 31), 900.0, grace=0, legacy=True),
        _invoice(2, date(2026, 5, 1), 5.0),
    ]
    out = pb.behaviour_features(invoices, [], date(2026, 6, 1))
    assert out == pb.empty_behaviour()


def test_behaviour_trend_compares_last_quarter_with_the_previous_one():
    invoices = [_invoice(1, date(2026, 1, 10), 100.0), _invoice(2, date(2026, 4, 20), 100.0)]
    credits = [_credit(date(2026, 1, 17), 100.0), _credit(date(2026, 5, 12), 100.0)]
    out = pb.behaviour_features(invoices, credits, date(2026, 6, 1))
    assert out["delay_trend_3m_days"] == 15.0


def _card() -> dict:
    return {
        "model": si.INDEX_MODEL_NAME,
        "model_version": si.INDEX_MODEL_VERSION,
        "run_id": "index-test",
        "components": {
            "overdue_now_eur": {"edges": [0.0, 100.0], "logodds": [-1.0, 0.5, 2.0]},
            "worst_delay_12m_days": {"edges": [7.0, 30.0], "logodds": [-2.0, 0.0, 1.0]},
        },
        "history_components": ["worst_delay_12m_days"],
        "scale": {"z_min": -3.0, "z_max": 3.0},
        "bands": {
            "A": {"min_score": 80, "max_score": 100, "pd": 0.001},
            "B": {"min_score": 60, "max_score": 79, "pd": 0.005},
            "C": {"min_score": 30, "max_score": 59, "pd": 0.02},
            "D": {"min_score": 0, "max_score": 29, "pd": 0.15},
        },
        "policy": {
            "severe_min_eur": 100.0,
            "severe_pd": 0.87,
            "delinquent_min_eur": 100.0,
            "delinquent_pd": 0.31,
        },
    }


def test_clean_payer_with_history_scores_100_in_band_a():
    out = si.score_index({"matured_invoices_12m": 12.0, "worst_delay_12m_days": 2.0}, _card())
    assert (out["score"], out["band"], out["pd"]) == (100, "A", 0.001)
    assert out["policy_overrides"] == []


def test_no_payment_history_is_neutral_not_perfect():
    out = si.score_index({}, _card())
    assert out["score"] == 67
    assert out["band"] == "B"
    history = next(c for c in out["contributions"] if c["feature"] == "worst_delay_12m_days")
    assert history["value"] is None and history["points"] == 0.0


def test_score_falls_as_overdue_and_delay_grow():
    base = {"matured_invoices_12m": 5.0}
    scores = [
        si.score_index({**base, "overdue_eur_1_30": overdue, "worst_delay_12m_days": delay}, _card())[
            "score"
        ]
        for overdue, delay in ((0.0, 0.0), (50.0, 10.0), (400.0, 45.0))
    ]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_debt_ninety_days_overdue_is_always_band_d():
    out = si.score_index(
        {"matured_invoices_12m": 5.0, "worst_delay_12m_days": 0.0, "overdue_eur_91_180": 150.0},
        _card(),
    )
    assert out["band"] == "D"
    assert out["score"] <= 29
    assert out["pd"] == 0.87
    assert out["policy_overrides"] == ["overdue_90_plus"]


def test_debt_thirty_days_overdue_cannot_rate_above_c():
    out = si.score_index(
        {"matured_invoices_12m": 5.0, "worst_delay_12m_days": 0.0, "overdue_eur_31_60": 150.0},
        _card(),
    )
    assert out["band"] == "C"
    assert out["score"] <= 59
    assert out["pd"] == 0.31
    assert out["policy_overrides"] == ["overdue_30_plus"]


def test_small_old_residue_does_not_trigger_the_policy():
    out = si.score_index(
        {"matured_invoices_12m": 5.0, "worst_delay_12m_days": 0.0, "overdue_eur_180plus": 0.7},
        _card(),
    )
    assert out["policy_overrides"] == []
    assert out["band"] in ("A", "B")
    real = si.score_index(
        {"matured_invoices_12m": 5.0, "overdue_eur_180plus": 0.7}, json.loads(CARD_PATH.read_text())
    )
    assert real["score"] == 100 and real["policy_overrides"] == []


def test_index_inputs_derive_ratios_from_served_features():
    inputs = si.index_inputs(
        {
            "total_debt_eur": 600.0,
            "turnover_eur_12mo": 2400.0,
            "credit_limit_eur": 1000.0,
            "overdue_eur_1_30": 40.0,
            "overdue_eur_61_90": 10.0,
        }
    )
    assert inputs["debt_to_monthly_turnover"] == pytest.approx(3.0)
    assert inputs["limit_utilization"] == pytest.approx(0.6)
    assert inputs["overdue_now_eur"] == pytest.approx(50.0)
    assert si.index_inputs({"total_debt_eur": 50.0})["debt_to_monthly_turnover"] == 12.0
    residue = si.index_inputs({"total_debt_eur": 5.0, "credit_limit_eur": 1.0})
    assert residue["debt_to_monthly_turnover"] == 0.0 and residue["limit_utilization"] == 0.0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.pop("policy"),
        lambda c: c.update(model_version="creditscore-v3"),
        lambda c: c["components"]["overdue_now_eur"]["logodds"].pop(),
        lambda c: c["bands"]["B"].update(min_score=90),
    ],
)
def test_malformed_card_is_rejected(mutate):
    card = _card()
    mutate(card)
    with pytest.raises((si.IndexCardError, KeyError)):
        si.validate_card(card)


def test_shipped_card_is_valid_monotone_and_passed_its_gates():
    card = si.validate_card(json.loads(CARD_PATH.read_text()))
    assert card["gates"]["passed"] is True
    assert card["panel"]["event_clients"] >= card["gates"]["min_event_clients"]
    assert card["validation"]["oot_auc_ci95"][0] >= card["gates"]["min_oot_auc_ci_lower"]
    for comp in card["components"].values():
        assert comp["logodds"] == sorted(comp["logodds"])
    rates = [card["bands"][b]["pd"] for b in ("A", "B", "C", "D")]
    assert rates == sorted(rates)
    assert card["policy"]["severe_pd"] > card["policy"]["delinquent_pd"] > rates[1]


def test_shipped_card_separates_clean_dormant_and_late_payers():
    card = json.loads(CARD_PATH.read_text())
    clean = si.score_index({"matured_invoices_12m": 20.0, "turnover_eur_12mo": 5000.0}, card)
    dormant = si.score_index({}, card)
    late = si.score_index(
        {
            "matured_invoices_12m": 20.0,
            "turnover_eur_12mo": 5000.0,
            "total_debt_eur": 900.0,
            "overdue_eur_1_30": 600.0,
            "oldest_overdue_days": 20.0,
            "worst_delay_12m_days": 45.0,
            "share_late_invoices_12m": 0.6,
            "delay_trend_3m_days": 12.0,
        },
        card,
    )
    assert clean["score"] == 100 and clean["band"] == "A"
    assert dormant["band"] == "B" and 60 <= dormant["score"] < 80
    assert late["band"] == "D"
    assert clean["score"] > dormant["score"] > late["score"]


@pytest.fixture
def _served_day(monkeypatch):
    monkeypatch.setattr(
        serving_context, "utc_now", lambda: datetime(2026, 6, 1, 9, tzinfo=UTC)
    )
    monkeypatch.setattr(service.cache, "get_many", lambda keys: [None] * len(keys))
    monkeypatch.setattr(service.cache, "set_many", lambda *a, **k: None)
    monkeypatch.setattr(service.repo, "turnover_eur_by_currency_many", lambda *a, **k: {})


def _wire_batch(monkeypatch, behaviour_calls: list):
    from app.risk.dataset import FEATURE_COLUMNS

    monkeypatch.setattr(
        service.repo,
        "client_gates",
        lambda ids: {c: service.repo.ClientGate(is_buyer=True, hopeless_debt=False) for c in ids},
    )
    monkeypatch.setattr(
        service.risk_dataset,
        "features_many",
        lambda ids, *a, **k: {c: {f: 0.0 for f in FEATURE_COLUMNS} for c in ids},
    )

    def behaviour(ids, as_of):
        behaviour_calls.append(list(ids))
        return {
            c: {**pb.empty_behaviour(), "matured_invoices_12m": 10.0, "worst_delay_12m_days": 50.0}
            for c in ids
        }

    monkeypatch.setattr(service.payment_behaviour, "behaviour_many", behaviour)


def test_model_version_switch_serves_the_index(monkeypatch, _served_day):
    calls: list = []
    _wire_batch(monkeypatch, calls)
    settings = service.get_settings().model_copy(update={"model_version": si.INDEX_MODEL_VERSION})
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    results, errors = service.score_batch([11, 12], "2026-06-01", 12, use_cache=False)
    assert errors == [] and calls == [[11, 12]]
    card = json.loads(CARD_PATH.read_text())
    for result in results:
        assert result.model_version == si.INDEX_MODEL_VERSION
        assert result.current_model_run_id == card["run_id"]
        assert {c.feature for c in result.contributions} == set(card["components"])
        assert result.rating in (Rating.A, Rating.B, Rating.C, Rating.D)
        assert 0 <= result.score <= 100 and 0.0 <= result.pd <= 1.0


def test_default_model_version_keeps_the_scorecard(monkeypatch, _served_day):
    calls: list = []
    _wire_batch(monkeypatch, calls)
    settings = service.get_settings().model_copy(update={"model_version": "creditscore-v3"})
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    results, _ = service.score_batch([11], "2026-06-01", 12, use_cache=False)
    assert calls == []
    assert results[0].model_version == "creditscore-v3"
    assert results[0].current_model_run_id.startswith("current-")


def test_policy_overrides_map_to_cap_types():
    assert CapType("overdue_90_plus") is CapType.OVERDUE_90_PLUS
    assert CapType("overdue_30_plus") is CapType.OVERDUE_30_PLUS
