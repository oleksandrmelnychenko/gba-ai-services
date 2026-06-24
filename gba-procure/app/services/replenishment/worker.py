"""Pre-compute worker — builds purchase plans for all active producers into the cache.

Single worker, single key scheme (procure:v1:producer:{id}:{as_of}). Idempotent/resumable.
Active producer = has a non-deleted supply order within history window.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

from app.core.logging import get_logger
from app.data import cache
from app.data.db import query
from app.services.replenishment import policy

log = get_logger("procure_worker")


def active_producers(as_of: str, active_days: int = 365) -> list[int]:
    rows = query(
        """
        SELECT DISTINCT so.ClientID AS pid
        FROM dbo.SupplyOrder so
        WHERE so.Deleted = 0
              AND so.ClientID IS NOT NULL
              AND so.Created >= DATEADD(day, -:days, :asof)
              AND so.Created < :asof
        ORDER BY so.ClientID
        """,
        {"days": active_days, "asof": as_of},
    )
    return [int(r["pid"]) for r in rows]


# Default cart-plan limit; MUST match /plan/cart's default so the warm key is the one served.
CART_LIMIT = 200


def warm_cart(as_of: str | None = None, cart_limit: int = CART_LIMIT) -> dict:
    """Pre-compute the cart plan into the EXACT key /plan/cart reads on a cache miss.

    /plan/cart keys on make_key('cart', limit, as_of) with default limit=200, as_of=today,
    so the worker writes that same key and the API serves warm (cache hit, <1s).
    """
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    started = time.time()
    log.info("procure_cart_warm_start", as_of=as_of, limit=cart_limit)
    plan = policy.build_cart_plan(as_of, only_needed=True, limit=cart_limit)
    key = cache.make_key("cart", cart_limit, as_of)
    cache.set(key, plan.model_dump(mode="json"), ttl=691200)  # 8 days
    stats = {"key": key, "items": plan.item_count,
             "duration_s": round(time.time() - started, 1), "as_of": as_of}
    log.info("procure_cart_warm_done", **stats)
    return stats


def run(as_of: str | None = None, limit: int | None = None, warm_cart_key: bool = True) -> dict:
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    started = time.time()
    from app.core.config import get_settings
    from app.data import supply_repository as repo
    producers = repo.all_producers(as_of, get_settings().history_days)
    if limit:
        producers = producers[:limit]
    log.info("procure_worker_start", producers=len(producers), as_of=as_of)

    ok = failed = 0
    for i, pid in enumerate(producers, 1):
        try:
            plan = policy.build_plan(pid, as_of, only_needed=True)
            key = cache.make_key("producer", pid, as_of)
            cache.set(key, plan.model_dump(mode="json"), ttl=691200)  # 8 days
            ok += 1
            log.info("producer_planned", producer_id=pid, items=plan.item_count)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("producer_failed", producer_id=pid, error=str(exc))
        if i % 10 == 0:
            log.info("procure_worker_progress", done=i, total=len(producers), ok=ok, failed=failed)

    cart = None
    if warm_cart_key:
        try:
            cart = warm_cart(as_of)
        except Exception as exc:  # noqa: BLE001
            log.warning("procure_cart_warm_failed", error=str(exc))

    stats = {"producers": len(producers), "ok": ok, "failed": failed,
             "duration_s": round(time.time() - started, 1), "as_of": as_of,
             "cart_items": cart.get("items") if cart else None}
    log.info("procure_worker_done", **stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--cart-only", action="store_true", help="only warm the cart key")
    ap.add_argument("--no-cart", action="store_true", help="skip cart warming")
    args = ap.parse_args()
    if args.cart_only:
        warm_cart(as_of=args.as_of)
    else:
        run(as_of=args.as_of, limit=args.limit, warm_cart_key=not args.no_cart)


if __name__ == "__main__":
    main()
