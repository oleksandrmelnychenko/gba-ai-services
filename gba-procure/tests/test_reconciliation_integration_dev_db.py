"""Read-only end-to-end reconciliation against the configured development database."""

from __future__ import annotations

import os
from datetime import date

import pytest

pytestmark = pytest.mark.integration

_DB_ENV_READY = bool(os.getenv("DB_PASSWORD")) and bool(os.getenv("DB_HOST"))

if not _DB_ENV_READY:
    pytest.skip(
        "DB env not configured (set DB_HOST/DB_PASSWORD); reconciliation integration skipped",
        allow_module_level=True,
    )

from app.core.config import get_settings  # noqa: E402
from app.data import cache  # noqa: E402
from app.services.reconciliation import (  # noqa: E402
    ReconciliationExitCode,
    run_reconciliation,
)
from app.services.replenishment import policy  # noqa: E402


def test_live_canonical_cart_reconciles_without_redis_io(monkeypatch):
    """Every returned product/quantity/cent must match independent SQL in one source epoch."""
    as_of = date.today().isoformat()
    settings = get_settings()
    redis_client_calls: list[str] = []

    monkeypatch.setattr(
        cache,
        "_get_client",
        lambda: redis_client_calls.append("client"),
    )
    monkeypatch.setattr(cache, "get", lambda _key: None)
    monkeypatch.setattr(cache, "set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cache, "delete", lambda *_args, **_kwargs: False)
    payload = policy.build_cart_plan(
        as_of,
        only_needed=True,
        limit=None,
        source_fingerprint=None,
    ).model_dump(mode="json")

    report = run_reconciliation(
        as_of,
        settings.history_days,
        lambda: payload,
        repeat_builds=1,
        strict_coverage=False,
    )

    assert redis_client_calls == []
    assert report.exit_code == ReconciliationExitCode.EXACT, report.to_dict()
    assert report.source_epoch_before == report.source_epoch_after
    assert report.metrics["plan_items"] == report.metrics["unique_plan_products"]
    assert report.metrics["consignment_drift_keys"] == 0
