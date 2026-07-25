from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.data import signals_repository as sig


def test_synthetic_product_status_distinguishes_verified_row_from_fallback(monkeypatch):
    monkeypatch.setattr(sig, "_synthetic_cached", None)
    monkeypatch.setattr(sig, "query", lambda sql, params=None: [{"id": 123}])

    assert sig.synthetic_product_status() == {
        "product_id": 123,
        "resolved": True,
        "source": "verified_database",
    }
    assert sig.synthetic_product_id() == 123


def test_synthetic_product_status_fails_closed_when_lookup_fails(monkeypatch):
    monkeypatch.setattr(sig, "_synthetic_cached", None)
    monkeypatch.setattr(
        sig,
        "query",
        lambda sql, params=None: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    assert sig.synthetic_product_status() == {
        "product_id": sig._SYNTHETIC_FALLBACK_ID,
        "resolved": False,
        "source": "unverified_fallback",
    }


def test_sales_source_status_fails_closed_when_schema_is_missing(monkeypatch):
    calls: list[str] = []

    def fake_query(sql: str, params=None):
        calls.append(sql)
        return [{"source_schema_present": 0}]

    monkeypatch.setattr(sig, "query", fake_query)

    result = sig.sales_source_status(datetime(2026, 7, 25), 24)

    assert len(calls) == 1
    assert result == {
        "source_schema_present": False,
        "canonical_row_count": 0,
        "history_row_count": 0,
        "history_product_count": 0,
        "history_client_count": 0,
        "latest_sale_at": None,
        "invalid_value_row_count": 0,
    }


def test_sales_source_status_normalizes_exact_integer_counts(monkeypatch):
    latest = datetime(2026, 7, 24, 10, 30)
    responses = iter(
        [
            [{"source_schema_present": 1}],
            [
                {
                    "canonical_row_count": Decimal("100"),
                    "history_row_count": 90,
                    "history_product_count": 12,
                    "history_client_count": 8,
                    "latest_sale_at": latest,
                    "invalid_value_row_count": None,
                }
            ],
        ]
    )
    monkeypatch.setattr(sig, "query", lambda sql, params=None: next(responses))
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    result = sig.sales_source_status(datetime(2026, 7, 25), 24)

    assert result["canonical_row_count"] == 100
    assert result["history_row_count"] == 90
    assert result["latest_sale_at"] == latest
    assert result["invalid_value_row_count"] == 0


def test_sales_source_status_uses_same_floor_and_effective_window_params(monkeypatch):
    captured: list[dict] = []

    def fake_query(sql: str, params=None):
        if sql == sig._SALES_SOURCE_SCHEMA_SQL:
            return [{"source_schema_present": 1}]
        captured.append(params)
        return []

    as_of = datetime(2026, 7, 25, 12, 30)
    monkeypatch.setattr(sig, "query", fake_query)
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    sig.sales_source_status(as_of, 24)

    assert captured == [
        {
            "asof": as_of,
            "source_history_start": "2025-01-01",
            "history_start": "2025-01-01",
            "synth": 999,
        }
    ]


def test_history_summary_keeps_decimal_precision_until_money_boundary():
    summary = sig.history_summary(
        [
            {"ym": "2026-05", "eur": Decimal("10.004")},
            {"ym": "2026-06", "eur": Decimal("20.001")},
        ]
    )

    assert summary == {
        "month_count": 2,
        "non_zero_month_count": 2,
        "total_eur": Decimal("30.005"),
    }


def test_history_summary_rejects_duplicate_months():
    with pytest.raises(ValueError, match="duplicate month 2026-06"):
        sig.history_summary(
            [
                {"ym": "2026-06", "eur": Decimal("10")},
                {"ym": "2026-06", "eur": Decimal("20")},
            ]
        )


def test_history_summary_rejects_rows_beyond_calendar_window():
    with pytest.raises(ValueError, match="exceeds the configured"):
        sig.history_summary(
            [
                {"ym": "2026-05", "eur": Decimal("10")},
                {"ym": "2026-06", "eur": Decimal("20")},
            ],
            max_months=1,
        )


def test_calendar_window_enforces_source_and_effective_parameter_boundaries():
    assert "o.Created >= :source_history_start" in sig._WINDOW
    assert "o.Created >= :history_start" in sig._WINDOW
    assert "o.Created < :asof" in sig._WINDOW
    assert "DATEADD" not in sig._WINDOW


def test_monthly_query_clamps_sql_params_to_source_floor(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sig,
        "query",
        lambda sql, params=None: captured.append((sql, params)) or [],
    )
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    sig.monthly_sales_by_client(11, "2026-07-25", 24)

    assert captured[0][1] == {
        "asof": "2026-07-25",
        "source_history_start": "2025-01-01",
        "history_start": "2025-01-01",
        "cid": 11,
        "synth": 999,
    }


def test_monthly_query_uses_requested_start_once_full_window_is_available(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(
        sig,
        "query",
        lambda sql, params=None: captured.append(params) or [],
    )

    sig.monthly_sales_by_product(22, "2027-07-25", 24)

    assert captured[0]["source_history_start"] == "2025-01-01"
    assert captured[0]["history_start"] == "2025-08-01"


def test_forecast_source_fingerprint_is_scoped_and_money_sensitive(monkeypatch):
    captured: list[tuple[str, dict]] = []
    amount = Decimal("30.005")

    def fake_query(sql: str, params=None):
        captured.append((sql, params))
        return [
            {
                "row_count": 3,
                "max_item_id": 33,
                "max_item_updated": datetime(2026, 7, 24, 10),
                "max_order_updated": datetime(2026, 7, 24, 11),
                "max_order_created": datetime(2026, 7, 23, 12),
                "quantity_sum": Decimal("6"),
                "amount_sum": amount,
                "row_checksum": 12345,
            }
        ]

    monkeypatch.setattr(sig, "query", fake_query)
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    first = sig.forecast_source_fingerprint(11, 22, "2026-07-25", 24)
    amount = Decimal("30.006")
    second = sig.forecast_source_fingerprint(11, 22, "2026-07-25", 24)

    assert first != second
    assert len(first) == len(second) == 24
    assert "ca.ClientID = :cid OR oi.ProductID = :pid" in captured[0][0]
    assert captured[0][1] == {
        "asof": "2026-07-25",
        "source_history_start": "2025-01-01",
        "history_start": "2025-01-01",
        "synth": 999,
        "cid": 11,
        "pid": 22,
    }


def test_forecast_source_fingerprint_no_scope_never_reads_db(monkeypatch):
    monkeypatch.setattr(
        sig,
        "query",
        lambda *args: (_ for _ in ()).throw(AssertionError("no scope has no factual rows")),
    )

    first = sig.forecast_source_fingerprint(None, None, "2026-07-25", 24)
    monkeypatch.setattr(sig.get_settings(), "source_history_start_date", date(2025, 2, 1))
    second = sig.forecast_source_fingerprint(None, None, "2026-07-25", 24)

    assert len(first) == len(second) == 24
    assert first != second


@pytest.mark.parametrize("value", [Decimal("-0.01"), Decimal("NaN"), Decimal("Infinity")])
def test_history_summary_rejects_invalid_money(value):
    with pytest.raises(ValueError, match="non-finite or negative"):
        sig.history_summary([{"ym": "2026-06", "eur": value}])
