"""Smoke test: run the recommender against the live read-only DB for a few customers.

Usage: DB_PASSWORD=... .venv/bin/python scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.recommendations import recommender  # noqa: E402

TEST_CUSTOMERS = [410169, 410175, 410176, 410180]
AS_OF = "2024-07-01"


def main() -> None:
    for cid in TEST_CUSTOMERS:
        res = recommender.recommend(cid, as_of_date=AS_OF, top_n=10)
        print(f"\nCustomer {cid}: {res.segment} | {res.count} recs "
              f"({res.discovery_count} discovery) | {res.latency_ms}ms")
        for r in res.recommendations:
            print(f"  {r.rank:2}. product {r.product_id:>10}  score={r.score:.4f}  {r.source.value}")


if __name__ == "__main__":
    main()
