"""Offline validation harness for the per-SKU price-elasticity lever (gba-pricing).

NOT a serving path: fits e on PRE-period data at a historical as_of, then checks against the
POST-period whether the implied constant-elasticity curve predicts the DIRECTION of the quantity
move better than a naive constant (last-period mean) baseline. Reports coverage and fit quality so
the adopt/hold verdict rests on measured numbers, never faith.

Method (per estimable SKU = bands A/B):
  pre  window  = [as_of - window, as_of)
  post window  = [as_of, as_of + post_months]
  1. fit_elasticity on the PRE panel (ln Q ~ -e ln price + agreement FE + month FE).
  2. economic sanity: e>0 and e in [lo,hi].
  3. directional test on SKUs WITH post-period sales: between the pre-period volume-weighted price
     and the post-period one, did Q move the way a downward-sloping demand curve predicts?
       price up  => model predicts Q down; price down => Q up; ~flat price => excluded (no signal).
     elastic_correct = sign(dQ) == -sign(dP). The CONSTANT baseline always predicts dQ=0 (no
     directional call), so its directional accuracy on the same movers is 0 by construction -- we
     also report a coin-flip reference (0.5). pseudo-R^2 of the pre fit is reported alongside.

Usage:
  .venv/bin/python scripts/elasticity_backtest.py [as_of=YYYY-MM-DD] [pre_months] [post_months]
                                                  [max_skus] [band=AB|A]
Defaults: as_of=2026-01-01 pre_months=9 post_months=5 max_skus=400 band=AB
Reads the dev DB via the service read-only creds (.env). Read-only; no live service touched.
"""
from __future__ import annotations

import json
import sys
from datetime import date

import numpy as np

from app.data.db import query
from app.services.pricing import elasticity as el


def _estimable_products(as_of: str, pre_months: int, min_lines: int, max_skus: int) -> list[int]:
    rows = query(
        """
        SELECT TOP (:max_skus) oi.ProductID AS pid, COUNT(*) AS n_lines
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.Sale s ON s.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> 25422404
              AND oi.PricePerItem > 0
              AND oi.Qty > 0
              AND s.Created < :asof
              AND s.Created >= DATEADD(month, :neg, :asof)
        GROUP BY oi.ProductID
        HAVING COUNT(*) >= :min_lines
        ORDER BY COUNT(*) DESC
        """,
        {"asof": as_of, "neg": -pre_months, "min_lines": min_lines, "max_skus": max_skus},
    )
    return [int(r["pid"]) for r in rows]


def _pre_panel(pid: int, as_of: str, pre_months: int) -> list[el.PanelCell]:
    rows = query(
        """
        SELECT o.ClientAgreementID AS agreement_id,
               FORMAT(s.Created, 'yyyy-MM') AS month,
               SUM(oi.Qty) AS qty,
               SUM(oi.Qty * oi.PricePerItem) / NULLIF(SUM(oi.Qty), 0) AS price
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.Sale s ON s.OrderID = o.ID
        WHERE oi.ProductID = :pid AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> 25422404 AND oi.PricePerItem > 0 AND oi.Qty > 0
              AND s.Created < :asof AND s.Created >= DATEADD(month, :neg, :asof)
        GROUP BY o.ClientAgreementID, FORMAT(s.Created, 'yyyy-MM')
        """,
        {"pid": pid, "asof": as_of, "neg": -pre_months},
    )
    return [
        el.PanelCell(int(r["agreement_id"]), str(r["month"]), float(r["qty"]), float(r["price"]))
        for r in rows
    ]


def _period_aggregate(pid: int, lo: str, hi: str) -> tuple[float, float] | None:
    """Volume-weighted price and total Qty for a product over [lo, hi). None if no valid sales."""
    rows = query(
        """
        SELECT SUM(oi.Qty) AS q,
               SUM(oi.Qty * oi.PricePerItem) / NULLIF(SUM(oi.Qty), 0) AS p
        FROM dbo.OrderItem oi
        JOIN dbo.[Order] o ON o.ID = oi.OrderID
        JOIN dbo.Sale s ON s.OrderID = o.ID
        WHERE oi.ProductID = :pid AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> 25422404 AND oi.PricePerItem > 0 AND oi.Qty > 0
              AND s.Created >= :lo AND s.Created < :hi
        """,
        {"pid": pid, "lo": lo, "hi": hi},
    )
    if not rows or rows[0]["q"] is None or rows[0]["p"] is None:
        return None
    return float(rows[0]["p"]), float(rows[0]["q"])


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    return date(y, m % 12 + 1, min(d.day, 28))


def main() -> int:
    as_of = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    pre_months = int(sys.argv[2]) if len(sys.argv) > 2 else 9
    post_months = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    max_skus = int(sys.argv[4]) if len(sys.argv) > 4 else 400
    band = sys.argv[5].upper() if len(sys.argv) > 5 else "AB"
    min_lines = 500 if band == "A" else 100

    as_of_d = date.fromisoformat(as_of)
    post_hi = _add_months(as_of_d, post_months).isoformat()
    rel_flat = 0.01  # price moves under 1% carry no directional signal -> excluded

    pids = _estimable_products(as_of, pre_months, min_lines, max_skus)

    fitted: list[dict] = []
    elasticities: list[float] = []
    r2s: list[float] = []
    notes: dict[str, int] = {}
    n_attempt = len(pids)

    for pid in pids:
        cells = _pre_panel(pid, as_of, pre_months)
        fit = el.fit_elasticity(cells)
        notes[fit.note] = notes.get(fit.note, 0) + 1
        if fit.elasticity is None:
            continue
        fitted.append({"pid": pid, "e": fit.elasticity, "r2": fit.r_squared, "n_kept": fit.n_kept})
        elasticities.append(fit.elasticity)
        if fit.r_squared is not None:
            r2s.append(fit.r_squared)

    sane = [f for f in fitted if el.is_sane_elasticity(f["e"])]

    sane_pids = {f["pid"] for f in sane}
    movers = 0
    elastic_correct = 0
    comovement = 0
    sane_movers = 0
    sane_correct = 0
    for f in fitted:
        pid = f["pid"]
        pre = _period_aggregate(pid, _add_months(as_of_d, -pre_months).isoformat(), as_of)
        post = _period_aggregate(pid, as_of, post_hi)
        if pre is None or post is None:
            continue
        p0, q0 = pre
        p1, q1 = post
        if p0 <= 0 or q0 <= 0 or p1 <= 0 or q1 <= 0:
            continue
        dp = (p1 - p0) / p0
        if abs(dp) < rel_flat:
            continue
        dq = q1 - q0
        movers += 1
        correct = np.sign(dq) == -np.sign(dp)
        if correct:
            elastic_correct += 1
        if np.sign(dq) == np.sign(dp):
            comovement += 1
        if pid in sane_pids:
            sane_movers += 1
            if correct:
                sane_correct += 1

    def pctl(xs, q):
        return float(np.percentile(xs, q)) if xs else None

    report = {
        "as_of": as_of, "band": band, "min_lines": min_lines,
        "pre_months": pre_months, "post_months": post_months,
        "skus_attempted": n_attempt,
        "skus_fitted": len(fitted),
        "coverage_fitted_pct": round(100.0 * len(fitted) / n_attempt, 1) if n_attempt else None,
        "skus_sane": len(sane),
        "sane_pct_of_fitted": round(100.0 * len(sane) / len(fitted), 1) if fitted else None,
        "elasticity_p10": pctl(elasticities, 10),
        "elasticity_median": pctl(elasticities, 50),
        "elasticity_p90": pctl(elasticities, 90),
        "elasticity_min": min(elasticities) if elasticities else None,
        "elasticity_max": max(elasticities) if elasticities else None,
        "frac_positive": round(sum(1 for e in elasticities if e > 0) / len(elasticities), 3)
        if elasticities else None,
        "frac_in_0p5_5": round(sum(1 for e in elasticities if 0.5 <= e <= 5.0) / len(elasticities), 3)
        if elasticities else None,
        "pseudo_r2_median": pctl(r2s, 50),
        "directional_movers": movers,
        "directional_accuracy_elastic": round(elastic_correct / movers, 3) if movers else None,
        "price_qty_comovement_frac": round(comovement / movers, 3) if movers else None,
        "directional_movers_sane": sane_movers,
        "directional_accuracy_sane": round(sane_correct / sane_movers, 3) if sane_movers else None,
        "directional_accuracy_constant": 0.0,
        "directional_accuracy_coinflip": 0.5,
        "fit_notes": notes,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
