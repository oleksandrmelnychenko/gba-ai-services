"""DB-backed regression smoke (pytest.mark.integration) — runs against the live dev ConcordDb_V5.

SKIPPED when DB_PASSWORD is absent so default CI stays green without a DB; run with
`make integration` / `pytest -m integration` against the dev DB. These assert the SANE MAGNITUDES
that only live smoke caught: per-client overdue debt in real EUR (NOT the ~50x inflated 6-figure
UAH-as-EUR numbers), and a real spread of task urgencies (NOT every task saturated to critical).
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("DB_PASSWORD"):
    pytest.skip("integration: set DB_* env to run against dev ConcordDb_V5", allow_module_level=True)

from app.core.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.core.money import cents  # noqa: E402
from app.data import signals_repository as sig  # noqa: E402
from app.services import targets  # noqa: E402
from app.services.generators import (  # noqa: E402
    churn_winback,
    debt_followup,
    reorder_due,
)

MANAGER_NETUID = "9E8CC9EA-9F71-4546-988E-0F2F388B7B43"
AS_OF = datetime.now(ZoneInfo(get_settings().timezone)).date().isoformat()
WIN = AS_OF[:7]


@pytest.fixture(scope="module")
def manager_id() -> int:
    mid = sig.manager_id_for_netuid(MANAGER_NETUID)
    assert mid is not None, "test manager NetUID not found in dev DB"
    return mid


def test_overdue_debts_are_real_euro_not_inflated(manager_id):
    rows = sig.overdue_debts_for_manager(manager_id, AS_OF, min_amount=10.0)
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
    for gen in (debt_followup, reorder_due, churn_winback):  # new_client_activation retired (unwired)
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


def test_target_mtd_matches_independent_sql_to_the_cent(manager_id):
    from app.data.db import query

    result = targets.compute_target(manager_id, as_of=AS_OF)
    month_start = f"{AS_OF[:7]}-01"
    asof_exclusive = (
        datetime.fromisoformat(AS_OF).date()
        + __import__("datetime").timedelta(days=1)
    ).isoformat()
    synthetic = sorted(sig._excluded(AS_OF))
    from app.data.db import in_clause

    excluded_sql, excluded_params = in_clause("excluded", synthetic)
    shipped = query(
        f"""
        SELECT SUM(CAST(oi.Qty AS decimal(38, 14))
                   * CAST(oi.PricePerItem AS decimal(38, 14))) AS amount
        FROM dbo.[Order] o
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.Client c ON c.ID = ca.ClientID
        WHERE c.Deleted = 0 AND c.MainManagerID = :mid
              AND oi.IsValidForCurrentSale = 1
              AND o.Created >= :month_start AND o.Created < :asof_exclusive
              AND oi.ProductID IS NOT NULL
              AND oi.ProductID NOT IN {excluded_sql}
        """,
        {
            "mid": manager_id,
            "month_start": month_start,
            "asof_exclusive": asof_exclusive,
            **excluded_params,
        },
    )[0]["amount"]
    paid = query(
        """
        SELECT SUM(CAST(dbo.GetExchangedToEuroValue(
                       p.Amount, p.CurrencyID, p.FromDate) AS decimal(38, 14))) AS amount
        FROM dbo.IncomePaymentOrder p
        JOIN dbo.Client c ON c.ID = p.ClientID
        WHERE c.Deleted = 0 AND c.MainManagerID = :mid
              AND p.Deleted = 0
              AND p.FromDate >= :month_start AND p.FromDate < :asof_exclusive
        """,
        {
            "mid": manager_id,
            "month_start": month_start,
            "asof_exclusive": asof_exclusive,
        },
    )[0]["amount"]

    assert result["shipped"]["mtd"] == cents(shipped)
    assert result["paid"]["mtd"] == cents(paid)


def test_monthly_history_clamp_matches_explicit_source_floor(manager_id):
    """A pre-floor caller cannot make either accounting series read unavailable source history."""
    pre_floor = "2024-01-01"
    source_floor = get_settings().source_history_start_date.isoformat()

    def to_cents(series):
        return {month: cents(amount) for month, amount in series.items()}

    assert to_cents(sig.monthly_shipped(manager_id, pre_floor, AS_OF)) == to_cents(
        sig.monthly_shipped(manager_id, source_floor, AS_OF)
    )
    assert to_cents(sig.monthly_paid(manager_id, pre_floor, AS_OF)) == to_cents(
        sig.monthly_paid(manager_id, source_floor, AS_OF)
    )


def test_book_scope_candidates_are_the_union_of_every_manager(manager_id):
    """The retrain dataset enumerates through the serving readers with no manager: what it
    sees for a manager's clients is exactly what that manager's generator sees."""
    as_of = datetime.fromisoformat(AS_OF).date().replace(day=1).isoformat()
    book_churn = {int(r["client_id"]): (int(r["recent_orders"]), int(r["prior_orders"]),
                                        int(r["silence_days"]))
                  for r in sig.churn_candidates_for_manager(None, as_of)}
    own_churn = {int(r["client_id"]): (int(r["recent_orders"]), int(r["prior_orders"]),
                                       int(r["silence_days"]))
                 for r in sig.churn_candidates_for_manager(manager_id, as_of)}
    assert own_churn and own_churn.items() <= book_churn.items()

    book = {(int(r["client_id"]), int(r["product_id"])): (int(r["n_orders"]), int(r["elapsed_days"]))
            for r in sig.reorder_candidates_for_manager(None, as_of)}
    own = {(int(r["client_id"]), int(r["product_id"])): (int(r["n_orders"]), int(r["elapsed_days"]))
           for r in sig.reorder_candidates_for_manager(manager_id, as_of)}
    assert own and own.items() <= book.items()
    own_clients = {int(r["client_id"]) for r in sig.clients_for_manager(manager_id)}
    assert {pair for pair in book if pair[0] in own_clients} == set(own)


def test_dataset_rows_carry_the_served_features_and_later_labels(manager_id):
    from app.ml import dataset as ds

    vintage = "2026-09-01"
    served = {(int(r["client_id"]), int(r["product_id"]))
              for r in sig.reorder_candidates_for_manager(manager_id, vintage)}
    rows = {(r["client_id"], r["product_id"]) for r in ds.reorder_candidates(vintage)}
    excluded = ds._excluded_pids(vintage)
    assert {pair for pair in served if pair[1] not in excluded} <= rows
    bought = sig.purchases_between(sorted({c for c, _ in served})[:50], vintage, "2026-10-31")
    assert bought and all(int(r["product_id"]) > 0 for r in bought)


def test_opening_balances_reconcile_between_card_column_and_tasks(manager_id):
    from app.data import debt_snapshot

    card = debt_snapshot.dashboards(AS_OF)[manager_id]
    book = debt_snapshot.overdue_clients(AS_OF, manager_id, with_unverified=True)
    assert cents(sum(r["overdue_amount"] for r in book)) == card["value_at_risk_eur"]
    assert cents(sum(r["opening_balance_amount"] for r in book)) == card["debt_opening_balance_eur"]
    assert sum(r["opening_balance_lines"] for r in book) == card["debt_opening_balance_count"]
    assert cents(sum(r["unknown_date_amount"] for r in book)) == card["debt_unknown_date_eur"]
    assert card["value_at_risk_eur"] == cents(sum(b["amount_eur"] for b in card["debt_aging"]))
    grace = get_settings().debt_grace_days
    for row in sig.debt_task_candidates(manager_id, AS_OF):
        assert row["max_days_past_terms"] > grace and row["debt_lines"] >= 1
        assert float(row["overdue_amount"]) >= get_settings().debt_min_amount
    opening_only = {r["client_id"] for r in book if r["opening_balance_lines"] and not r["task_debt_lines"]}
    assert not opening_only & {r["client_id"] for r in sig.debt_task_candidates(manager_id, AS_OF)}
