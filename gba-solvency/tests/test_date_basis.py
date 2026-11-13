"""Date basis of 2026-11-13: Kyiv-local ages, invoice-dated sales, gap-free monthly series.

No database: every query is captured and its SQL text and bound parameters are asserted.
"""
from __future__ import annotations

import inspect
import re
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.core import history
from app.data import solvency_repository as repo
from app.data import source_rules
from app.risk import dataset
from app.services.solvency import charts

KYIV_DAY = "AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS date)"
DEBT_DAY = f"CAST(d.Created {KYIV_DAY}"


class Capture:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.rows: list[dict] = []

    def __call__(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        if "AS sev180_eur" in sql and "cid.ClientID = :cid" in sql:
            return [{"sev180_eur": Decimal(0)}]
        return list(self.rows)


@pytest.fixture
def capture(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(repo, "query", cap)
    monkeypatch.setattr(dataset, "query", cap)
    monkeypatch.setattr(repo, "synthetic_product_ids", lambda: {29555414})
    monkeypatch.setattr(dataset, "synthetic_product_ids", lambda: {29555414})
    return cap


def test_days_since_compares_the_kyiv_date_of_the_stored_instant():
    assert source_rules.local_date("d.Created") == DEBT_DAY
    assert source_rules.days_since("d.Created", ":asof") == f"DATEDIFF(day, {DEBT_DAY}, :asof)"
    assert source_rules.months_since("MIN(s.ChangedToInvoice)", ":fd") == (
        f"DATEDIFF(month, CAST(MIN(s.ChangedToInvoice) {KYIV_DAY}, :fd)"
    )
    assert source_rules.sold_at() == "s.ChangedToInvoice"
    assert source_rules.sold_at("s2") == "s2.ChangedToInvoice"


_DEBT_AGE_READS = [
    lambda: repo.debt_aging_buckets_truth(1, "2026-11-13", "2026-11-13"),
    lambda: repo.debt_exposure_donut_truth(1, "2026-11-13", 12, "2026-11-13"),
    lambda: repo.overdue_amount_eur(1, "2026-11-13", "2026-11-13"),
    lambda: dataset.feat_debt_aging("2026-11-13"),
    lambda: dataset.label_sev180("2026-11-13", [1]),
    lambda: dataset.label_sev180_one(1, "2026-11-13"),
    lambda: dataset.features_one(1, "2026-11-13", 12),
    lambda: dataset.features_many([1, 2], "2026-11-13", 12),
]


@pytest.mark.parametrize("read", _DEBT_AGE_READS)
def test_debt_age_is_counted_from_the_kyiv_day_of_the_debt(capture, read, monkeypatch):
    monkeypatch.setattr(repo, "total_sales_count", lambda *_a: 0)
    read()
    aged = [sql for sql, _ in capture.calls if "NumberDaysDebt" in sql and "dbo.Debt" in sql]
    assert aged
    for sql in aged:
        assert f"DATEDIFF(day, {DEBT_DAY}, :" in sql, sql
        assert "DATEDIFF(day, d.Created" not in sql
        assert re.search(r"d\.Created <= :(asof|fd)", sql), "the visibility cutoff is unchanged"


@pytest.mark.parametrize(
    "read",
    [
        lambda: dataset.feat_debt_trajectory("2026-11-13"),
        lambda: dataset.features_one(1, "2026-11-13", 12),
        lambda: dataset.features_many([1, 2], "2026-11-13", 12),
    ],
)
def test_debt_trajectory_windows_and_months_use_kyiv_days(capture, read):
    read()
    trajectory = [sql for sql, _ in capture.calls if "new3" in sql]
    assert len(trajectory) == 1
    sql = trajectory[0]
    assert f"{DEBT_DAY} AS created_day" in sql
    assert "created_day >= DATEADD(month, -3, :fd)" in sql
    assert "created_day < DATEADD(month, -3, :fd)" in sql
    assert "created_day >= DATEADD(month, -12, :fd)" in sql
    assert "FORMAT(" not in sql
    assert "d.Created >" not in sql.replace("d.Created >= :history_start", "")


@pytest.mark.parametrize(
    "read",
    [
        lambda: dataset.feat_rfm("2026-11-13", 12),
        lambda: dataset.features_one(1, "2026-11-13", 12),
        lambda: dataset.features_many([1, 2], "2026-11-13", 12),
        lambda: repo.activity_stats(1, "2026-11-13", 12),
    ],
)
def test_recency_and_tenure_use_the_kyiv_day_of_the_invoice(capture, read):
    read()
    history_reads = [sql for sql, _ in capture.calls if "recency_days" in sql]
    assert len(history_reads) == 1
    sql = history_reads[0]
    assert f"DATEDIFF(day, CAST(MAX(s.ChangedToInvoice) {KYIV_DAY}, :" in sql
    assert f"DATEDIFF(month, CAST(MIN(s.ChangedToInvoice) {KYIV_DAY}, :" in sql


def test_no_sql_placeholder_is_left_unrendered(capture, monkeypatch):
    monkeypatch.setattr(repo, "total_sales_count", lambda *_a: 0)
    for read in _DEBT_AGE_READS:
        read()
    dataset.feat_debt_trajectory("2026-11-13")
    dataset.feat_rfm("2026-11-13", 12)
    dataset.feat_returns("2026-11-13", 12)
    for sql, _ in capture.calls:
        assert "{" not in sql and "}" not in sql, sql


_INVOICE_DATED = (
    repo.turnover_eur,
    repo.turnover_eur_by_currency,
    repo.turnover_eur_by_currency_many,
    repo.activity_stats,
    repo.return_qty_rate,
    repo.monthly_turnover_series,
    dataset.feat_rfm,
    dataset.feat_returns,
    dataset.features_one,
    dataset.features_many,
)


@pytest.mark.parametrize("fn", _INVOICE_DATED)
def test_confirmed_sale_consumers_never_date_a_sale_by_its_creation(fn):
    source = inspect.getsource(fn)
    assert "confirmed_sale(" in source
    assert not re.search(r"\bs[23]?\.Created\b", source), fn.__name__


def test_monthly_series_buckets_the_kyiv_month_of_the_invoice(capture):
    repo.monthly_turnover_series(1, "2026-11-13", 12, "2026-11-13")
    sql, params = capture.calls[0]
    month = f"CONVERT(char(7), CAST(s.ChangedToInvoice {KYIV_DAY}, 126)"
    assert f"SELECT {month} AS period" in sql
    assert f"GROUP BY {month}" in sql
    assert "s.ChangedToInvoice >= :window_start AND s.ChangedToInvoice < :window_end" in sql
    assert params["window_start"] == datetime(2025, 11, 30, 22, 0)
    assert params["window_end"] == datetime(2026, 11, 12, 22, 0)


def test_monthly_series_has_an_explicit_zero_for_a_month_without_sales(capture):
    capture.rows = [
        {"period": "2026-01", "turnover_eur": Decimal("100.004")},
        {"period": "2026-09", "turnover_eur": Decimal("7.5")},
    ]
    series = repo.monthly_turnover_series(1, "2026-11-13", 12, "2026-11-13")
    assert [point["period"] for point in series] == [
        "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
        "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11",
    ]
    by_period = {point["period"]: point["turnover_eur"] for point in series}
    assert by_period["2026-01"] == Decimal("100.00")
    assert by_period["2026-09"] == Decimal("7.50")
    assert all(
        value == Decimal("0.00") and isinstance(value, Decimal)
        for period, value in by_period.items() if period not in {"2026-01", "2026-09"}
    )


def test_client_without_sales_gets_a_flat_zero_series(capture):
    series = repo.monthly_turnover_series(1, "2026-11-13", 12, "2026-11-13")
    assert len(series) == 12
    assert {point["turnover_eur"] for point in series} == {Decimal("0.00")}


@pytest.mark.parametrize(
    ("as_of", "months", "periods"),
    [
        ("2026-11-13", 3, ("2026-09", "2026-10", "2026-11")),
        ("2026-11-01", 3, ("2026-08", "2026-09", "2026-10")),
        ("2026-11-02", 1, ("2026-11",)),
        ("2026-11-13", 1, ("2026-11",)),
        ("2026-01-15", 2, ("2025-12", "2026-01")),
        ("2025-03-10", 12, ("2025-01", "2025-02", "2025-03")),
    ],
)
def test_series_months_run_from_first_full_month_to_the_last_complete_day(
    as_of, months, periods
):
    assert history.sales_window(as_of, months).periods == periods


def test_month_keys_cross_a_year_boundary_and_reject_an_empty_range():
    assert history.month_keys(date(2025, 11, 1), date(2026, 2, 28)) == (
        "2025-11", "2025-12", "2026-01", "2026-02",
    )
    assert history.month_keys(date(2026, 12, 1), date(2026, 11, 30)) == ()


def test_chart_points_keep_zero_months(capture):
    monthly = repo.monthly_turnover_series(1, "2026-11-13", 3, "2026-11-13")
    trend = charts._turnover_trend(monthly)
    exposure = charts._turnover_vs_exposure(monthly, 12.345)
    assert [(p.period, p.turnover_eur) for p in trend] == [
        ("2026-09", 0.0), ("2026-10", 0.0), ("2026-11", 0.0),
    ]
    assert [(p.period, p.turnover_eur, p.exposure_eur) for p in exposure] == [
        ("2026-09", 0.0, 12.35), ("2026-10", 0.0, 12.35), ("2026-11", 0.0, 12.35),
    ]


def test_charts_no_longer_issue_the_unfiltered_payment_status_reads():
    source = inspect.getsource(charts)
    assert "open_unpaid" not in source and "payment_status_counts" not in source
    assert not hasattr(repo, "open_unpaid_aging_buckets")
