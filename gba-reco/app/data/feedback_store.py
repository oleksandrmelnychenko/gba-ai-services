"""Durable negative-feedback journal.

Redis is the fast path for «не пропонувати» exclusions, but a TTL'd set in a cache that has
been flushed before is not a system of record: a redis flush (or 90 quiet days) silently
resurrects every rejected product. This module keeps an append-only JSONL journal of every
accepted feedback event under the same natural keys the cache uses (Client.NetUID +
Product.VendorCode) and replays it into Redis on service startup, re-arming the TTL.

The journal is tiny (one line per feedback call) and append-only; corrupt/partial lines are
skipped on replay so a torn write can never take the service down.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("feedback_store")

_lock = threading.Lock()


def _store_path() -> Path:
    configured = getattr(get_settings(), "feedback_store_path", "") or ""
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data" / "negatives.jsonl"


def append(net_uid: str, vendor_codes: list[str]) -> None:
    """Journal one accepted feedback event. Failures are logged, never raised — the Redis
    write already succeeded and the caller's response must not depend on disk."""
    if not net_uid or not vendor_codes:
        return
    line = json.dumps({
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "net_uid": net_uid.lower(),
        "vendor_codes": sorted(str(c) for c in vendor_codes),
    }, ensure_ascii=False)
    try:
        path = _store_path()
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        log.warning("feedback_journal_append_failed", error=str(exc))


def load() -> dict[str, set[str]]:
    """The journal collapsed to {client_net_uid: {vendor_code, ...}}. Bad lines are skipped."""
    path = _store_path()
    out: dict[str, set[str]] = {}
    if not path.exists():
        return out
    skipped = 0
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                doc = json.loads(raw)
                net_uid = str(doc["net_uid"]).lower()
                codes = {str(c) for c in doc["vendor_codes"] if c}
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            if net_uid and codes:
                out.setdefault(net_uid, set()).update(codes)
    if skipped:
        log.warning("feedback_journal_lines_skipped", skipped=skipped)
    return out


def replay_into_redis() -> int:
    """Re-arm Redis negative sets from the journal (startup self-heal after a flush/expiry).
    Returns the number of clients replayed; 0 and a warning if Redis is unavailable."""
    from app.data import cache

    journal = load()
    if not journal:
        return 0
    client = cache._get_client()
    if client is None:
        log.warning("feedback_replay_skipped_no_redis", clients=len(journal))
        return 0
    ttl = get_settings().feedback_ttl
    replayed = 0
    for net_uid, codes in journal.items():
        try:
            key = cache._neg_key(net_uid)
            client.sadd(key, *sorted(codes))
            client.expire(key, ttl)
            replayed += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("feedback_replay_failed", net_uid=net_uid, error=str(exc))
    log.info("feedback_replayed", clients=replayed,
             codes=sum(len(c) for c in journal.values()))
    return replayed
