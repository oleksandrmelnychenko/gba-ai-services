"""Lens 4 — margin & returns rankings over the portfolio ROW dicts (pure, DB-free).

Every function takes the portfolio rows (see portfolio.build_portfolio) and small params, and
returns a ranked list / summary. margin_pct is a fraction (0.30 = 30%) or None when unit cost is
unknown (no on-hand stock => no purchase cost) — those rows are excluded from margin stats, never
crash. revenue_eur / unit_cost_eur / avg_price_eur / eur_value are already EUR; no conversion here.
"""
from __future__ import annotations

from decimal import Decimal

from app.core import exact_numbers as exact


def _margin_eur(row: dict) -> Decimal:
    """Margin-€ contribution = margin_pct * revenue_eur. Caller guards margin_pct is not None."""
    return exact.decimal_value(
        row["margin_pct"] or 0,
        "margin_pct",
    ) * exact.decimal_value(
        row["revenue_eur"] or 0,
        "revenue_eur",
        non_negative=True,
    )


def _enrich(row: dict) -> dict:
    """Compact view of a row for the margin/returns lenses (derived fields, no DB)."""
    margin_pct = row.get("margin_pct")
    revenue = exact.decimal_value(
        row.get("revenue_eur") or 0,
        "revenue_eur",
        non_negative=True,
    )
    annual = exact.decimal_value(
        row.get("annual_units") or 0,
        "annual_units",
        non_negative=True,
    )
    rate = exact.decimal_value(
        row.get("return_rate") or 0,
        "return_rate",
        non_negative=True,
    )
    returned = exact.decimal_value(
        row.get("returned_units") if row.get("returned_units") is not None else rate * annual,
        "returned_units",
        non_negative=True,
    )
    return {
        "product_id": row["product_id"],
        "margin_pct": margin_pct,
        "margin_eur": (
            exact.money(
                exact.decimal_value(margin_pct, "margin_pct") * revenue,
                "margin_eur",
                non_negative=False,
            )
            if margin_pct is not None
            else None
        ),
        "revenue_eur": exact.money(revenue, "revenue_eur"),
        "unit_cost_eur": row.get("unit_cost_eur"),
        "avg_price_eur": row.get("avg_price_eur"),
        "annual_units": exact.quantity(annual, "annual_units"),
        "return_rate": exact.ratio(rate, "return_rate", non_negative=True),
        "returned_units": exact.quantity(returned, "returned_units"),
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
    rev_known = exact.decimal_sum(
        [row["revenue_eur"] or 0 for row in known],
        "known revenue_eur",
        non_negative=True,
    )
    wsum = sum((_margin_eur(row) for row in known), Decimal("0"))
    weighted_margin = (wsum / rev_known) if rev_known > 0 else None

    neg = [r for r in known if r["margin_pct"] < 0]
    eur_at_negative_margin = exact.decimal_sum(
        [row["revenue_eur"] or 0 for row in neg],
        "negative-margin revenue_eur",
        non_negative=True,
    )

    total_units = exact.decimal_sum(
        [row.get("annual_units") or 0 for row in rows],
        "annual_units",
        non_negative=True,
    )
    total_returned = sum(
        (
            exact.decimal_value(
                (
                    row.get("returned_units")
                    if row.get("returned_units") is not None
                    else exact.decimal_value(
                        row.get("return_rate") or 0,
                        "return_rate",
                        non_negative=True,
                    )
                    * exact.decimal_value(
                        row.get("annual_units") or 0,
                        "annual_units",
                        non_negative=True,
                    )
                ),
                "returned_units",
                non_negative=True,
            )
            for row in rows
        ),
        Decimal("0"),
    )
    overall_return_rate = (total_returned / total_units) if total_units > 0 else 0.0

    return {
        "total_skus": len(rows),
        "skus_with_known_margin": len(known),
        "skus_unknown_margin": len(rows) - len(known),
        "weighted_avg_margin_pct": (
            exact.ratio(weighted_margin, "weighted_avg_margin_pct")
            if weighted_margin is not None
            else None
        ),
        "negative_margin_skus": len(neg),
        "eur_at_negative_margin": exact.money(
            eur_at_negative_margin,
            "eur_at_negative_margin",
        ),
        "revenue_eur_known_margin": exact.money(
            rev_known,
            "revenue_eur_known_margin",
        ),
        "total_annual_units": exact.quantity(total_units, "total_annual_units"),
        "total_returned_units": exact.quantity(total_returned, "total_returned_units"),
        "overall_return_rate": exact.ratio(
            overall_return_rate,
            "overall_return_rate",
            non_negative=True,
        ),
    }
