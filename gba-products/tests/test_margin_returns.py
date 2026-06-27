"""Pure unit tests for Lens 4 margin/returns rankings (no DB, synthetic portfolio rows)."""
from __future__ import annotations

from app.services import margin_returns as mr


def _row(pid, margin_pct, revenue, annual_units=0.0, return_rate=0.0,
         unit_cost=None, avg_price=None, band="healthy"):
    return {
        "product_id": pid,
        "margin_pct": margin_pct,
        "revenue_eur": revenue,
        "annual_units": annual_units,
        "return_rate": return_rate,
        "unit_cost_eur": unit_cost,
        "avg_price_eur": avg_price,
        "band": band,
        "lifecycle": "mature",
        "abc": "A",
        "health": 70.0,
    }


def _rows():
    return [
        _row(1, 0.40, 10000.0, annual_units=100, return_rate=0.0),    # margin €4000
        _row(2, 0.50, 2000.0, annual_units=50, return_rate=0.10),     # margin €1000, high returns
        _row(3, 0.05, 50000.0, annual_units=500, return_rate=0.02),   # thin %, big margin € (€2500)
        _row(4, -0.20, 3000.0, annual_units=30, return_rate=0.30),    # NEGATIVE margin + worst returns
        _row(5, None, 8000.0, annual_units=80, return_rate=0.0),      # unknown cost -> excluded from margin
        _row(6, 0.10, 0.0, annual_units=0, return_rate=0.0),          # no sales -> excluded from returns
    ]


def test_margin_leaders_by_euro_contribution():
    out = mr.margin_leaders(_rows(), limit=10)
    ids = [r["product_id"] for r in out]
    assert ids[0] == 1            # €4000 contribution is the top
    assert 5 not in ids           # unknown margin excluded
    assert out[0]["margin_eur"] == 4000.0


def test_margin_leaders_respects_limit():
    assert len(mr.margin_leaders(_rows(), limit=2)) == 2


def test_margin_laggards_lowest_first():
    out = mr.margin_laggards(_rows(), limit=10)
    assert out[0]["product_id"] == 4    # -0.20 is the lowest margin%
    assert out[1]["product_id"] == 3    # 0.05 next
    assert all(r["margin_pct"] is not None for r in out)


def test_negative_margin_alert():
    out = mr.negative_margin(_rows())
    assert [r["product_id"] for r in out] == [4]
    assert out[0]["margin_pct"] < 0


def test_negative_margin_empty_when_none():
    rows = [_row(1, 0.10, 100.0), _row(2, None, 100.0)]
    assert mr.negative_margin(rows) == []


def test_high_returns_threshold_and_order():
    out = mr.high_returns(_rows(), min_rate=0.05, limit=10)
    ids = [r["product_id"] for r in out]
    assert ids == [4, 2]              # 0.30 then 0.10; others below 0.05 or no sales
    assert out[0]["returned_units"] == 9.0   # 0.30 * 30
    assert 6 not in ids               # zero annual_units excluded even if rate qualified


def test_high_returns_respects_limit():
    assert len(mr.high_returns(_rows(), min_rate=0.0, limit=1)) == 1


def test_summary_weighted_margin_and_negatives():
    s = mr.margin_returns_summary(_rows())
    assert s["total_skus"] == 6
    assert s["skus_with_known_margin"] == 5
    assert s["skus_unknown_margin"] == 1
    assert s["negative_margin_skus"] == 1
    assert s["eur_at_negative_margin"] == 3000.0
    # weighted avg = Σ(margin_pct*rev)/Σrev over known rows
    # = (4000 + 1000 + 2500 - 600 + 0) / (10000+2000+50000+3000+0) = 6900/65000
    assert s["weighted_avg_margin_pct"] == round(6900.0 / 65000.0, 4)


def test_summary_overall_return_rate():
    s = mr.margin_returns_summary(_rows())
    # returned = 100*0 + 50*.10 + 500*.02 + 30*.30 + 80*0 + 0 = 0+5+10+9 = 24
    # units = 100+50+500+30+80+0 = 760
    assert s["total_returned_units"] == 24.0
    assert s["total_annual_units"] == 760.0
    assert s["overall_return_rate"] == round(24.0 / 760.0, 4)


def test_summary_all_unknown_margin_safe():
    rows = [_row(1, None, 100.0, annual_units=10), _row(2, None, 200.0, annual_units=20)]
    s = mr.margin_returns_summary(rows)
    assert s["weighted_avg_margin_pct"] is None
    assert s["negative_margin_skus"] == 0
    assert s["overall_return_rate"] == 0.0


def test_empty_rows_no_crash():
    assert mr.margin_leaders([]) == []
    assert mr.margin_laggards([]) == []
    assert mr.negative_margin([]) == []
    assert mr.high_returns([]) == []
    s = mr.margin_returns_summary([])
    assert s["total_skus"] == 0
    assert s["weighted_avg_margin_pct"] is None
    assert s["overall_return_rate"] == 0.0
