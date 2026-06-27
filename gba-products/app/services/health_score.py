"""Composite product health-score (0-100) — pure, env-weighted triage blend.

This is not a calibrated probability. Treat it as an assortment triage signal
until scripts/product_health_backtest.py shows stable lift against future
revenue / margin outcomes.
"""
from __future__ import annotations

from app.core.config import Settings
from app.domain.models import AbcClass, InventoryBand, LifecycleStage, XyzClass

_BAND_SCORE: dict[InventoryBand, float] = {
    InventoryBand.HEALTHY: 1.0,
    InventoryBand.ORDER_TO_DEMAND: 0.7,  # fallback only; zero-stock is graded via _ORDER_TO_DEMAND_SCORE
    InventoryBand.UNDERSTOCK: 0.6,
    InventoryBand.OVERSTOCK: 0.4,
    InventoryBand.SLOW: 0.3,
    InventoryBand.DEAD: 0.0,
}

# Zero-stock (order_to_demand) is 64% of the SKU population. A flat 0.7 was a no-data flattering
# default that hid the stockout-vs-dead distinction: a SKU still selling briskly with nothing on hand
# is a healthy order-to-demand line; one whose demand has faded is a weak no-stock SKU.
# Score it on its demand trend (lifecycle) instead — the only signal that separates the two.
_ORDER_TO_DEMAND_SCORE: dict[LifecycleStage, float] = {
    LifecycleStage.GROWING: 0.75,   # rising demand, no holding cost — genuinely healthy to-order
    LifecycleStage.NEW: 0.65,       # young, demand still establishing
    LifecycleStage.MATURE: 0.55,    # steady to-order line — fine, not a star
    LifecycleStage.DECLINING: 0.25,  # fading demand + nothing on hand — heading for dead
    LifecycleStage.DEAD: 0.0,        # no demand in the dead window
}

_LIFECYCLE_SCORE: dict[LifecycleStage, float] = {
    LifecycleStage.GROWING: 1.0,
    LifecycleStage.MATURE: 0.8,
    LifecycleStage.NEW: 0.7,
    LifecycleStage.DECLINING: 0.3,
    LifecycleStage.DEAD: 0.0,
}


def _stock_score(band: InventoryBand, lifecycle: LifecycleStage) -> float:
    """Stock sub-score. Zero-stock (order_to_demand) is graded on demand trend, not a flat default."""
    if band == InventoryBand.ORDER_TO_DEMAND:
        return _ORDER_TO_DEMAND_SCORE.get(lifecycle, 0.5)
    return _BAND_SCORE.get(band, 0.5)


_STABILITY_SCORE: dict[XyzClass, float] = {XyzClass.X: 1.0, XyzClass.Y: 0.6, XyzClass.Z: 0.3}
_ABC_SCORE: dict[AbcClass, float] = {AbcClass.A: 1.0, AbcClass.B: 0.6, AbcClass.C: 0.2}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _abc_score(abc: AbcClass | str | None) -> float:
    if abc is None:
        return 0.5
    try:
        return _ABC_SCORE[AbcClass(abc)]
    except (KeyError, ValueError):
        return 0.5


def demand_score(band: InventoryBand, lifecycle: LifecycleStage, xyz: XyzClass,
                 abc: AbcClass | str | None, cfg: Settings) -> tuple[float, dict]:
    """Demand-oriented score: future sales potential, not margin or stock health."""
    comp = {
        "abc": _abc_score(abc),
        "stability": _STABILITY_SCORE.get(xyz, 0.5),
        "trend": _LIFECYCLE_SCORE.get(lifecycle, 0.5),
        "stock": _stock_score(band, lifecycle),
    }
    weights = {"abc": 0.40, "stability": 0.25, "trend": 0.20, "stock": 0.15}
    value = sum(comp[k] * weights[k] for k in comp) / sum(weights.values())
    return round(100.0 * value, 1), {k: round(comp[k], 3) for k in comp}


def margin_score(margin_pct: float | None, return_rate: float, cfg: Settings,
                 abc: AbcClass | str | None = None) -> tuple[float, dict]:
    """Margin/quality score: profitability and return drag, with a small commercial-scale prior."""
    comp = {
        "margin": 0.5 if margin_pct is None else _clamp01(margin_pct / cfg.margin_target),
        "returns": _clamp01(1.0 - return_rate / cfg.return_rate_cap) if cfg.return_rate_cap > 0 else 1.0,
        "abc": _abc_score(abc),
    }
    weights = {"margin": 0.70, "returns": 0.20, "abc": 0.10}
    value = sum(comp[k] * weights[k] for k in comp) / sum(weights.values())
    return round(100.0 * value, 1), {k: round(comp[k], 3) for k in comp}


def action_label(
    band: InventoryBand,
    lifecycle: LifecycleStage,
    abc: AbcClass | str | None,
    margin_pct: float | None,
    return_rate: float,
    demand: float,
    margin: float,
    cfg: Settings,
) -> tuple[str, list[str]]:
    """Deterministic manager-facing action bucket from the separated scores."""
    abc_value = _abc_score(abc)
    reasons: list[str] = []
    if margin_pct is not None and margin_pct < 0:
        reasons.append("negative_margin")
        return "fix_margin", reasons
    if return_rate >= cfg.returns_high_min_rate:
        reasons.append("high_returns")
        return "quality_review", reasons
    if band == InventoryBand.DEAD:
        reasons.append("dead_stock")
        return "dead_stock_review", reasons
    if band == InventoryBand.OVERSTOCK:
        reasons.append("overstock")
        return "discount_or_redistribute", reasons
    if band == InventoryBand.UNDERSTOCK:
        reasons.append("understock")
        return "reorder_check", reasons
    if band == InventoryBand.SLOW:
        reasons.append("slow_mover")
        return "slow_mover_review", reasons
    if band == InventoryBand.ORDER_TO_DEMAND and demand >= 70:
        reasons.append("strong_to_order_demand")
        if margin_pct is None:
            reasons.append("unknown_margin")
        return "to_order_candidate", reasons
    if demand >= 70 and margin_pct is None and abc_value >= 0.6:
        reasons.extend(["strong_demand", "unknown_margin"])
        return "margin_review", reasons
    if demand >= 70 and margin_pct is not None and margin >= 60 and abc_value >= 0.6:
        reasons.extend(["strong_demand", "healthy_margin"])
        return "keep_push", reasons
    if lifecycle == LifecycleStage.DECLINING and demand < 50:
        reasons.append("declining_demand")
        return "monitor_decline", reasons
    reasons.append("no_immediate_action")
    return "monitor", reasons


def score(band: InventoryBand, lifecycle: LifecycleStage, margin_pct: float | None,
          xyz: XyzClass, return_rate: float, cfg: Settings,
          abc: AbcClass | str | None = None) -> tuple[float, dict]:
    """Return (0–100 health, component breakdown). margin_pct None (unknown cost) -> neutral 0.5."""
    comp = {
        "stock": _stock_score(band, lifecycle),
        "trend": _LIFECYCLE_SCORE.get(lifecycle, 0.5),
        "margin": 0.5 if margin_pct is None else _clamp01(margin_pct / cfg.margin_target),
        "stability": _STABILITY_SCORE.get(xyz, 0.5),
        "returns": _clamp01(1.0 - return_rate / cfg.return_rate_cap) if cfg.return_rate_cap > 0 else 1.0,
        "abc": _abc_score(abc),
    }
    weights = {"stock": cfg.w_stock, "trend": cfg.w_trend, "margin": cfg.w_margin,
               "stability": cfg.w_stability, "returns": cfg.w_returns, "abc": cfg.w_abc}
    wsum = sum(weights.values()) or 1.0
    value = sum(comp[k] * weights[k] for k in comp) / wsum
    return round(100.0 * value, 1), {k: round(comp[k], 3) for k in comp}
