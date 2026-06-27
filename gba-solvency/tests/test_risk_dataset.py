"""Tiny smoke test for the solvency-v3 dataset label fn.

Marked `integration` (DB-backed) like the other live tests; skipped without DB env.
Asserts (1) label fn returns a strict 0/1, and (2) a known severely-overdue buyer labels 1.
"""
from __future__ import annotations

import pytest

LABEL_DATE = "2026-06-25"
KNOWN_OVERDUE_CLIENT = 411780  # ТРАМП ОЙЛ — large 180+ overdue exposure


def _has_db() -> bool:
    """True when a read-only DB password is configured (via .env or env). Mirrors prod load."""
    try:
        from app.core.config import get_settings

        return bool(get_settings().db_password)
    except Exception:
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _has_db(), reason="requires DB env")
def test_label_returns_binary() -> None:
    from app.risk.dataset import label_sev180_one

    val = label_sev180_one(KNOWN_OVERDUE_CLIENT, LABEL_DATE)
    assert val in (0, 1)


@pytest.mark.integration
@pytest.mark.skipif(not _has_db(), reason="requires DB env")
def test_known_overdue_client_is_positive() -> None:
    from app.risk.dataset import label_sev180_one

    assert label_sev180_one(KNOWN_OVERDUE_CLIENT, LABEL_DATE) == 1
