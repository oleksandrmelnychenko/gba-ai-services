"""Smoke test: run the recommender against the live read-only DB for a few customers.

Usage: DB_PASSWORD=... .venv/bin/python scripts/smoke_test.py
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.recommendations import recommender  # noqa: E402

DEFAULT_TEST_CUSTOMERS = [410169, 410175, 410176, 410180]
DEFAULT_AS_OF = "2024-07-01"


def _parse_customers(raw: str) -> list[int]:
    customers = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not customers:
        raise argparse.ArgumentTypeError("at least one customer id is required")
    return customers


def validate_result(customer_id: int, result, top_n: int) -> list[str]:
    """Return smoke-test contract violations for one recommender response."""
    errors: list[str] = []
    recs = list(result.recommendations)

    if not recs:
        errors.append(f"customer {customer_id}: empty recommendation list")
    if result.count != len(recs):
        errors.append(f"customer {customer_id}: count={result.count} != recommendations={len(recs)}")
    if len(recs) > top_n:
        errors.append(f"customer {customer_id}: returned {len(recs)} recs for top_n={top_n}")
    if result.discovery_count < 0 or result.discovery_count > len(recs):
        errors.append(
            f"customer {customer_id}: invalid discovery_count={result.discovery_count} for {len(recs)} recs"
        )
    if not math.isfinite(float(result.latency_ms)) or float(result.latency_ms) < 0:
        errors.append(f"customer {customer_id}: invalid latency_ms={result.latency_ms}")
    if not getattr(result, "model_version", ""):
        errors.append(f"customer {customer_id}: empty model_version")

    ranks = [int(r.rank) for r in recs]
    expected_ranks = list(range(1, len(recs) + 1))
    if ranks != expected_ranks:
        errors.append(f"customer {customer_id}: ranks={ranks} expected={expected_ranks}")

    product_ids = [int(r.product_id) for r in recs]
    if len(product_ids) != len(set(product_ids)):
        errors.append(f"customer {customer_id}: duplicate product ids in recommendations")

    for rec in recs:
        if int(rec.product_id) <= 0:
            errors.append(f"customer {customer_id}: invalid product_id={rec.product_id}")
        if not math.isfinite(float(rec.score)):
            errors.append(f"customer {customer_id}: invalid score for product {rec.product_id}: {rec.score}")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", default=DEFAULT_AS_OF)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument(
        "--customers",
        type=_parse_customers,
        default=DEFAULT_TEST_CUSTOMERS,
        help="comma-separated client ids",
    )
    args = parser.parse_args(argv)
    if args.top_n <= 0:
        parser.error("--top-n must be positive")

    failures: list[str] = []
    for cid in args.customers:
        res = recommender.recommend(cid, as_of_date=args.as_of, top_n=args.top_n)
        print(f"\nCustomer {cid}: {res.segment} | {res.count} recs "
              f"({res.discovery_count} discovery) | {res.latency_ms}ms")
        for r in res.recommendations:
            print(f"  {r.rank:2}. product {r.product_id:>10}  score={r.score:.4f}  {r.source.value}")
        failures.extend(validate_result(cid, res, args.top_n))

    if failures:
        print("\nSMOKE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nSMOKE PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
