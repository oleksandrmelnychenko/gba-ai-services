"""Portfolio builder — joins all per-SKU signals into one classified, health-scored table.

One heavy build per as_of (cached); the /assortment/* and /product/{id} endpoints slice it.
ABC is ranked by REVENUE-€ contribution (the standard, universally-computable basis — purchase cost
exists only for on-hand stock, so a margin-ABC would silently drop every non-stocked SKU). Margin% is
still computed per-SKU (where cost is known) and feeds the health-score.
"""
from __future__ import annotations

from app.core.config import get_settings
from app.data import signals_repository as sig
from app.services import classification as cl
from app.services import health_score
from app.services.stock_health import classify_band


def build_portfolio(as_of: str) -> dict:
    cfg = get_settings()

    stock = {int(r["product_id"]): r for r in sig.on_hand_stock()}
    vel = {int(r["product_id"]): r for r in sig.sales_velocity(as_of, cfg.velocity_window_days)}
    sold_recently = sig.sold_product_ids(as_of, cfg.dead_window_days)
    price = {int(r["product_id"]): float(r["avg_price_eur"] or 0)
             for r in sig.avg_sale_price_eur(as_of, cfg.dead_window_days)}
    rets = {int(r["product_id"]): float(r["returned_qty"] or 0)
            for r in sig.returns_for_products(as_of, cfg.return_window_days)}

    labels = cl.month_labels(as_of, cfg.classify_months)
    monthly: dict[int, dict[str, float]] = {}
    for r in sig.monthly_units(as_of, cfg.classify_months):
        monthly.setdefault(int(r["product_id"]), {})[r["ym"]] = float(r["units"] or 0)

    universe = set(stock) | set(monthly) | set(vel)
    rows: list[dict] = []
    for pid in universe:
        st = stock.get(pid)
        qty = float(st["qty_on_hand"]) if st else 0.0
        eur_value = float(st["eur_value"] or 0) if st else 0.0
        unit_cost = (eur_value / qty) if qty > 0 else None

        sold_recent_qty = float(vel[pid]["sold_qty"] or 0) if pid in vel else 0.0
        daily_rate = sold_recent_qty / cfg.velocity_window_days
        band = classify_band(qty, daily_rate, pid in sold_recently, cfg)
        cover = (qty / daily_rate) if daily_rate > 0 else None

        series = cl.series_from(monthly.get(pid, {}), labels)
        annual_units = sum(series)
        xyz = cl.xyz_classify(cl.demand_variability(series, cfg), cfg)
        nonzero = [i for i, u in enumerate(series) if u > 0]
        days_since_first = ((len(series) - 1 - nonzero[0]) * 30 + 15) if nonzero else None
        lifecycle = cl.lifecycle_from_series(series, days_since_first, pid in sold_recently, cfg)

        avg_price = price.get(pid, 0.0)
        margin_pct = None
        if unit_cost is not None and avg_price > 0:
            margin_pct = (avg_price - unit_cost) / avg_price
        revenue_eur = avg_price * annual_units
        return_rate = (rets.get(pid, 0.0) / annual_units) if annual_units > 0 else 0.0
        rows.append({
            "product_id": pid,
            "qty_on_hand": round(qty, 2),
            "eur_value": round(eur_value, 2),
            "unit_cost_eur": round(unit_cost, 4) if unit_cost is not None else None,
            "avg_price_eur": round(avg_price, 4) if avg_price else None,
            "margin_pct": round(margin_pct, 4) if margin_pct is not None else None,
            "annual_units": round(annual_units, 2),
            "revenue_eur": round(revenue_eur, 2),
            "cover_days": round(cover, 1) if cover is not None else None,
            "return_rate": round(return_rate, 4),
            "band": band.value,
            "xyz": xyz.value,
            "lifecycle": lifecycle.value,
            "_band_enum": band,
            "_xyz_enum": xyz,
            "_lifecycle_enum": lifecycle,
            "_margin_pct_raw": margin_pct,
            "_return_rate_raw": return_rate,
        })

    _assign_abc(rows, cfg)
    for row in rows:
        health, breakdown = health_score.score(
            row["_band_enum"],
            row["_lifecycle_enum"],
            row["_margin_pct_raw"],
            row["_xyz_enum"],
            row["_return_rate_raw"],
            cfg,
            abc=row["abc"],
        )
        demand, demand_breakdown = health_score.demand_score(
            row["_band_enum"],
            row["_lifecycle_enum"],
            row["_xyz_enum"],
            row["abc"],
            cfg,
        )
        margin, margin_breakdown = health_score.margin_score(
            row["_margin_pct_raw"],
            row["_return_rate_raw"],
            cfg,
            abc=row["abc"],
        )
        action, reasons = health_score.action_label(
            row["_band_enum"],
            row["_lifecycle_enum"],
            row["abc"],
            row["_margin_pct_raw"],
            row["_return_rate_raw"],
            demand,
            margin,
            cfg,
        )
        row["health"] = health
        row["health_components"] = breakdown
        row["demand_score"] = demand
        row["demand_components"] = demand_breakdown
        row["margin_score"] = margin
        row["margin_components"] = margin_breakdown
        row["action_label"] = action
        row["action_reasons"] = reasons
        for key in ("_band_enum", "_xyz_enum", "_lifecycle_enum", "_margin_pct_raw", "_return_rate_raw"):
            row.pop(key, None)
    return {
        "as_of": as_of,
        "model_version": cfg.model_version,
        "count": len(rows),
        "overview": _overview(rows),
        "rows": rows,
    }


def _assign_abc(rows: list[dict], cfg) -> None:
    total_rev = sum(r["revenue_eur"] for r in rows)
    if total_rev <= 0.0:
        for r in rows:
            r["abc"] = "unknown"
        return
    cum = 0.0
    for r in sorted(rows, key=lambda x: x["revenue_eur"], reverse=True):
        cum += r["revenue_eur"]
        r["abc"] = cl.abc_classify(cum / total_rev, cfg).value


def _overview(rows: list[dict]) -> dict:
    def tally(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            value = r[key] if r[key] is not None else "unknown"
            out[value] = out.get(value, 0) + 1
        return out

    return {
        "total_skus": len(rows),
        "total_eur_value": round(sum(r["eur_value"] for r in rows), 2),
        "total_revenue_eur": round(sum(r["revenue_eur"] for r in rows), 2),
        "by_band": tally("band"),
        "by_lifecycle": tally("lifecycle"),
        "by_action": tally("action_label"),
        "by_abc": tally("abc"),
        "by_xyz": tally("xyz"),
        "avg_health": round(sum(r["health"] for r in rows) / len(rows), 1) if rows else 0.0,
    }
