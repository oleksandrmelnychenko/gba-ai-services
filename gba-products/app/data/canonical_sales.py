"""Confirmed gross sale and independently dated return facts from native report SQL.

Generated currency proof is never replaced with original sale price or a current FX rate.
Raw additive values stay Decimal until the JSON display boundary.
"""
from decimal import Decimal, localcontext
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from app.core import exact_numbers as exact

ARTIFACTS = Path(__file__).with_name("canonical_sql")
CONTRACT = "native-confirmed-gross-and-active-returns-with-vat-v1"


@lru_cache
def fragment(name: str) -> str:
    manifest = json.loads((ARTIFACTS / "manifest.json").read_text())
    content = (ARTIFACTS / name).read_text()
    if manifest["contract"] != CONTRACT or hashlib.sha256(content.encode()).hexdigest() != manifest["artifacts"][name]:
        raise RuntimeError("canonical_sales_sql_artifact_mismatch")
    return content.strip()


def facts_cte(*, returns: bool = False) -> str:
    amount = fragment("decimal_product.sql").format(price="oi.PricePerItem", quantity="oi.Qty")
    gross = f"""WITH regionCodes AS (
        SELECT ID, CASE WHEN UPPER(NULLIF(LTRIM(RTRIM(Name)), N''))=N'200' THEN N'KR'
                        ELSE UPPER(NULLIF(LTRIM(RTRIM(Name)), N'')) END AS Name
        FROM dbo.Region
    ), regionLookup AS (
        SELECT ID, Name, CASE WHEN Name IS NULL THEN NULL ELSE MIN(ID) OVER(PARTITION BY Name) END AS CanonicalID
        FROM regionCodes
    ), salesFacts AS (
        SELECT oi.ProductID AS product_id, s.ID AS sale_id, o.ID AS order_id,
               ca.ClientID AS client_id, region.CanonicalID AS region_id, region.Name AS region_name,
               s.EffectiveDate AS event_utc,
               CAST(s.EffectiveDate AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS datetime2) AS event_local,
               CAST(oi.Qty AS decimal(30,14)) AS sold_qty,
               {amount} AS revenue_eur,
               CAST(CASE WHEN {amount} IS NULL THEN 1 ELSE 0 END AS bigint) AS arithmetic_failures
        {fragment('sales_from.sql')}
        LEFT JOIN dbo.Client cl ON cl.ID = ca.ClientID
        LEFT JOIN regionLookup region ON region.ID = cl.RegionID
        {fragment('sales_where.sql')}
          AND s.EffectiveDate >= :history_start AND s.EffectiveDate < :asof
    )"""
    if not returns:
        return gross
    currency = fragment("return_currency.sql").replace("@FromUtc", ":history_start").replace("@ToUtc", ":asof")
    return gross + currency + f""", returnFacts AS (
        SELECT oi.ProductID AS product_id, s.ID AS sale_id, oi.OrderID AS order_id,
               ca.ClientID AS client_id, region.CanonicalID AS region_id, region.Name AS region_name,
               sr.FromDate AS event_utc, calendar.LocalDate AS event_local,
               CAST(sri.Qty AS decimal(28,8)) AS returned_qty,
               {fragment('return_value.sql')} AS known_returned_value_eur,
               CAST({fragment('return_unresolved.sql')} AS bigint) AS unresolved_amount_count,
               CAST(CASE WHEN sourceSale.MatchCount <> 1 THEN 1 ELSE 0 END AS bigint) AS ambiguous_lines,
               CAST(CASE WHEN CONVERT(float,CAST(sri.Qty AS decimal(28,8))) <> sri.Qty THEN 1 ELSE 0 END AS bigint) AS arithmetic_failures,
               CAST(CASE WHEN sri.IsMoneyReturned = 1 THEN 1 ELSE 0 END AS bigint) AS money_returned_lines
        {fragment('return_from.sql')}
        LEFT JOIN regionLookup region ON region.ID = cl.RegionID
        WHERE sri.Deleted = 0 AND sr.Deleted = 0 AND sr.IsCanceled = 0
          AND sr.FromDate >= :history_start AND sr.FromDate < :asof
    ), financialFacts AS (
        SELECT product_id, sale_id, order_id, client_id, region_id, region_name, event_utc, event_local,
               sold_qty, CAST(0 AS decimal(28,8)) AS returned_qty, revenue_eur,
               CAST(0 AS decimal(28,14)) AS known_returned_value_eur,
               CAST(0 AS bigint) AS unresolved_amount_count, CAST(0 AS bigint) AS ambiguous_lines,
               arithmetic_failures, CAST(0 AS bigint) AS money_returned_lines, 0 AS is_return
        FROM salesFacts
        UNION ALL
        SELECT product_id, sale_id, order_id, client_id, region_id, region_name, event_utc, event_local,
               CAST(0 AS decimal(30,14)), returned_qty, CAST(0 AS decimal(38,22)), known_returned_value_eur,
               unresolved_amount_count, ambiguous_lines, arithmetic_failures, money_returned_lines, 1
        FROM returnFacts
    )"""


FINANCIAL_SELECT = """
    SUM(revenue_eur) AS revenue_eur,
    SUM(known_returned_value_eur) AS known_returned_value_eur,
    SUM(unresolved_amount_count) AS unresolved_amount_count,
    SUM(arithmetic_failures) AS arithmetic_failures,
    SUM(ambiguous_lines) AS ambiguous_lines
"""


def validate_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        if row.get("arithmetic_failures", 0):
            raise ValueError("canonical_sales_arithmetic_precision_unresolved")
        if row.get("ambiguous_lines", 0):
            raise ValueError("canonical_return_requires_one_confirmed_sale_for_same_agreement")
    return rows


def financial_rows(rows: list[dict]) -> list[dict]:
    validate_rows(rows)
    result = []
    with localcontext() as context:
        context.prec = 50
        for row in rows:
            gross = exact.decimal_value(row.get("revenue_eur"), "confirmed gross revenue")
            returned = exact.decimal_value(row.get("known_returned_value_eur"), "known returned amount")
            unresolved = exact.non_negative_int(row.get("unresolved_amount_count"), "unresolved amount count")
            known_net = gross - returned
            result.append({**row, "gross_revenue_eur": gross,
                           "returned_value_eur": None if unresolved else returned,
                           "net_revenue_eur": None if unresolved else known_net,
                           "known_net_revenue_eur": known_net})
    return result


def money_summary(rows: list[dict]) -> dict:
    with localcontext() as context:
        context.prec = 50
        gross = sum((exact.decimal_value(row["revenue_eur"], "gross revenue") for row in rows), Decimal(0))
        returned = sum((exact.decimal_value(row["known_returned_value_eur"], "known returns") for row in rows), Decimal(0))
        unresolved = sum(exact.non_negative_int(row["unresolved_amount_count"], "unresolved") for row in rows)
        net = gross - returned
        return {"total_revenue_eur": exact.money(gross, non_negative=False),
                "total_gross_revenue_eur": exact.money(gross, non_negative=False),
                "total_returned_value_eur": None if unresolved else exact.money(returned, non_negative=False),
                "known_total_returned_value_eur": exact.money(returned, non_negative=False),
                "total_net_revenue_eur": None if unresolved else exact.money(net, non_negative=False),
                "known_total_net_revenue_eur": exact.money(net, non_negative=False),
                "unresolved_amount_count": unresolved}
