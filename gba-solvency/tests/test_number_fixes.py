"""Reconciliation fixes of 2026-11-13: limit gauge, confirmed-sale turnover, zero debt lines.

No database: every query is captured and its SQL text and bound parameters are asserted.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core import history
from app.data import solvency_repository as repo
from app.data import source_rules
from app.domain.models import GaugeChart
from app.risk import dataset
from app.services.solvency import charts, service

_real_unconverted_debt = repo.unconverted_debt


class Capture:
    def __init__(self, rows=None):
        self.calls: list[tuple[str, dict]] = []
        self.rows = rows or []

    def __call__(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        return list(self.rows)

    @property
    def sql(self) -> str:
        return "\n".join(sql for sql, _ in self.calls)


@pytest.fixture
def capture(monkeypatch):
    cap = Capture()
    monkeypatch.setattr(repo, "query", cap)
    monkeypatch.setattr(dataset, "query", cap)
    monkeypatch.setattr(repo, "synthetic_product_ids", lambda: {29555414})
    monkeypatch.setattr(dataset, "synthetic_product_ids", lambda: {29555414})
    return cap


def test_gauge_is_debt_over_limit_of_the_worst_agreement():
    gauge = charts._gauge([
        {"agreement_id": 1, "currency_id": 2, "limit_amount": Decimal("5000"),
         "debt_amount": Decimal("387.19")},
        {"agreement_id": 2, "currency_id": 10038, "limit_amount": Decimal("10000"),
         "debt_amount": Decimal("5410.21")},
    ])
    assert gauge.status == "ok"
    assert gauge.value == 0.541
    assert (gauge.debt_amount, gauge.limit_amount, gauge.currency_id) == (5410.21, 10000.0, 10038)


def test_gauge_above_limit_is_not_clamped():
    gauge = charts._gauge([
        {"agreement_id": 1, "currency_id": 2, "limit_amount": Decimal("1000"),
         "debt_amount": Decimal("2500")},
    ])
    assert gauge.value == 2.5


def test_gauge_with_limit_and_no_debt_is_a_real_zero():
    gauge = charts._gauge([
        {"agreement_id": 1, "currency_id": 2, "limit_amount": Decimal("1000"),
         "debt_amount": Decimal("0")},
    ])
    assert (gauge.value, gauge.status) == (0.0, "ok")


def test_gauge_without_a_limit_is_labelled_not_zero():
    gauge = charts._gauge([])
    assert gauge.value is None
    assert gauge.status == "no_limit"
    assert gauge.label == "no_credit_limit"
    assert gauge.model_dump(mode="json")["value"] is None


def test_gauge_rejects_a_number_without_ok_status_and_the_reverse():
    with pytest.raises(ValidationError):
        GaugeChart(value=0.0, status="no_limit")
    with pytest.raises(ValidationError):
        GaugeChart(value=None)


def test_non_buyer_gauge_is_not_applicable(monkeypatch):
    monkeypatch.setattr(
        service, "coverage", lambda *_a: history.HistoryCoverage(
            date(2025, 1, 1), date(2025, 11, 13), date(2025, 11, 13), True)
    )
    result = service._not_applicable_charts(7, "2026-11-13", 12)
    assert result.limit_utilization_gauge.value is None
    assert result.limit_utilization_gauge.status == "not_applicable"


def test_limit_basis_reads_positive_open_debt_in_the_limit_currency(capture):
    capture.rows = [{"agreement_id": 9, "currency_id": 2, "limit_amount": Decimal("5000.0000"),
                     "debt_amount": Decimal("387.19000000000000")}]
    rows = repo.limit_utilization_basis(631102, "2026-11-13")
    sql, params = capture.calls[0]
    assert "cid.Deleted = 0 AND d.Deleted = 0 AND d.Total >= 0.5" in sql
    assert "da.ID = a.ID OR da.CurrencyID = a.CurrencyID" in sql
    assert "a.IsControlAmountDebt = 1 AND a.AmountDebt > 0" in sql
    assert "ca.Deleted = 0 AND a.Deleted = 0" in sql
    assert "CurrentAmount" not in sql and "GetExchangedToEuroValue" not in sql
    assert params == {"cid": 631102, "asof": "2026-11-13"}
    assert rows == [{"agreement_id": 9, "currency_id": 2, "limit_amount": Decimal("5000.0000"),
                     "debt_amount": Decimal("387.19000000000000")}]


def test_sales_window_is_whole_kyiv_days_as_utc_instants():
    window = history.sales_window("2026-11-13", 12)
    assert window.start == datetime(2025, 11, 12, 22, 0)
    assert window.end == datetime(2026, 11, 12, 22, 0)
    assert window.history_start == datetime(2024, 12, 31, 22, 0)
    summer = history.sales_window("2026-07-01", 1)
    assert summer.start == datetime(2026, 5, 31, 21, 0)
    assert summer.end == datetime(2026, 6, 30, 21, 0)


def test_sales_window_is_clamped_to_the_history_floor():
    window = history.sales_window("2025-06-10", 12)
    assert window.start == window.history_start == window.series_start


@pytest.mark.parametrize(
    ("as_of", "months", "series_start"),
    [
        ("2026-11-13", 12, datetime(2025, 11, 30, 22, 0)),
        ("2026-11-01", 12, datetime(2025, 10, 31, 22, 0)),
        ("2026-12-15", 12, datetime(2025, 12, 31, 22, 0)),
        ("2026-07-20", 1, datetime(2026, 6, 30, 21, 0)),
    ],
)
def test_monthly_series_never_opens_with_a_partial_month(as_of, months, series_start):
    assert history.sales_window(as_of, months).series_start == series_start


def test_local_midnight_follows_daylight_saving():
    assert history.local_midnight_utc(date(2026, 3, 29)) == datetime(2026, 3, 28, 22, 0)
    assert history.local_midnight_utc(date(2026, 3, 30)) == datetime(2026, 3, 29, 21, 0)
    assert history.local_midnight_utc(date(2026, 10, 25)) == datetime(2026, 10, 24, 21, 0)
    assert history.local_midnight_utc(date(2026, 10, 26)) == datetime(2026, 10, 25, 22, 0)


def test_confirmed_sale_rule_matches_the_server_reference():
    sale = source_rules.confirmed_sale()
    assert "s.Deleted = 0" in sale and "s.ChangedToInvoice IS NOT NULL" in sale
    assert "carrier.OwnershipType = 1" in sale and "NOT EXISTS" in sale
    assert source_rules.confirmed_line("x") == (
        "x.Deleted = 0 AND x.IsValidForCurrentSale = 1 AND x.Qty > 0"
    )
    assert source_rules.open_debt() == "cid.Deleted = 0 AND d.Deleted = 0 AND d.Total >= 0.5"


_SALE_READS = [
    lambda: repo.turnover_eur(1, "2026-11-13", 12, "2026-11-13"),
    lambda: repo.turnover_eur_by_currency(1, "2026-11-13", 12, "2026-11-13"),
    lambda: repo.turnover_eur_by_currency_many([1, 2], "2026-11-13", 12, "2026-11-13"),
    lambda: repo.monthly_turnover_series(1, "2026-11-13", 12, "2026-11-13"),
    lambda: repo.activity_stats(1, "2026-11-13", 12),
    lambda: repo.return_qty_rate(1, "2026-11-13", 12),
    lambda: dataset.feat_rfm("2026-11-13", 12),
    lambda: dataset.feat_returns("2026-11-13", 12),
    lambda: dataset.features_one(1, "2026-11-13", 12),
    lambda: dataset.features_many([1, 2], "2026-11-13", 12),
]


@pytest.mark.parametrize("read", _SALE_READS)
def test_every_sale_read_uses_confirmed_sales_in_the_kyiv_window(capture, read):
    read()
    sale_queries = [(sql, params) for sql, params in capture.calls if "dbo.Sale " in sql]
    assert sale_queries
    for sql, params in sale_queries:
        assert sql.count("Deleted = 0 AND") >= 2, sql
        for alias in ("s", "s2", "s3"):
            if f"dbo.Sale {alias} " in sql:
                assert f"{alias}.Deleted = 0 AND {alias}.ChangedToInvoice IS NOT NULL" in sql
                assert f"{alias}.ChangedToInvoice < :window_end" in sql
                assert f"{alias}.Created" not in sql
        assert "Deleted = 0 AND oi.IsValidForCurrentSale = 1 AND oi.Qty > 0" in sql or (
            "oi2.Deleted = 0 AND oi2.IsValidForCurrentSale = 1 AND oi2.Qty > 0" in sql
        )
        assert "DATEADD(month" not in sql
        assert params["window_end"] == datetime(2026, 11, 12, 22, 0)
        assert params["history_start"] == datetime(2024, 12, 31, 22, 0)


def test_twelve_month_turnover_starts_at_kyiv_midnight(capture):
    repo.turnover_eur(1, "2026-11-13", 12, "2026-11-13")
    assert capture.calls[0][1]["window_start"] == datetime(2025, 11, 12, 22, 0)


def test_monthly_series_buckets_kyiv_months_from_the_first_full_month(capture):
    capture.rows = [{"period": "2026-07", "turnover_eur": Decimal("15.919298")}]
    series = repo.monthly_turnover_series(632426, "2026-11-13", 12, "2026-11-13")
    sql, params = capture.calls[0]
    assert "AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time'" in sql
    assert "FORMAT(s.Created" not in sql
    assert params["window_start"] == datetime(2025, 11, 30, 22, 0)
    assert series[0]["period"] == "2025-12" and series[-1]["period"] == "2026-11"
    assert {"period": "2026-07", "turnover_eur": Decimal("15.92")} in series


def test_sale_document_count_ignores_deleted_documents(capture):
    capture.rows = [{"n": 3}]
    assert repo.total_sales_count(1, "2026-11-13", 12) == 3
    sql, _ = capture.calls[0]
    assert "s.Deleted = 0" in sql and "s.Created < :window_end" in sql


_DEBT_READS = [
    lambda: repo.debt_aging_buckets_truth(1, "2026-11-13", "2026-11-13"),
    lambda: repo.overdue_amount_eur(1, "2026-11-13", "2026-11-13"),
    lambda: repo.limit_utilization_basis(1, "2026-11-13"),
    lambda: dataset.feat_debt_aging("2026-11-13"),
    lambda: dataset.feat_debt_trajectory("2026-11-13"),
    lambda: dataset.features_one(1, "2026-11-13", 12),
    lambda: dataset.features_many([1, 2], "2026-11-13", 12),
]


@pytest.mark.parametrize("read", _DEBT_READS)
def test_settled_lines_and_rounding_residues_are_not_open_debt(capture, read):
    read()
    debt_queries = [sql for sql, _ in capture.calls if "dbo.ClientInDebt" in sql]
    assert debt_queries
    for sql in debt_queries:
        assert "d.Deleted = 0 AND d.Total >= 0.5" in sql, sql


def test_donut_slices_partition_the_window_sales(capture):
    capture.rows = [{"settled": 7, "current_sales": 1, "overdue_sales": 2,
                     "current_eur": Decimal("5"), "overdue_eur": Decimal("7.005"),
                     "window_lines": 3, "lines": 9}]
    donut = repo.debt_exposure_donut_truth(1, "2026-11-13", 12, "2026-11-13")
    sales_sql, sales_params = capture.calls[0]
    assert "d.Deleted = 0 AND d.Total >= 0.5" in sales_sql
    assert "cid.SaleID = s.ID" in sales_sql
    assert "s.ChangedToInvoice >= :window_start" in sales_sql
    assert "s.ChangedToInvoice < :window_end" in sales_sql
    assert "s.Deleted = 0 AND s.ChangedToInvoice IS NOT NULL" in sales_sql
    assert {"window_start", "window_end", "history_start"} <= set(sales_params)
    assert donut == {"settled": 7, "current": 1, "overdue": 2,
                     "current_eur": Decimal("5.00"), "overdue_eur": Decimal("7.01"),
                     "outside_window_lines": 6}


def test_debt_reads_skip_currencies_the_fx_function_leaves_unconverted(capture):
    capture.rows = []
    repo.debt_aging_buckets_truth(1, "2026-11-13", "2026-11-13")
    repo.overdue_amount_eur(1, "2026-11-13", "2026-11-13")
    dataset.features_many([1], "2026-11-13", 12)
    guard = ("dbo.GetExchangedToEuroValue(1000000, probe.ID, {0}) <> 1000000)"
             " fx ON fx.ID = a.CurrencyID")
    debt_reads = [sql for sql, _ in capture.calls if "dbo.ClientInDebt" in sql]
    assert len(debt_reads) >= 4
    for sql in debt_reads:
        assert guard.format(":fxdate") in sql or guard.format(":fd") in sql


def test_unconverted_debt_is_reported_in_its_own_currency(capture):
    capture.rows = [{"client_id": 5, "currency_id": 77, "currency": "CZK", "lines": 2,
                     "amount": Decimal("2500.005")}]
    report = _real_unconverted_debt([5], "2026-11-13", "2026-11-13")
    assert report == {5: [{"currency_id": 77, "currency": "CZK", "lines": 2,
                           "amount": Decimal("2500.01")}]}
    assert "NOT EXISTS" in capture.calls[0][0]


def test_risk_90d_carries_all_three_amounts_whatever_the_band():
    from app.services.solvency.service import _risk_90d
    features = {"overdue_eur_91_180": 150.0, "overdue_eur_180plus": 50.0,
                "overdue_eur_1_30": 30.0, "overdue_eur_31_60": 20.0, "overdue_eur_61_90": 10.0,
                "total_debt_eur": 400.0}
    critical = _risk_90d(features)
    assert (critical.band.value, critical.exposure_eur) == ("critical", 200.0)
    assert (critical.overdue_91_plus_eur, critical.overdue_1_90_eur, critical.total_debt_eur) == (
        200.0, 60.0, 400.0)
    medium = _risk_90d({**features, "overdue_eur_91_180": 0.0, "overdue_eur_180plus": 0.0})
    assert (medium.band.value, medium.exposure_eur) == ("medium", 400.0)
    assert (medium.overdue_91_plus_eur, medium.overdue_1_90_eur, medium.total_debt_eur) == (
        0.0, 60.0, 400.0)


def test_client_with_only_a_settled_line_has_no_open_debt_features(capture):
    capture.rows = []
    features = dataset.features_many([631091], "2026-11-13", 12)[631091]
    assert features["n_open_debt_lines"] == 0.0
    assert features["max_overdue_days"] == 0.0
    assert features["months_with_debt_last12"] == 0.0
