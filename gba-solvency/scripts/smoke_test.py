"""Live smoke test — scores a single client end-to-end against the dev DB.

Requires a populated .env (read-only login). NOT run in CI; the user owns bring-up.
Usage: .venv/bin/python scripts/smoke_test.py [client_id]
"""
from __future__ import annotations

import sys

from app.core.logging import get_logger
from app.services.solvency import service

log = get_logger("smoke")


def main() -> int:
    client_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    result = service.score_client(client_id=client_id)
    log.info(
        "smoke_score",
        client_id=result.client_id,
        score=result.score,
        rating=result.rating,
        debt_load_source=result.debt_load_source,
        caps=result.caps_applied,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
