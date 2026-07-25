"""Tiny smoke test for the solvency-v3 dataset label fn.

Marked `integration` (DB-backed) like the other live tests; skipped without DB env.
Asserts (1) label fn returns a strict 0/1, and (2) a known severely-overdue buyer labels 1.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

LABEL_DATE = os.environ.get(
    "SOLVENCY_TEST_AS_OF", datetime.now(UTC).date().isoformat()
)


def _has_db() -> bool:
    """True when a read-only DB password is configured (via .env or env). Mirrors prod load."""
    try:
        from app.core.config import get_settings

        return bool(get_settings().db_password)
    except Exception:
        return False


@pytest.fixture(scope="module")
def known_overdue_client() -> int:
    """Resolve a current positive independently of the Python label implementation."""
    from app.data.db import query
    from app.risk.dataset import SEV180_MIN_EUR

    rows = query(
        """
        SELECT TOP 1 cid.ClientID AS client_id,
               SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)) AS sev180_eur
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        WHERE cid.Deleted = 0
              AND d.Deleted = 0
              AND d.Created <= :asof
              AND DATEDIFF(day, d.Created, :asof) > a.NumberDaysDebt + 180
        GROUP BY cid.ClientID
        HAVING SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)) >= :minimum
        ORDER BY SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)) DESC,
                 cid.ClientID
        """,
        {"asof": LABEL_DATE, "minimum": SEV180_MIN_EUR},
    )
    if not rows:
        pytest.skip(f"no current SEV180-positive client as of {LABEL_DATE}")
    return int(rows[0]["client_id"])


@pytest.mark.integration
@pytest.mark.skipif(not _has_db(), reason="requires DB env")
def test_label_returns_binary(known_overdue_client) -> None:
    from app.risk.dataset import label_sev180_one

    val = label_sev180_one(known_overdue_client, LABEL_DATE)
    assert val in (0, 1)


@pytest.mark.integration
@pytest.mark.skipif(not _has_db(), reason="requires DB env")
def test_current_overdue_client_is_positive(known_overdue_client) -> None:
    from app.risk.dataset import label_sev180_one

    assert label_sev180_one(known_overdue_client, LABEL_DATE) == 1
