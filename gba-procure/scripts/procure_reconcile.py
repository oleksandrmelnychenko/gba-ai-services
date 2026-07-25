"""Read-only product/quantity/cent reconciliation gate for the canonical cart.

The default path builds the plan in-process with every Redis operation disabled, then
checks it against independent SQL reads.  It never invalidates or warms a cache.

Examples:
  .venv/bin/python scripts/procure_reconcile.py --as-of 2026-07-25
  .venv/bin/python scripts/procure_reconcile.py --plan-json cart.json --repeat-builds 1
  .venv/bin/python scripts/procure_reconcile.py --strict-coverage --output report.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402
from app.data import cache  # noqa: E402
from app.services.reconciliation import (  # noqa: E402
    ReconciliationExitCode,
    run_reconciliation,
)
from app.services.replenishment import policy  # noqa: E402


@contextlib.contextmanager
def _without_cache_io() -> Iterator[None]:
    """Prevent even accidental Redis reads/writes while the local plan is built."""
    original_get = cache.get
    original_set = cache.set
    original_delete = cache.delete
    original_exists = cache.exists
    cache.get = lambda _key: None
    cache.set = lambda *_args, **_kwargs: None
    cache.delete = lambda _key: False
    cache.exists = lambda _key: False
    try:
        yield
    finally:
        cache.get = original_get
        cache.set = original_set
        cache.delete = original_delete
        cache.exists = original_exists


def _local_plan_factory(as_of: str) -> Callable[[], Any]:
    def build() -> Any:
        with _without_cache_io():
            return policy.build_cart_plan(
                as_of,
                only_needed=True,
                limit=None,
                source_fingerprint=None,
            )

    return build


def _file_plan_factory(path: Path) -> Callable[[], dict[str, Any]]:
    def load() -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)

    return load


def _today_in_service_timezone() -> str:
    settings = get_settings()
    return datetime.now(ZoneInfo(settings.timezone)).date().isoformat()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only procurement reconciliation gate.",
    )
    parser.add_argument("--as-of", default=None, help="audit date (YYYY-MM-DD)")
    parser.add_argument(
        "--plan-json",
        type=Path,
        default=None,
        help="validate an existing canonical plan JSON instead of building locally",
    )
    parser.add_argument(
        "--repeat-builds",
        type=int,
        default=2,
        help="number of independent builds used for deterministic digest verification",
    )
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help="fail with exit 5 when live data has no reservation or in-transit example",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="also write the JSON report to this path",
    )
    return parser


def _emit(payload: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    if output is not None:
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.repeat_builds < 1:
        _parser().error("--repeat-builds must be at least 1")

    as_of = args.as_of or _today_in_service_timezone()
    settings = get_settings()
    plan_factory = (
        _file_plan_factory(args.plan_json) if args.plan_json is not None else _local_plan_factory(as_of)
    )
    if args.plan_json is not None and not args.plan_json.is_file():
        _parser().error(f"plan file does not exist: {args.plan_json}")

    try:
        # Application logs belong on stderr; stdout stays a machine-readable JSON report.
        with contextlib.redirect_stdout(sys.stderr):
            report = run_reconciliation(
                as_of,
                settings.history_days,
                plan_factory,
                repeat_builds=args.repeat_builds,
                strict_coverage=args.strict_coverage,
            )
        _emit(report.to_dict(), args.output)
        return int(report.exit_code)
    except Exception as exc:  # noqa: BLE001
        payload = {
            "schema_version": 1,
            "as_of": as_of,
            "ok": False,
            "exit_code": int(ReconciliationExitCode.INTERNAL_ERROR),
            "exit_name": ReconciliationExitCode.INTERNAL_ERROR.name.lower(),
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        _emit(payload, args.output)
        return int(ReconciliationExitCode.INTERNAL_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
