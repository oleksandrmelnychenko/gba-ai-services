"""One-shot: migrate legacy reco:neg:{client_id} sets (minted-id key, product-id members)
onto the natural-key scheme reco:neg:{net_uid} with Product.VendorCode members.

Keys whose integer client id no longer resolves to a LIVE dbo.Client row are dropped — the
identity is dead, so there is nothing to attach the negatives to. Idempotent: already-migrated
GUID-keyed sets are left untouched.

Run:  .venv/bin/python -m scripts.migrate_neg_keys
"""
from __future__ import annotations

import redis

from app.core.config import get_settings
from app.data import sales_repository as repo
from app.data.db import query


def main() -> None:
    s = get_settings()
    r = redis.Redis(host=s.redis_host, port=s.redis_port, db=s.redis_db, decode_responses=True)
    migrated = dropped = already_natural = 0
    for key in list(r.scan_iter(match="reco:neg:*", count=200)):
        suffix = key.rsplit(":", 1)[1]
        if not suffix.isdigit():
            already_natural += 1
            continue
        cid = int(suffix)
        rows = query(
            "SELECT NetUID AS uid FROM dbo.Client WHERE ID = :cid AND Deleted = 0",
            {"cid": cid},
        )
        if not rows or rows[0]["uid"] is None:
            r.delete(key)
            dropped += 1
            print(f"dropped {key} (client {cid} is not a live client)")
            continue
        net_uid = str(rows[0]["uid"]).lower()
        product_ids = [int(x) for x in r.smembers(key)]
        codes = repo.product_vendor_codes(product_ids)
        new_key = f"reco:neg:{net_uid}"
        if codes:
            r.sadd(new_key, *codes)
            old_ttl = r.ttl(key)
            new_ttl = r.ttl(new_key)
            if old_ttl > 0:
                r.expire(new_key, max(old_ttl, new_ttl if new_ttl > 0 else 0))
        r.delete(key)
        migrated += 1
        print(f"migrated {key} -> {new_key} ({len(product_ids)} ids -> {len(codes)} vendor codes)")
    print(f"done: migrated={migrated} dropped={dropped} already_natural={already_natural}")


if __name__ == "__main__":
    main()
