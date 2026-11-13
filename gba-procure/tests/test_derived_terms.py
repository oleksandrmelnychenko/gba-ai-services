"""Derived minimum-order terms: enabled only when no sanity check objects."""
from __future__ import annotations

import pytest

from app.services.replenishment import derived_terms as dt
from app.services.replenishment import policy

AS_OF = "2026-11-13"


def _docs(*quantities, date="2026-06-01"):
    return [(date, 100 + i, float(q)) for i, q in enumerate(quantities)]


def _term(quantities, pack=None, sold=1000.0, unit="шт", date="2026-06-01"):
    return dt.evaluate(7, 1, _docs(*quantities, date=date), pack, sold, unit, AS_OF)


def test_clean_term_is_validated_with_its_pack():
    term = _term([20, 20, 40, 60], pack=20)
    assert term.validated
    assert term.moq == 20 and term.pack == 20 and term.effective_moq == 20
    assert term.as_plan_term()["moq_basis"] == "derived_validated"


def test_other_documents_must_mostly_be_multiples_of_the_minimum():
    assert _term([20, 30, 50, 70]).flags == (dt.FLAG_NOT_MULTIPLES_OF_MINIMUM,)
    assert _term([20, 40, 50]).validated
    assert _term([1, 3, 7]).validated


def test_minimum_above_a_year_of_sales_or_without_sales_is_held():
    assert _term([20, 20, 40], sold=19.0).flags == (dt.FLAG_ABOVE_YEAR_SALES,)
    assert _term([20, 20, 40], sold=0.0).flags == (dt.FLAG_NO_SALES,)
    assert _term([20, 20, 40], sold=20.0).validated


def test_the_year_of_sales_is_compared_with_the_pack_rounded_minimum():
    term = _term([100, 200, 300], pack=100, sold=150.0)
    assert term.validated
    assert _term([100, 200, 300], pack=100, sold=99.0).flags == (dt.FLAG_ABOVE_YEAR_SALES,)


def test_source_document_older_than_eighteen_months_is_held():
    assert _term([20, 20, 40], date="2025-05-12").flags == (dt.FLAG_SOURCE_OLD,)
    assert _term([20, 20, 40], date="2025-05-13").validated


def test_the_source_document_is_the_latest_one_with_the_minimum():
    documents = [("2025-02-01", 1, 20.0), ("2026-03-01", 2, 20.0), ("2026-04-01", 3, 40.0)]
    term = dt.evaluate(7, 1, documents, None, 500.0, "шт", AS_OF)
    assert term.source_document_date == "2026-03-01"
    assert term.validated


def test_pack_from_the_card_is_not_applied_when_deliveries_ignore_it():
    term = _term([20, 20, 150, 150], pack=100)
    assert dt.FLAG_MINIMUM_NOT_PACK_MULTIPLE in term.flags
    assert dt.FLAG_DELIVERIES_NOT_PACK_MULTIPLES in term.flags
    assert not term.validated


def test_minimum_off_the_pack_grid_is_held_even_when_deliveries_follow_it():
    term = _term([50, 100, 200, 300], pack=100)
    assert term.flags == (dt.FLAG_MINIMUM_NOT_PACK_MULTIPLE,)


def test_a_pack_of_one_is_no_pack():
    term = _term([5, 5, 10], pack=1)
    assert term.pack is None and term.validated


def test_fractional_units_keep_a_fractional_minimum():
    assert _term([2.5, 5.0, 7.5], unit="кг").effective_moq == 2.5
    assert _term([2.5, 5.0, 7.5], unit="шт").effective_moq == 3.0


@pytest.mark.derived_terms
def test_only_validated_terms_reach_the_plan(monkeypatch):
    evidence = {
        (7, 1): {"pack": 20.0, "documents": _docs(20, 20, 40)},
        (7, 2): {"pack": 100.0, "documents": _docs(20, 20, 150, 150)},
        (7, 3): {"pack": None, "documents": _docs(5, 5, 10)},
        (8, 1): {"pack": None, "documents": _docs(6, 6, 12)},
    }
    monkeypatch.setattr(dt.repo, "supply_document_quantities", lambda *a, **k: evidence)
    monkeypatch.setattr(dt.repo, "net_units_sold", lambda ids, *a: {1: 500.0, 2: 500.0, 3: 0.0})
    terms = dt.validated_terms(AS_OF, {7: [1, 2, 3], 9: [1]}, {1: "шт", 2: "шт", 3: "шт"})
    assert set(terms) == {7}
    assert set(terms[7]) == {1}
    assert terms[7][1]["moq"] == 20.0 and terms[7][1]["order_multiple"] == 20.0
    found = dt.derive(AS_OF, {7: [1, 2, 3]}, {1: "шт", 2: "шт", 3: "шт"})
    assert dt.summary(found) == {
        "derived": 3,
        "validated": 1,
        "validated_with_pack_multiple": 1,
        "held_for_review": 2,
        "flags": {
            dt.FLAG_DELIVERIES_NOT_PACK_MULTIPLES: 1,
            dt.FLAG_MINIMUM_NOT_PACK_MULTIPLE: 1,
            dt.FLAG_NO_SALES: 1,
            dt.FLAG_NOT_MULTIPLES_OF_MINIMUM: 1,
        },
    }


@pytest.mark.derived_terms
def test_derivation_can_be_switched_off(monkeypatch):
    settings = dt.get_settings().model_copy(update={"derived_moq_enabled": False})
    monkeypatch.setattr(dt, "get_settings", lambda: settings)
    monkeypatch.setattr(
        dt.repo, "supply_document_quantities", lambda *a, **k: pytest.fail("must not read")
    )
    assert dt.validated_terms(AS_OF, {7: [1]}, {}) == {}


def test_buyer_term_wins_and_a_seeded_term_yields_to_the_validated_derivation():
    derived = {"moq": 20.0, "order_multiple": None, "moq_basis": "derived_validated"}
    buyer = {"moq": 5.0, "source": "buyer"}
    seeded = {"moq": 3.0, "order_multiple": 100.0, "source": "derived"}
    assert policy._order_term(buyer, derived) is buyer
    assert policy._order_term(seeded, derived) is derived
    assert policy._order_term(seeded, None) is None
    assert policy._order_term({"unit_cost_override": 2.0}, derived) is derived
    assert policy._order_term(None, None) is None
