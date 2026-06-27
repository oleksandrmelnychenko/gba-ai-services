"""Lens 4 — margin & returns rankings over the portfolio ROW dicts (pure, DB-free).

Every function takes the portfolio rows (see portfolio.build_portfolio) and small params, and
returns a ranked list / summary. margin_pct is a fraction (0.30 = 30%) or None when unit cost is
unknown (no on-hand stock => no purchase cost) — those rows are excluded from margin stats, never
crash. revenue_eur / unit_cost_eur / avg_price_eur / eur_value are already EUR; no conversion here.
"""
from __future__ import annotations


def _margin_eur(row: dict) -> float:
    """Margin-€ contribution = margin_pct * revenue_eur. Caller guards margin_pct is not None."""
    return (row["margin_pct"] or 0.0) * (row["revenue_eur"] or 0.0)


def _enrich(row: dict) -> dict:
    """Compact view of a row for the margin/returns lenses (derived fields, no DB)."""
    margin_pct = row.get("margin_pct")
    revenue = row.get("revenue_eur") or 0.0
    annual = row.get("annual_units") or 0.0
    rate = row.get("return_rate") or 0.0
    return {
        "product_id": row["product_id"],
        "margin_pct": margin_pct,
        "margin_eur": round(margin_pct * revenue, 2) if margin_pct is not None else None,
        "revenue_eur": round(revenue, 2),
        "unit_cost_eur": row.get("unit_cost_eur"),
        "avg_price_eur": row.get("avg_price_eur"),
        "annual_units": round(annual, 2),
        "return_rate": round(rate, 4),
        "returned_units": round(rate * annual, 2),
        "band": row.get("band"),
        "lifecycle": row.get("lifecycle"),
        "abc": row.get("abc"),
        "health": row.get("health"),
    }


def margin_leaders(rows: list[dict], limit: int = 20) -> list[dict]:
    """Highest margin-€ contribution (margin_pct * revenue_eur). Rows with unknown cost excluded."""
    known = [r for r in rows if r.get("margin_pct") is not None]
    ranked = sorted(known, key=_margin_eur, reverse=True)
    return [_enrich(r) for r in ranked[:limit]]


def margin_laggards(rows: list[dict], limit: int = 20) -> list[dict]:
    """Lowest margin% (incl. negative) among rows where cost is known. Rows with unknown cost excluded."""
    known = [r for r in rows if r.get("margin_pct") is not None]
    ranked = sorted(known, key=lambda r: r["margin_pct"])
    return [_enrich(r) for r in ranked[:limit]]


def negative_margin(rows: list[dict]) -> list[dict]:
    """Rows sold below cost (margin_pct < 0) — a real alert. Most-negative first."""
    flagged = [r for r in rows if r.get("margin_pct") is not None and r["margin_pct"] < 0]
    ranked = sorted(flagged, key=lambda r: r["margin_pct"])
    return [_enrich(r) for r in ranked]


def high_returns(rows: list[dict], min_rate: float = 0.05, limit: int = 20) -> list[dict]:
    """Rows whose return_rate >= min_rate, ranked desc. Only products with sales (annual_units>0)."""
    flagged = [r for r in rows
               if (r.get("annual_units") or 0.0) > 0 and (r.get("return_rate") or 0.0) >= min_rate]
    ranked = sorted(flagged, key=lambda r: r.get("return_rate") or 0.0, reverse=True)
    return [_enrich(r) for r in ranked[:limit]]


def margin_returns_summary(rows: list[dict]) -> dict:
    """Portfolio totals: revenue-weighted avg margin% (where known), €-at-negative-margin,
    overall return rate (Σ returned units / Σ annual_units), and the relevant counts."""
    known = [r for r in rows if r.get("margin_pct") is not None]
    rev_known = sum((r["revenue_eur"] or 0.0) for r in known)
    wsum = sum(_margin_eur(r) for r in known)
    weighted_margin = (wsum / rev_known) if rev_known > 0 else None

    neg = [r for r in known if r["margin_pct"] < 0]
    eur_at_negative_margin = sum((r["revenue_eur"] or 0.0) for r in neg)

    total_units = sum((r.get("annual_units") or 0.0) for r in rows)
    total_returned = sum((r.get("return_rate") or 0.0) * (r.get("annual_units") or 0.0) for r in rows)
    overall_return_rate = (total_returned / total_units) if total_units > 0 else 0.0

    return {
        "total_skus": len(rows),
        "skus_with_known_margin": len(known),
        "skus_unknown_margin": len(rows) - len(known),
        "weighted_avg_margin_pct": round(weighted_margin, 4) if weighted_margin is not None else None,
        "negative_margin_skus": len(neg),
        "eur_at_negative_margin": round(eur_at_negative_margin, 2),
        "revenue_eur_known_margin": round(rev_known, 2),
        "total_annual_units": round(total_units, 2),
        "total_returned_units": round(total_returned, 2),
        "overall_return_rate": round(overall_return_rate, 4),
    }
