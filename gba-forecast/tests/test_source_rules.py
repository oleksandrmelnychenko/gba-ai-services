from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from app.data import signals_repository as sig
from app.data import source_rules as rules


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def test_confirmed_sale_is_live_invoiced_and_not_an_accounting_carrier():
    predicate = rules.confirmed_sale("x")

    assert "x.Deleted = 0" in predicate
    assert "x.ChangedToInvoice IS NOT NULL" in predicate
    assert "NOT EXISTS" in predicate
    assert "carrier.TargetEntityType = N'Sale'" in predicate
    assert "carrier.TargetEntityID = x.ID" in predicate
    assert "carrier.IsCurrent = 1 AND carrier.OwnershipType = 1" in predicate


def test_confirmed_line_is_live_valid_and_positive():
    assert rules.confirmed_line("l") == (
        "l.Deleted = 0 AND l.IsValidForCurrentSale = 1 AND l.Qty > 0"
    )


def test_confirmed_return_excludes_deleted_and_cancelled_documents():
    assert rules.confirmed_return("i", "d") == (
        "i.Deleted = 0 AND d.Deleted = 0 AND d.IsCanceled = 0"
    )


def test_sale_lines_date_sales_by_invoice_and_net_returns_on_the_returned_line():
    lines = _normalized(rules.sale_lines())

    assert "UNION ALL" not in lines
    assert "s.ChangedToInvoice AS fact_at" in lines
    assert "JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID" in lines
    assert rules.confirmed_sale("s") in lines
    assert rules.confirmed_line("oi") in lines
    assert "o.Created" not in lines
    assert rules.confirmed_return("sri", "sr") in lines
    assert "GROUP BY sri.OrderItemID ) returned ON returned.line_id = oi.ID" in lines
    assert "sr.FromDate < :asof" in lines
    assert "sr.FromDate >=" not in lines
    assert "sr.ClientAgreementID" not in lines
    assert "sri.Amount" not in lines
    assert "0 AS placeholder_return" in lines


def test_a_line_returned_in_full_or_more_counts_as_zero():
    lines = _normalized(rules.sale_lines())
    sold = "CAST(oi.Qty AS decimal(38, 6))"
    kept = (
        f"CASE WHEN {sold} > ISNULL(returned.qty, 0) "
        f"THEN {sold} - ISNULL(returned.qty, 0) ELSE 0 END"
    )

    assert f"{kept} AS qty" in lines
    assert f"CAST({kept} AS decimal(18, 8)) * CAST(oi.PricePerItem AS decimal(28, 14)) AS eur" in lines


def test_whole_source_window_drops_only_the_rolling_start():
    rolling = rules.sale_lines()
    whole = rules.sale_lines(whole_source=True)

    assert ":history_start" in rolling.replace(":source_history_start", "")
    assert ":history_start" not in whole.replace(":source_history_start", "")
    assert whole.count(":source_history_start") == 1
    assert whole.count(":asof") == 2

    returns = rules.placeholder_return_facts(whole_source=True)
    assert ":history_start" in rules.placeholder_return_facts().replace(":source_history_start", "")
    assert ":history_start" not in returns.replace(":source_history_start", "")
    assert returns.count(":source_history_start") == 1
    assert returns.count(":asof") == 1


def test_import_placeholder_is_the_line_the_sync_writes():
    assert rules.import_placeholder_line("l") == (
        "l.Qty = 0 AND l.PricePerItem = 0 AND l.PricePerItemWithoutVat = 0 "
        "AND l.ExchangeRateAmount = 0 AND LTRIM(RTRIM(l.Comment)) = N'1С'"
    )
    assert rules.IMPORT_PLACEHOLDER_CAPTION == "1\u0421"
    assert rules.placeholder_return("i", "d", "l") == (
        "i.Deleted = 0 AND d.Deleted = 0 AND d.IsCanceled = 0 AND i.Qty > 0 "
        "AND d.SourceDocumentID IS NOT NULL AND " + rules.import_placeholder_line("l")
    )


def test_a_placeholder_return_is_a_negative_fact_of_its_own_date_client_and_amount():
    returns = _normalized(rules.placeholder_return_facts())

    assert "sr.FromDate AS fact_at" in returns
    assert "JOIN dbo.ClientAgreement ca ON ca.ID = sr.ClientAgreementID" in returns
    assert "JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID" in returns
    assert "oi.ProductID AS product_id" in returns
    assert "-CAST(sri.Amount AS decimal(28, 14)) AS eur" in returns
    assert "1 AS placeholder_return" in returns
    assert rules.placeholder_return("sri", "sr", "oi") in returns
    assert rules.window("sr.FromDate") in returns
    assert "dbo.Sale " not in returns
    assert "PricePerItem AS decimal" not in returns


def test_facts_are_the_sale_lines_plus_the_placeholder_returns():
    for whole in (False, True):
        assert rules.net_sales_facts(whole) == (
            f"({rules.sale_lines(whole)}\n        UNION ALL"
            f"{rules.placeholder_return_facts(whole)}\n    ) f"
        )


def test_a_month_never_goes_below_zero_and_the_dropped_part_is_counted():
    assert rules.monthly_net_eur() == "CASE WHEN SUM(f.eur) > 0 THEN SUM(f.eur) ELSE 0 END"
    assert rules.dropped_placeholder_eur() == "CASE WHEN SUM(f.eur) < 0 THEN -SUM(f.eur) ELSE 0 END"
    assert rules.placeholder_eur() == (
        "-SUM(CASE WHEN f.placeholder_return = 1 THEN f.eur ELSE 0 END)"
    )


def _kyiv_month_by_offsets(instant: datetime, start: datetime, end: datetime) -> str:
    offsets, changes = rules.zone_offsets(start, end)
    index = sum(1 for change in changes if instant >= change)
    return (instant + timedelta(minutes=offsets[index])).strftime("%Y-%m")


def test_month_offsets_agree_with_the_kyiv_zone_for_every_hour_of_the_window():
    start, end = datetime(2024, 12, 31, 22, 0), datetime(2027, 7, 24, 21, 0)
    offsets, changes = rules.zone_offsets(start, end)

    assert offsets == (120, 180, 120, 180, 120, 180)
    assert changes == (
        datetime(2025, 3, 30, 1, 0),
        datetime(2025, 10, 26, 1, 0),
        datetime(2026, 3, 29, 1, 0),
        datetime(2026, 10, 25, 1, 0),
        datetime(2027, 3, 28, 1, 0),
    )
    instant = start
    while instant < end:
        local = instant.replace(tzinfo=UTC).astimezone(rules.BUSINESS_ZONE)
        assert _kyiv_month_by_offsets(instant, start, end) == f"{local:%Y-%m}"
        instant += timedelta(minutes=30)


def test_month_expression_shifts_by_the_offset_in_force_without_time_zone_conversion():
    start, end = datetime(2025, 12, 31, 22, 0), datetime(2026, 7, 24, 21, 0)
    month = rules.local_month("f.fact_at", start, end)

    assert month == (
        "CONVERT(char(7), DATEADD(minute, CASE WHEN f.fact_at < :zone_change_0 THEN 120 "
        "ELSE 180 END, f.fact_at), 126)"
    )
    assert rules.zone_params(start, end) == {"zone_change_0": datetime(2026, 3, 29, 1, 0)}
    assert "AT TIME ZONE" not in month


def test_month_expression_is_a_constant_shift_when_the_window_has_no_clock_change():
    start, end = datetime(2026, 3, 31, 21, 0), datetime(2026, 7, 24, 21, 0)

    assert rules.local_month("f.fact_at", start, end) == (
        "CONVERT(char(7), DATEADD(minute, 180, f.fact_at), 126)"
    )
    assert rules.zone_params(start, end) == {}


def test_window_bounds_are_kyiv_midnights_as_utc_instants():
    assert rules.utc_instant("2026-07-25") == datetime(2026, 7, 24, 21, 0)
    assert rules.utc_instant(date(2026, 1, 1)) == datetime(2025, 12, 31, 22, 0)
    assert rules.utc_instant(datetime(2026, 3, 29, 12, 30)) == datetime(2026, 3, 28, 22, 0)
    assert rules.utc_instant(date(2026, 3, 30)) == datetime(2026, 3, 29, 21, 0)
    assert rules.utc_instant(date(2026, 10, 26)) == datetime(2026, 10, 25, 22, 0)


def test_every_series_query_reads_the_shared_facts(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        sig, "query", lambda sql, params=None: captured.append((sql, params)) or []
    )
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    sig.monthly_sales_by_client(1, "2026-07-25", 24)
    sig.monthly_sales_by_product(2, "2026-07-25", 24)
    sig.monthly_sales_by_client_and_product(1, 2, "2026-07-25", 24)
    sig.sample_client_monthly_series("2026-07-25", 24, 5)
    sig.sample_product_monthly_series("2026-07-25", 24, 5)
    sig.all_client_monthly_series("2026-07-25", 24)
    sig.all_product_monthly_series("2026-07-25", 24)
    sig.forecast_source_fingerprint(1, 2, "2026-07-25", 24)

    assert len(captured) == 8
    for sql, _params in captured:
        assert rules.net_sales_facts() in sql
        assert "o.Created" not in sql
    for sql, params in captured[:7]:
        assert rules.monthly_net_eur() in sql
        assert rules.fact_month(params["source_history_start"], params["asof"]) in sql
        assert params["zone_change_0"] == datetime(2025, 3, 30, 1, 0)
        assert params["zone_change_2"] == datetime(2026, 3, 29, 1, 0)


def test_source_status_counts_sales_over_the_whole_source_window():
    assert rules.net_sales_facts(whole_source=True) in sig._SALES_SOURCE_STATUS_SQL
    assert rules.SALE_FACT == "f.placeholder_return = 0"
    assert f"AND {rules.SALE_FACT}" in sig._SALES_SOURCE_STATUS_SQL


def test_placeholder_diagnostic_reports_what_each_series_dropped(monkeypatch):
    captured: list[str] = []
    answers = iter(
        [
            [{"line_count": 3, "client_count": 2, "product_count": 3, "return_eur": Decimal("95.5")}],
            [{"month_count": 2, "return_eur": Decimal("95.5"), "dropped_eur": Decimal("0"),
              "dropped_month_count": 0}],
            [{"month_count": 3, "return_eur": Decimal("95.5"), "dropped_eur": Decimal("40.25"),
              "dropped_month_count": 1}],
            [],
        ]
    )
    monkeypatch.setattr(
        sig, "query", lambda sql, params=None: captured.append(sql) or next(answers)
    )
    monkeypatch.setattr(sig, "synthetic_product_id", lambda: 999)

    outcome = sig.placeholder_return_diagnostic("2026-07-25", 24)

    assert outcome == {
        "line_count": 3,
        "client_count": 2,
        "product_count": 3,
        "return_eur": Decimal("95.5"),
        "by_client": {"month_count": 2, "absorbed_eur": Decimal("95.5"),
                      "dropped_eur": Decimal("0"), "dropped_month_count": 0},
        "by_product": {"month_count": 3, "absorbed_eur": Decimal("55.25"),
                       "dropped_eur": Decimal("40.25"), "dropped_month_count": 1},
        "by_client_and_product": {"month_count": 0, "absorbed_eur": Decimal("0"),
                                  "dropped_eur": Decimal("0"), "dropped_month_count": 0},
    }
    assert len(captured) == 4
    for sql in captured:
        assert rules.net_sales_facts() in sql
    for sql in captured[1:]:
        assert rules.dropped_placeholder_eur() in sql
        assert "HAVING MAX(f.placeholder_return) = 1" in sql


def test_cache_namespace_was_bumped_with_the_definition():
    from app.data import cache

    assert cache._VER == "v7-placeholder-returns"
