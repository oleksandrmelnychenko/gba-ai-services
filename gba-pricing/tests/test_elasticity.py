"""Pure elasticity-math + secondary-signal gating tests (no DB).

Covers: a clean synthetic constant-elasticity panel recovers e; UoM-outlier reject; degenerate
panels return NO estimate (not a fabricated number); the economic-sanity gate; the markup-rule
elastic price; and that the secondary signal NEVER mutates the A+B recommended_price.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.services.pricing import elasticity as el
from app.services.pricing import recommend as engine


def _panel(e_true: float, n_agr: int = 12, n_mon: int = 6, seed: int = 0) -> list[el.PanelCell]:
    """Synthetic constant-elasticity panel: ln Q = a - e*ln p + agr_fe + mon_fe, with genuine
    WITHIN price variation (each agreement-month gets its own price draw)."""
    rng = np.random.default_rng(seed)
    agr_fe = {a: rng.normal(0, 0.3) for a in range(n_agr)}
    mon_fe = {m: rng.normal(0, 0.2) for m in range(n_mon)}
    cells = []
    for a in range(n_agr):
        for m in range(n_mon):
            price = math.exp(rng.normal(2.7, 0.25))
            lnq = 5.0 - e_true * math.log(price) + agr_fe[a] + mon_fe[m] + rng.normal(0, 0.02)
            cells.append(el.PanelCell(a, f"2025-{m + 1:02d}", math.exp(lnq), price))
    return cells


def test_fit_recovers_known_elasticity_on_clean_panel():
    fit = el.fit_elasticity(_panel(1.8))
    assert fit.elasticity is not None
    assert fit.note == "ok"
    assert fit.elasticity == pytest.approx(1.8, abs=0.15)
    assert fit.r_squared is not None and fit.r_squared > 0.9


def test_fit_returns_none_on_empty_or_degenerate():
    assert el.fit_elasticity([]).elasticity is None
    tied = [el.PanelCell(a, "2025-01", 10.0, 5.0) for a in range(8)]
    fit = el.fit_elasticity(tied)
    assert fit.elasticity is None
    assert fit.note in ("insufficient-price-levels", "no-within-price-variation",
                        "insufficient-residual-df", "rank-deficient")


def test_fit_refuses_when_price_collapsed_by_fixed_effects():
    """Each agreement has ONE constant price -> after agreement FE there is no within price
    variation, so e is unidentified and the fit must refuse (None), not emit noise."""
    cells = []
    for a in range(10):
        p = 5.0 + a
        for m in range(4):
            cells.append(el.PanelCell(a, f"2025-{m + 1:02d}", 100.0 / p, p))
    fit = el.fit_elasticity(cells)
    assert fit.elasticity is None
    assert fit.note == "no-within-price-variation"


def test_mad_rejects_uom_outlier_cell():
    clean = _panel(1.5, seed=3)
    n0 = len(clean)
    clean.append(el.PanelCell(99, "2025-01", 5000.0, 0.30))  # piece-price outlier
    fit = el.fit_elasticity(clean)
    assert fit.n_cells == n0 + 1
    assert fit.n_kept <= n0
    assert fit.elasticity == pytest.approx(1.5, abs=0.3)


def test_elastic_optimal_price_markup_rule():
    assert el.elastic_optimal_price(2.0, 10.0) == pytest.approx(20.0)
    assert el.elastic_optimal_price(3.0, 10.0) == pytest.approx(15.0)
    assert el.elastic_optimal_price(1.0, 10.0) is None
    assert el.elastic_optimal_price(0.6, 10.0) is None
    assert el.elastic_optimal_price(None, 10.0) is None
    assert el.elastic_optimal_price(2.0, None) is None


def test_is_sane_elasticity_band():
    assert el.is_sane_elasticity(1.8)
    assert el.is_sane_elasticity(0.5)
    assert el.is_sane_elasticity(5.0)
    assert not el.is_sane_elasticity(-6.0)
    assert not el.is_sane_elasticity(0.0)
    assert not el.is_sane_elasticity(20.0)
    assert not el.is_sane_elasticity(None)


def test_engine_gates_insane_elasticity_to_none():
    e, src, price = engine.elasticity_outputs(
        {"elasticity": -6.0, "source": engine.ELAS_SOURCE_PER_SKU}, unit_cost_eur=10.0
    )
    assert e is None
    assert src == engine.ELAS_SOURCE_NONE
    assert price is None


def test_engine_surfaces_sane_elasticity_and_price():
    e, src, price = engine.elasticity_outputs(
        {"elasticity": 2.0, "source": engine.ELAS_SOURCE_PER_SKU}, unit_cost_eur=10.0
    )
    assert e == 2.0
    assert src == engine.ELAS_SOURCE_PER_SKU
    assert price == pytest.approx(20.0)


def test_engine_no_estimate_yields_none_fields():
    e, src, price = engine.elasticity_outputs(None, unit_cost_eur=10.0)
    assert e is None
    assert src == engine.ELAS_SOURCE_NONE
    assert price is None


def _build(elasticity_estimate=None, **over):
    base = dict(
        product_id=7, client_agreement_netuid="ca-uid", baseline=20.0, marked_up=20.0,
        cost={"unit_cost_eur": 10.0, "lot_count": 4, "cost_source": "median_onhand"},
        peer={"p25": 17.0, "p50": 18.5, "p75": 19.5, "n": 12},
        segment={"p75": 12.0, "p90": 18.0, "n": 40},
        target_margin_pct=12.0, as_of_date="2026-06-15",
    )
    base.update(over)
    return engine.build_recommendation(elasticity_estimate=elasticity_estimate, **base)


def test_elasticity_is_secondary_never_changes_recommended_price():
    """The PRIMARY recommended_price (A+B clamp -> peer P50 = 18.5) is identical with and without a
    sane elasticity. The elastic price is exposed only as a separate advisory field."""
    base = _build(elasticity_estimate=None)
    with_e = _build(elasticity_estimate={"elasticity": 2.0, "source": engine.ELAS_SOURCE_PER_SKU})
    assert base.recommended_price == with_e.recommended_price == 18.5
    assert base.suggested_discount_pct == with_e.suggested_discount_pct
    assert base.elasticity is None
    assert with_e.elasticity == 2.0
    assert with_e.elasticity_source == engine.ELAS_SOURCE_PER_SKU
    assert with_e.elastic_optimal_price == pytest.approx(20.0)


def test_insane_elasticity_does_not_surface_in_recommendation():
    reco = _build(elasticity_estimate={"elasticity": -7.0, "source": engine.ELAS_SOURCE_PER_SKU})
    assert reco.recommended_price == 18.5
    assert reco.elasticity is None
    assert reco.elasticity_source == engine.ELAS_SOURCE_NONE
    assert reco.elastic_optimal_price is None
