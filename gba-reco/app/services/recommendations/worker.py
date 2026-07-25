"""Pre-compute worker — warms the cache for all active clients.

Single worker, single key scheme (fixes the prototype's two-divergent-workers problem).
Critically, it warms the *exact* keys the API reads: the default /recommend key
(reco:{model_version}:{cid}:{as_of}:{top_n}:{discovery}) AND the unseeded /recommend/copurchase
key (copurchase:{model_version}:{cid}:{as_of}:{top_n}) — same model_version, same top_n/discovery
defaults pulled from config — so a warmed client is a guaranteed cache hit rather than warming an
off-by-default top_n nobody queries.

Each run also persists the active-client id set in Redis and compares it against the previous
run's snapshot: a >30% id shift means a catalog/client re-mint re-keyed identities («client
identity churn») — the warm rebuild already refreshes the data, but the shift is logged loudly
so it never passes silently.

Idempotent and resumable: writes per-customer as it goes; safe to re-run.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.data import cache
from app.data.db import query
from app.services.recommendations import copurchase, service

log = get_logger("worker")

_CHURN_WARN_RATIO = 0.30


def active_clients(as_of: str, active_days: int) -> list[int]:
    rows = query(
        """
        SELECT DISTINCT ca.ClientID AS cid
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1
              AND o.Created >= DATEADD(day, -:days, :asof)
              AND o.Created < :asof
        ORDER BY ca.ClientID
        """,
        {"days": active_days, "asof": as_of},
    )
    return [int(r["cid"]) for r in rows]


def _check_identity_churn(clients: list[int]) -> None:
    prev = cache.get_active_client_snapshot()
    curr = frozenset(clients)
    if prev:
        churn = len(prev ^ curr) / max(len(prev | curr), 1)
        if churn > _CHURN_WARN_RATIO:
            log.warning(
                "client_identity_churn",
                previous=len(prev), current=len(curr), changed_pct=round(churn * 100, 1),
                note="active-client id set shifted >30% since the last warm — likely a client "
                     "re-mint/re-sync; the warm rebuild refreshes the data, but downstream "
                     "consumers holding old client ids should be checked",
            )
    cache.set_active_client_snapshot(curr)


def run(
    as_of: str | None = None,
    top_n: int | None = None,
    include_discovery: bool | None = None,
    limit: int | None = None,
) -> dict:
    s = get_settings()
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    top_n = top_n if top_n is not None else s.default_top_n
    include_discovery = include_discovery if include_discovery is not None else s.discovery_count > 0
    started = time.time()
    clients = active_clients(as_of, s.active_client_days)
    _check_identity_churn(clients)
    if limit:
        clients = clients[:limit]
    log.info("worker_start", clients=len(clients), as_of=as_of, top_n=top_n,
             discovery=include_discovery, model_version=cache._MODEL_VERSION)

    ok = failed = cop_ok = cop_failed = 0
    for i, cid in enumerate(clients, 1):
        try:
            key = cache.make_key(cid, as_of, top_n, include_discovery)
            res = service.get_recommendations(cid, as_of_date=as_of, top_n=top_n,
                                              include_discovery=include_discovery, use_cache=False)
            cache.set(key, res.model_dump(mode="json"), ttl=s.warm_cache_ttl)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("worker_customer_failed", customer_id=cid, error=str(exc))
        try:
            # the exact key the unseeded /recommend/copurchase endpoint reads (default top_n)
            cop_key = cache.make_copurchase_key(cid, as_of, top_n)
            cop = copurchase.recommend(cid, as_of, top_n=top_n, include_owned=False)
            cache.set(cop_key, cop.model_dump(mode="json"), ttl=s.warm_cache_ttl)
            cop_ok += 1
        except Exception as exc:  # noqa: BLE001
            cop_failed += 1
            log.warning("worker_copurchase_failed", customer_id=cid, error=str(exc))
        if i % 25 == 0:
            log.info("worker_progress", done=i, total=len(clients), ok=ok, failed=failed,
                     copurchase_ok=cop_ok, copurchase_failed=cop_failed)

    stats = {"clients": len(clients), "ok": ok, "failed": failed,
             "copurchase_ok": cop_ok, "copurchase_failed": cop_failed,
             "duration_s": round(time.time() - started, 1), "as_of": as_of,
             "top_n": top_n, "discovery": include_discovery}
    log.info("worker_done", **stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--top-n", type=int, default=None)
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run(as_of=args.as_of, top_n=args.top_n,
        include_discovery=False if args.no_discovery else None, limit=args.limit)


if __name__ == "__main__":
    main()
