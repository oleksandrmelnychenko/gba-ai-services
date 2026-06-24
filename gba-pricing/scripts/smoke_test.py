"""Live smoke test — prices a single product × client-agreement end-to-end against the dev DB.

Requires a populated .env (read-only login). NOT run in CI; the user owns bring-up.
Usage: .venv/bin/python scripts/smoke_test.py <product_id> <client_agreement_net_uid>
"""
from __future__ import annotations

import sys

from app.core.logging import get_logger
from app.services.pricing import service

log = get_logger("smoke")


def main() -> int:
    if len(sys.argv) < 3:
        log.error("smoke_usage", usage="smoke_test.py <product_id> <client_agreement_net_uid>")
        return 2
    product_id = int(sys.argv[1])
    ca_net_uid = sys.argv[2]
    result = service.recommend_price(
        product_id=product_id, product_net_uid=None, client_agreement_net_uid=ca_net_uid,
    )
    log.info(
        "smoke_price",
        product_id=result.product_id,
        baseline_price=result.baseline_price,
        recommended_price=result.recommended_price,
        price_floor=result.price_floor,
        suggested_discount_pct=result.suggested_discount_pct,
        confidence=result.confidence,
        rationale=result.rationale,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
