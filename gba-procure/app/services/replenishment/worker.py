"""Pre-compute worker — builds purchase plans for all active producers into the cache.

Single worker, single key scheme (procure:v1:producer:{id}:{as_of}). Idempotent/resumable.
Warm candidates = supply-active producers ∪ active Manufacturer-role clients (the console's
/plan/producer picker list), so every openable producer plan is pre-warmed.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.core.logging import get_logger
from app.data import cache, masters
from app.domain.models import CartReplenishmentPlan
from app.services.replenishment import policy

log = get_logger("procure_worker")


class ProcurementBusinessReadinessError(RuntimeError):
    """Canonical procurement data cannot currently produce a usable plan."""


def active_producers(as_of: str, active_days: int = 365) -> list[int]:
    """Backward-compatible wrapper for the canonical producer candidate query.

    Keep worker-side producer selection on the repository path so it inherits the same
    DateFrom-based source semantics and synthetic-product exclusion as /plan/cart.
    """
    from app.data import supply_repository as repo

    return repo.all_producers(as_of, active_days)


def warm_producer_candidates(as_of: str) -> list[int]:
    """Every producer /plan/producer can be opened for: supply-active ∪ active manufacturers."""
    from app.core.config import get_settings
    from app.data import supply_repository as repo

    active = repo.all_producers(as_of, get_settings().history_days)
    return sorted(set(active) | set(repo.manufacturer_producers()))


# The canonical cart is deliberately untruncated: every unique needed product must reach
# the console. Explicit limits remain available for secondary/read-only consumers.
CART_LIMIT: int | None = None
# Default charts top_n; MUST match /plan/charts' default so the warm key is the one served.
CHARTS_TOP_N = 15
SOURCE_READINESS_TTL_S = 60
_CENT = Decimal("0.01")


def cart_cache_key(as_of: str, limit: int | None = CART_LIMIT) -> str:
    return cache.make_key("cart", "all" if limit is None else limit, as_of)


def canonical_cart_payload_is_ready(
    payload: dict | None,
    *,
    source_fingerprint: str | None = None,
) -> bool:
    """Fail-closed reconciliation for the canonical console payload.

    Besides shape and source epoch, prove that the payload is complete, has one line per
    product, preserves nested IDs and reconciles quantity/cost aggregates to the cent.
    An evaluated zero-item plan remains a valid business result.
    """
    if not isinstance(payload, dict):
        return False
    item_count = payload.get("item_count")
    items = payload.get("items")
    structurally_valid = (
        isinstance(item_count, int)
        and not isinstance(item_count, bool)
        and item_count >= 0
        and isinstance(items, list)
        and len(items) == item_count
    )
    if not structurally_valid or payload.get("total_item_count") != item_count:
        return False
    if payload.get("is_truncated") is not False:
        return False
    if (
        not isinstance(payload.get("duplicate_supplier_options_removed"), int)
        or isinstance(payload.get("duplicate_supplier_options_removed"), bool)
        or payload["duplicate_supplier_options_removed"] < 0
    ):
        return False

    seen_products: set[int] = set()
    total_qty = Decimal()
    priced_cost = Decimal()
    unpriced = 0
    try:
        for item in items:
            if not isinstance(item, dict):
                return False
            product_id = item.get("product_id")
            producer_id = item.get("producer_id")
            if (
                not isinstance(product_id, int)
                or isinstance(product_id, bool)
                or product_id <= 0
                or product_id in seen_products
                or not isinstance(producer_id, int)
                or isinstance(producer_id, bool)
                or producer_id <= 0
            ):
                return False
            seen_products.add(product_id)
            forecast = item.get("forecast")
            inventory = item.get("inventory")
            if (
                not isinstance(forecast, dict)
                or forecast.get("product_id") != product_id
                or not isinstance(inventory, dict)
                or inventory.get("product_id") != product_id
            ):
                return False

            qty = Decimal(str(item.get("suggested_qty")))
            if not qty.is_finite() or qty < 0:
                return False
            total_qty += qty

            unit_cost = item.get("unit_cost_eur")
            line_cost = item.get("line_cost_eur")
            if unit_cost is None or line_cost is None:
                unpriced += 1
                continue
            unit = Decimal(str(unit_cost))
            line = Decimal(str(line_cost))
            if not unit.is_finite() or unit <= 0 or not line.is_finite() or line < 0:
                return False
            expected_line = (unit * qty).quantize(_CENT, rounding=ROUND_HALF_UP)
            if line.quantize(_CENT, rounding=ROUND_HALF_UP) != expected_line:
                return False
            priced_cost += line

        expected_qty = total_qty.quantize(_CENT, rounding=ROUND_HALF_UP)
        expected_priced = priced_cost.quantize(_CENT, rounding=ROUND_HALF_UP)
        if Decimal(str(payload.get("total_suggested_qty"))).quantize(
            _CENT, rounding=ROUND_HALF_UP
        ) != expected_qty:
            return False
        if Decimal(str(payload.get("priced_cost_eur"))).quantize(
            _CENT, rounding=ROUND_HALF_UP
        ) != expected_priced:
            return False
        if payload.get("unpriced_item_count") != unpriced or unpriced:
            return False
        if Decimal(str(payload.get("total_cost_eur"))).quantize(
            _CENT, rounding=ROUND_HALF_UP
        ) != expected_priced:
            return False
    except (InvalidOperation, TypeError, ValueError):
        return False

    return source_fingerprint is None or payload.get("_source_fingerprint") == source_fingerprint


def get_source_readiness(as_of: str, *, force: bool = False) -> dict:
    """Return a short-lived snapshot of the factual inputs required by the policy."""
    from app.core.config import get_settings
    from app.data import supply_repository as repo

    key = cache.make_key("readiness", "source", as_of)
    if not force:
        cached = cache.get(key)
        if (
            isinstance(cached, dict)
            and cached.get("as_of") == as_of
            and isinstance(cached.get("ready"), bool)
        ):
            return cached
    snapshot = repo.procurement_source_readiness(as_of, get_settings().history_days)
    settings = get_settings()
    mongo_required = settings.use_masters or settings.use_feedback
    masters_connected = not mongo_required or masters.ping()
    snapshot["masters_connected"] = masters_connected
    if snapshot.get("ready") is True and not masters_connected:
        snapshot["ready"] = False
        snapshot["reason"] = "masters_store_unavailable"
    cache.set(key, snapshot, ttl=SOURCE_READINESS_TTL_S)
    return snapshot


def require_source_readiness(as_of: str, *, force: bool = False) -> dict:
    """Fail closed on incomplete source state, without treating a zero output as broken."""
    snapshot = get_source_readiness(as_of, force=force)
    if snapshot.get("ready") is True:
        return snapshot
    reason = str(snapshot.get("reason") or "procurement_source_not_ready")
    _mark_canonical_cart_not_ready(
        as_of,
        reason=reason,
        candidate_count=int(snapshot.get("producer_count") or 0),
        item_count=0,
    )
    raise ProcurementBusinessReadinessError(
        f"{reason}:producers={snapshot.get('producer_count', 0)}:"
        f"products={snapshot.get('product_count', 0)}"
    )


def _mark_canonical_cart_not_ready(
    as_of: str,
    *,
    reason: str,
    candidate_count: int,
    item_count: int,
    cart_limit: int | None = CART_LIMIT,
) -> None:
    cache.delete(cart_cache_key(as_of, cart_limit))
    cache.delete(cache.make_key("charts", f"all:{CHARTS_TOP_N}", as_of))
    cache.mark_cart_not_ready(
        as_of,
        reason,
        candidate_count=candidate_count,
        item_count=item_count,
    )
    log.error(
        "procure_business_not_ready",
        as_of=as_of,
        reason=reason,
        candidates=candidate_count,
        items=item_count,
    )


def cache_canonical_cart_plan(
    plan: CartReplenishmentPlan,
    as_of: str,
    *,
    cart_limit: int | None = CART_LIMIT,
    source_snapshot: dict | None = None,
) -> int:
    """Persist an evaluated canonical plan after its factual inputs pass readiness."""
    source_snapshot = source_snapshot or require_source_readiness(as_of)
    if source_snapshot.get("ready") is not True:
        raise ProcurementBusinessReadinessError(
            str(source_snapshot.get("reason") or "procurement_source_not_ready")
        )
    candidate_count = int(source_snapshot.get("producer_count") or 0)

    key = cart_cache_key(as_of, cart_limit)
    payload = plan.model_dump(mode="json")
    payload["_source_fingerprint"] = source_snapshot.get("source_fingerprint")
    if not canonical_cart_payload_is_ready(
        payload,
        source_fingerprint=source_snapshot.get("source_fingerprint"),
    ):
        _mark_canonical_cart_not_ready(
            as_of,
            reason="canonical_cart_reconciliation_failed",
            candidate_count=candidate_count,
            item_count=plan.item_count,
            cart_limit=cart_limit,
        )
        raise ProcurementBusinessReadinessError("canonical_cart_reconciliation_failed")
    cache.set(key, payload, ttl=691200)  # 8 days
    cache.clear_cart_not_ready(as_of)
    return candidate_count


def warm_cart(as_of: str | None = None, cart_limit: int | None = CART_LIMIT) -> dict:
    """Pre-compute the cart plan into the EXACT key /plan/cart reads on a cache miss.

    /plan/cart's canonical request has no truncation and keys as ``cart:all`` for today,
    so the worker writes that same key and the API serves warm (cache hit, <1s).
    """
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    started = time.time()
    log.info("procure_cart_warm_start", as_of=as_of, limit=cart_limit)
    source_snapshot = require_source_readiness(as_of, force=True)
    plan = policy.build_cart_plan(
        as_of,
        only_needed=True,
        limit=cart_limit,
        source_fingerprint=source_snapshot.get("source_fingerprint"),
    )
    key = cart_cache_key(as_of, cart_limit)
    candidate_count = cache_canonical_cart_plan(
        plan,
        as_of,
        cart_limit=cart_limit,
        source_snapshot=source_snapshot,
    )
    stats = {
        "key": key,
        "items": plan.item_count,
        "candidates": candidate_count,
        "business_ready": True,
        "duration_s": round(time.time() - started, 1),
        "as_of": as_of,
    }
    log.info("procure_cart_warm_done", **stats)
    return stats


def warm_charts(as_of: str | None = None, top_n: int = CHARTS_TOP_N) -> dict:
    """Pre-compute the cart-wide charts into the EXACT key /plan/charts reads on a cache miss.

    /plan/charts keys on make_key('charts', 'all:{top_n}', as_of) for producer_id=None,
    so the worker writes that same key and the API serves warm instead of rebuilding live.
    """
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    started = time.time()
    log.info("procure_charts_warm_start", as_of=as_of, top_n=top_n)
    source_snapshot = require_source_readiness(as_of, force=True)
    cart_key = cart_cache_key(as_of)
    if (
        cache.get_cart_not_ready(as_of) is not None
        or not canonical_cart_payload_is_ready(
            cache.get(cart_key),
            source_fingerprint=source_snapshot.get("source_fingerprint"),
        )
    ):
        cache.delete(cache.make_key("charts", f"all:{top_n}", as_of))
        raise ProcurementBusinessReadinessError(
            f"canonical_cart_not_ready:as_of={as_of}"
        )
    charts = policy.build_charts(
        None,
        as_of,
        top_n=top_n,
        source_fingerprint=source_snapshot.get("source_fingerprint"),
    )
    key = cache.make_key("charts", f"all:{top_n}", as_of)
    payload = charts.model_dump(mode="json")
    payload["_source_fingerprint"] = source_snapshot.get("source_fingerprint")
    cache.set(key, payload, ttl=691200)  # 8 days
    stats = {"key": key, "top_items": len(charts.top_items),
             "duration_s": round(time.time() - started, 1), "as_of": as_of}
    log.info("procure_charts_warm_done", **stats)
    return stats


def run(as_of: str | None = None, limit: int | None = None, warm_cart_key: bool = True,
        skip_existing: bool = False) -> dict:
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    started = time.time()
    source_snapshot = require_source_readiness(as_of, force=True)
    producers = warm_producer_candidates(as_of)
    if limit:
        producers = producers[:limit]
    log.info("procure_worker_start", producers=len(producers), as_of=as_of,
             skip_existing=skip_existing)

    ok = failed = skipped = nonempty_producers = total_items = 0
    for i, pid in enumerate(producers, 1):
        key = cache.make_key("producer", pid, as_of)
        if skip_existing:
            cached = cache.get(key)
            if (
                cached is not None
                and cached.get("_source_fingerprint")
                == source_snapshot.get("source_fingerprint")
            ):
                skipped += 1
                cached_item_count = cached.get("item_count")
                if isinstance(cached_item_count, int) and cached_item_count > 0:
                    nonempty_producers += 1
                    total_items += cached_item_count
                continue
        try:
            plan = policy.build_plan(pid, as_of, only_needed=True)
            payload = plan.model_dump(mode="json")
            payload["_source_fingerprint"] = source_snapshot.get("source_fingerprint")
            cache.set(key, payload, ttl=691200)  # 8 days
            ok += 1
            if plan.item_count > 0:
                nonempty_producers += 1
                total_items += plan.item_count
            log.info("producer_planned", producer_id=pid, items=plan.item_count)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("producer_failed", producer_id=pid, error=str(exc))
        if i % 10 == 0:
            log.info("procure_worker_progress", done=i, total=len(producers),
                     ok=ok, failed=failed, skipped=skipped)

    cart = charts = None
    if warm_cart_key:
        try:
            cart = warm_cart(as_of)
        except ProcurementBusinessReadinessError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("procure_cart_warm_failed", error=str(exc))
        try:
            charts = warm_charts(as_of)
        except ProcurementBusinessReadinessError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("procure_charts_warm_failed", error=str(exc))

    stats = {
        "producers": len(producers),
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "nonempty_producers": nonempty_producers,
        "producer_items": total_items,
        "business_ready": source_snapshot.get("ready") is True,
        "source_reason": source_snapshot.get("reason"),
        "duration_s": round(time.time() - started, 1),
        "as_of": as_of,
        "cart_items": cart.get("items") if cart else None,
        "charts_top_items": charts.get("top_items") if charts else None,
    }
    log.info("procure_worker_done", **stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--cart-only", action="store_true", help="only warm the cart key")
    ap.add_argument("--no-cart", action="store_true", help="skip cart warming")
    ap.add_argument("--skip-existing", action="store_true",
                    help="resume: skip producers whose key is already warm")
    args = ap.parse_args()
    if args.cart_only:
        warm_cart(as_of=args.as_of)
        warm_charts(as_of=args.as_of)
    else:
        run(as_of=args.as_of, limit=args.limit, warm_cart_key=not args.no_cart,
            skip_existing=args.skip_existing)


if __name__ == "__main__":
    main()
