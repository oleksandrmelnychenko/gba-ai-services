"""Regeneration worker — nightly job that keeps every manager's inbox fresh.

For each manager: run all generators (idempotent upsert via task_key). Then run lifecycle
sweeps: wake snoozed tasks whose time has come, and flag SLA-breached overdue tasks. Safe to
re-run any time (idempotent).
"""
from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from app.clients import reco_client
from app.core.config import get_settings
from app.core.logging import get_logger
from app.data import signals_repository as sig
from app.services import lifecycle, orchestrator

log = get_logger("worker")


def push_reco_feedback(window_days: int | None = None) -> dict:
    """Push NBA's negative cross_sell signals (dismissed / sold=False) to reco so it stops
    recommending those products to those clients. Best-effort per client; never raises."""
    window_days = window_days if window_days is not None else get_settings().feedback_window_days
    negs = lifecycle.cross_sell_negatives(window_days)
    clients = sent = products = 0
    for cid, pids in negs.items():
        clients += 1
        if reco_client.send_feedback(cid, sorted(pids)):
            sent += 1
            products += len(pids)
    log.info("reco_feedback_pushed", clients=clients, sent=sent, products=products)
    return {"clients": clients, "sent": sent, "products": products}


def run(as_of: str | None = None, limit: int | None = None) -> dict:
    as_of = as_of or datetime.now(UTC).strftime("%Y-%m-%d")
    started = time.time()
    managers = sig.all_managers()
    if limit:
        managers = managers[:limit]
    log.info("regen_start", managers=len(managers), as_of=as_of)

    try:
        fb = push_reco_feedback()
    except Exception as exc:  # noqa: BLE001
        log.warning("reco_feedback_skipped", error=str(exc))
        fb = {"clients": 0, "sent": 0, "products": 0}

    total_persisted = ok = failed = 0
    for i, mid in enumerate(managers, 1):
        try:
            stats = orchestrator.generate_for_manager(mid, as_of)
            total_persisted += stats["persisted"]
            ok += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.warning("manager_regen_failed", manager_id=mid, error=str(exc))
        if i % 5 == 0:
            log.info("regen_progress", done=i, total=len(managers), persisted=total_persisted)

    woken = lifecycle.wake_snoozed()
    heads = sig.head_user_ids()
    sla = lifecycle.sweep_sla(escalate_to=heads[0] if heads else None)
    expired = lifecycle.sweep_expired()

    stats = {"managers": len(managers), "ok": ok, "failed": failed,
             "tasks_persisted": total_persisted, "snoozed_woken": woken,
             "sla_breached": sla["flagged"], "sla_escalated": sla["escalated"],
             "expired_purged": expired, "reco_feedback_clients": fb["clients"],
             "reco_feedback_products": fb["products"],
             "duration_s": round(time.time() - started, 1), "as_of": as_of}
    log.info("regen_done", **stats)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    run(as_of=args.as_of, limit=args.limit)


if __name__ == "__main__":
    main()
