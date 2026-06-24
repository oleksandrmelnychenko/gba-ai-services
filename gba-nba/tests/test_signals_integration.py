"""DB-backed regression smoke (pytest.mark.integration) — runs against the live dev ConcordDb_V5.

SKIPPED when DB_PASSWORD is absent so default CI stays green without a DB; run with
`make integration` / `pytest -m integration` against the dev DB. These assert the SANE MAGNITUDES
that only live smoke caught: per-client overdue debt in real EUR (NOT the ~50x inflated 6-figure
UAH-as-EUR numbers), and a real spread of task urgencies (NOT every task saturated to critical).
"""
from __future__ import annotations

import os
from collections import Counter

import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("DB_PASSWORD"):
    pytest.skip("integration: set DB_* env to run against dev ConcordDb_V5", allow_module_level=True)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.data import signals_repository as sig  # noqa: E402
from app.services.generators import (  # noqa: E402
    churn_winback,
    debt_followup,
    new_client_activation,
    reorder_due,
)

MANAGER_NETUID = "9E8CC9EA-9F71-4546-988E-0F2F388B7B43"
AS_OF, WIN = "2026-06-15", "2026-06"


@pytest.fixture(scope="module")
def manager_id() -> int:
    mid = sig.manager_id_for_netuid(MANAGER_NETUID)
    assert mid is not None, "test manager NetUID not found in dev DB"
    return mid


def test_overdue_debts_are_real_euro_not_inflated(manager_id):
    rows = sig.overdue_debts_for_manager(manager_id, AS_OF, max_age_days=365, min_amount=10.0)
    assert rows, "expected this manager to have overdue debt clients on dev data"

    amounts = [float(r["overdue_amount"]) for r in rows]
    assert all(a >= 10.0 for a in amounts), "min_amount EUR threshold must hold per client"

    median = sorted(amounts)[len(amounts) // 2]
    assert median < 50_000, (
        f"median per-client overdue {median:.0f} EUR looks inflated — raw UAH summed as EUR "
        "(~50x) would push the typical client into 5-6 figures"
    )

    small = min(amounts)
    assert small < 5_000, (
        f"smallest per-client overdue {small:.0f} EUR is too high — a modest UAH debt converts to "
        "tens/hundreds of EUR, not thousands; raw-sum would inflate it ~50x past €10k"
    )


def test_overdue_conversion_collapses_uah_inflation(manager_id):
    from app.data.db import query

    rows = query(
        """
        SELECT SUM(dbo.GetExchangedToEuroValue(d.Total, ISNULL(a.CurrencyID, 2), d.Created)) AS eur,
               SUM(d.Total) AS raw_total
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID AND d.Deleted = 0
        JOIN dbo.Client c ON c.ID = cid.ClientID AND c.Deleted = 0
        LEFT JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        WHERE cid.Deleted = 0 AND c.MainManagerID = :mid AND d.Total > 0
              AND a.CurrencyID = :uah
              AND DATEDIFF(day, d.Created, :asof) > ISNULL(a.NumberDaysDebt, 0)
              AND DATEDIFF(day, d.Created, :asof) <= :maxage
        """,
        {"mid": manager_id, "asof": AS_OF, "maxage": 365, "uah": 10038},
    )
    eur = float(rows[0]["eur"] or 0)
    raw = float(rows[0]["raw_total"] or 0)
    assert eur > 0 and raw > 0, "expected UAH-agreement overdue debt for this manager"
    ratio = raw / eur
    assert ratio > 20, (
        f"raw/converted ratio {ratio:.1f} — UAH→EUR should be ~50x; a ratio near 1 means the fix "
        "was reverted and UAH is being summed as EUR"
    )


def test_generate_produces_a_spread_of_urgencies(manager_id):
    candidates = []
    for gen in (debt_followup, reorder_due, churn_winback, new_client_activation):
        candidates.extend(gen.generate(manager_id, AS_OF, WIN))
    assert candidates, "expected candidate tasks for this manager on dev data"

    bands = Counter(t.urgency.value for t in candidates)
    assert len(bands) >= 2, (
        f"urgencies collapsed to a single band {dict(bands)} — inflated debt saturates every task "
        "to critical; a real spread is the regression signal"
    )
    crit_share = bands.get("critical", 0) / len(candidates)
    assert crit_share < 0.95, (
        f"{crit_share:.0%} of tasks are critical — near-total saturation is the inflated-debt symptom"
    )
