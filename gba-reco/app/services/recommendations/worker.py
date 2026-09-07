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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from app.core.config import get_settings
from app.core.history import require_supported_as_of, source_history_start_iso
from app.core.logging import get_logger
from app.data import cache
from app.data import sales_repository as repo
from app.data.db import query
from app.services.recommendations import copurchase, service

log = get_logger("worker")

_CHURN_WARN_RATIO = 0.30


def active_clients(as_of: str, active_days: int) -> list[int]:
    require_supported_as_of(as_of)
    rows = query(
        """
        SELECT DISTINCT ca.ClientID AS cid
        FROM dbo.ClientAgreement ca
        JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0 AND c.NetUID IS NOT NULL
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1
              AND o.Created >= DATEADD(day, -:days, :asof)
              AND o.Created >= :history_start
              AND o.Created < :asof
        ORDER BY ca.ClientID
        """,
        {
            "days": active_days,
            "asof": as_of,
            "history_start": source_history_start_iso(),
        },
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
    workers: int = 1,
    refresh_token: str | None = None,
) -> dict:
    s = get_settings()
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    top_n = top_n if top_n is not None else s.default_top_n
    include_discovery = include_discovery if include_discovery is not None else s.discovery_count > 0
    started = time.time()
    if not cache.health():
        raise RuntimeError("cache unavailable; cannot persist the daily recommendation refresh")
    clients = active_clients(as_of, s.active_client_days)
    _check_identity_churn(clients)
    if limit:
        clients = clients[:limit]
    log.info("worker_start", clients=len(clients), as_of=as_of, top_n=top_n,
             discovery=include_discovery, model_version=cache._MODEL_VERSION)

    # Keep the legacy client scope warm for NBA while sales read a separate key
    # for each active agreement. Never select one arbitrary agreement for a client.
    targets = [
        (cid, agreement_uid)
        for cid in clients
        for agreement_uid in [None, *repo.active_agreements(cid)]
    ]
    def warm(target: tuple[int, str | None]) -> tuple[int, int, int, int]:
        cid, agreement_uid = target
        scope = {"client_agreement_net_id": agreement_uid} if agreement_uid else {}
        outcome = [0, 0, 0, 0]
        for cop in (False, True):
            try:
                key = (cache.make_copurchase_key(cid, as_of, top_n, **scope) if cop
                       else cache.make_key(cid, as_of, top_n, include_discovery, **scope))
                previous = cache.get(key) if refresh_token else None
                if not (previous and previous.get("_fleet_refresh") == refresh_token):
                    result = (copurchase.recommend(cid, as_of, top_n=top_n, include_owned=False, **scope)
                              if cop else service.get_recommendations(
                                  cid, as_of_date=as_of, top_n=top_n,
                                  include_discovery=include_discovery, use_cache=False, **scope))
                    payload = result.model_dump(mode="json")
                    if refresh_token:
                        payload["_fleet_refresh"] = refresh_token
                    if not cache.set(key, payload, ttl=s.warm_cache_ttl):
                        raise RuntimeError("recommendation cache write failed")
                outcome[2 if cop else 0] = 1
            except Exception as exc:  # noqa: BLE001
                outcome[3 if cop else 1] = 1
                log.warning("worker_context_failed", customer_id=cid, copurchase=cop,
                            client_agreement_net_id=agreement_uid, error=str(exc))
        return tuple(outcome)

    if not 1 <= workers <= 4:
        raise ValueError("workers must be within 1..4")
    ok = failed = cop_ok = cop_failed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for i, counts in enumerate(executor.map(warm, targets), 1):
            a, b, c, d = counts
            ok += a
            failed += b
            cop_ok += c
            cop_failed += d
            if i % 25 == 0:
                log.info("worker_progress", done=i, total=len(targets), ok=ok, failed=failed,
                         copurchase_ok=cop_ok, copurchase_failed=cop_failed)

    stats = {"clients": len(clients), "contexts": len(targets),
             "agreements": len(targets) - len(clients), "ok": ok, "failed": failed,
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
