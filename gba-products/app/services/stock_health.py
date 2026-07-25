"""Inventory-health: classify each on-hand SKU into a days-of-cover band + portfolio snapshot.

Phase-1 seed of Lens 2 — bands only (no composite health-score yet). Pure band logic is unit-tested;
the snapshot joins the canonical stock query with sales velocity over the live DB.
"""
from __future__ import annotations

from decimal import Decimal

from app.core import exact_numbers as exact
from app.core.config import Settings, get_settings
from app.data import signals_repository as sig
from app.domain.models import InventoryBand
from app.services import history_policy


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
    windows = history_policy.stock_windows(as_of, cfg)
    velocity_days = windows["velocity"].effective_days
    stock = sig.on_hand_stock()
    velocity: dict[int, Decimal] = {}
    for row in sig.sales_velocity(as_of, cfg.velocity_window_days):
        pid = exact.positive_int(row.get("product_id"), "sales_velocity.product_id")
        if pid in velocity:
            raise ValueError(f"sales_velocity returned duplicate product_id {pid}")
        velocity[pid] = exact.decimal_value(
            row.get("sold_qty") or 0,
            "sales_velocity.sold_qty",
            non_negative=True,
        )
    sold_recently = {
        exact.positive_int(product_id, "sold_product_ids.product_id")
        for product_id in sig.sold_product_ids(as_of, cfg.dead_window_days)
    }

    bands: dict[str, dict] = {
        band.value: {"count": 0, "eur_value": Decimal("0"), "qty": Decimal("0")}
        for band in InventoryBand
    }
    rows: list[dict] = []
    product_ids: set[int] = set()
    unvalued_skus = 0
    for r in stock:
        pid = exact.positive_int(r.get("product_id"), "stock.product_id")
        if pid in product_ids:
            raise ValueError(f"on_hand_stock returned duplicate product_id {pid}")
        product_ids.add(pid)
        qty = exact.decimal_value(
            r.get("qty_on_hand") or 0,
            "stock.qty_on_hand",
            non_negative=True,
        )
        eur = exact.decimal_value(
            r.get("eur_value") or 0,
            "stock.eur_value",
            non_negative=True,
        )
        valuation_available = r.get("unit_cost_eur") is not None
        if valuation_available != (r.get("eur_value") is not None):
            raise ValueError(f"stock valuation fields disagree for product {pid}")
        if not valuation_available:
            unvalued_skus += 1
        sold_qty = velocity.get(pid, Decimal("0"))
        if velocity_days == 0 and sold_qty > 0:
            raise ValueError("sales velocity has demand outside the effective history window")
        daily_rate = sold_qty / Decimal(velocity_days) if velocity_days > 0 else Decimal("0")
        band = classify_band(float(qty), float(daily_rate), pid in sold_recently, cfg)
        cover = (qty / daily_rate) if daily_rate > 0 else None
        row_qty = exact.quantity(qty, "qty_on_hand")
        row_eur = exact.money(eur, "eur_value")
        bands[band.value]["count"] += 1
        bands[band.value]["eur_value"] += exact.decimal_value(row_eur, "row eur_value")
        bands[band.value]["qty"] += exact.decimal_value(row_qty, "row qty_on_hand")
        rows.append(
            {
                "product_id": pid,
                "qty_on_hand": row_qty,
                "eur_value": row_eur,
                "valuation_available": valuation_available,
                "cover_days": exact.cover_days(cover) if cover is not None else None,
                "band": band.value,
            }
        )

    for values in bands.values():
        values["eur_value"] = exact.money(values["eur_value"], "band eur_value")
        values["qty"] = exact.quantity(values["qty"], "band qty")
    rows.sort(key=lambda x: x["eur_value"], reverse=True)
    total_eur = exact.decimal_sum(
        [row["eur_value"] for row in rows],
        "stock row eur_value",
        non_negative=True,
    )
    total_qty = exact.decimal_sum(
        [row["qty_on_hand"] for row in rows],
        "stock row qty_on_hand",
        non_negative=True,
    )
    result = {
        "as_of": as_of,
        "total_skus": len(stock),
        "total_qty": exact.quantity(total_qty, "total_qty"),
        "total_eur_value": exact.money(total_eur, "total_eur_value"),
        "valued_skus": len(stock) - unvalued_skus,
        "unvalued_skus": unvalued_skus,
        "bands": bands,
        "model_version": cfg.model_version,
        **history_policy.stock_metadata(as_of, cfg),
        "rows": rows,
    }
    _validate_snapshot(result)
    return result


def _validate_snapshot(snapshot: dict) -> None:
    rows = snapshot["rows"]
    product_ids = [
        exact.positive_int(row.get("product_id"), "stock snapshot product_id")
        for row in rows
    ]
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("stock snapshot contains duplicate product_id")
    if snapshot["total_skus"] != len(rows):
        raise ValueError("stock total_skus does not match rows")
    if sum(values["count"] for values in snapshot["bands"].values()) != len(rows):
        raise ValueError("stock band counts do not match rows")
    if snapshot["valued_skus"] + snapshot["unvalued_skus"] != len(rows):
        raise ValueError("stock valuation counts do not match rows")

    band_value = exact.money(
        exact.decimal_sum(
            [values["eur_value"] for values in snapshot["bands"].values()],
            "band eur_value",
            non_negative=True,
        ),
        "band total_eur_value",
    )
    band_qty = exact.quantity(
        exact.decimal_sum(
            [values["qty"] for values in snapshot["bands"].values()],
            "band qty",
            non_negative=True,
        ),
        "band total_qty",
    )
    if band_value != snapshot["total_eur_value"]:
        raise ValueError("stock band EUR total does not match rows")
    if band_qty != snapshot["total_qty"]:
        raise ValueError("stock band quantity total does not match rows")
