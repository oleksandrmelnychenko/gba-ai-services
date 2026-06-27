"""Inventory-health: classify each on-hand SKU into a days-of-cover band + portfolio snapshot.

Phase-1 seed of Lens 2 — bands only (no composite health-score yet). Pure band logic is unit-tested;
the snapshot joins the canonical stock query with sales velocity over the live DB.
"""
from __future__ import annotations

from app.core.config import Settings, get_settings
from app.data import signals_repository as sig
from app.domain.models import InventoryBand


def classify_band(qty_on_hand: float, daily_rate: float, sold_in_dead_window: bool,
                  cfg: Settings) -> InventoryBand:
    """Days-of-cover band for one SKU. daily_rate = recent sold qty / velocity window (units/day).
    GBA is order-to-demand: nothing on hand is normal (order_to_demand), not a stockout."""
    if qty_on_hand <= 0:
        return InventoryBand.ORDER_TO_DEMAND
    if not sold_in_dead_window:
        return InventoryBand.DEAD
    annual_units = daily_rate * 365.0
    if annual_units <= cfg.slow_max_annual_units:
        return InventoryBand.SLOW
    cover_days = qty_on_hand / daily_rate
    if cover_days > cfg.cover_overstock_days:
        return InventoryBand.OVERSTOCK
    if cover_days < cfg.cover_understock_days:
        return InventoryBand.UNDERSTOCK
    return InventoryBand.HEALTHY


def snapshot(as_of: str) -> dict:
    """Portfolio inventory-health snapshot over all on-hand sellable stock."""
    cfg = get_settings()
    stock = sig.on_hand_stock()
    velocity = {int(r["product_id"]): float(r["sold_qty"] or 0)
                for r in sig.sales_velocity(as_of, cfg.velocity_window_days)}
    sold_recently = sig.sold_product_ids(as_of, cfg.dead_window_days)

    bands: dict[str, dict] = {b.value: {"count": 0, "eur_value": 0.0, "qty": 0.0} for b in InventoryBand}
    rows: list[dict] = []
    total_eur = total_qty = 0.0
    for r in stock:
        pid = int(r["product_id"])
        qty = float(r["qty_on_hand"] or 0)
        eur = float(r["eur_value"] or 0)
        daily_rate = velocity.get(pid, 0.0) / cfg.velocity_window_days
        band = classify_band(qty, daily_rate, pid in sold_recently, cfg)
        cover = (qty / daily_rate) if daily_rate > 0 else None
        bands[band.value]["count"] += 1
        bands[band.value]["eur_value"] += eur
        bands[band.value]["qty"] += qty
        total_eur += eur
        total_qty += qty
        rows.append({"product_id": pid, "qty_on_hand": qty, "eur_value": round(eur, 2),
                     "cover_days": round(cover, 1) if cover is not None else None,
                     "band": band.value})

    for b in bands.values():
        b["eur_value"] = round(b["eur_value"], 2)
        b["qty"] = round(b["qty"], 2)
    rows.sort(key=lambda x: x["eur_value"], reverse=True)
    return {
        "as_of": as_of,
        "total_skus": len(stock),
        "total_qty": round(total_qty, 2),
        "total_eur_value": round(total_eur, 2),
        "bands": bands,
        "model_version": cfg.model_version,
        "rows": rows,
    }
