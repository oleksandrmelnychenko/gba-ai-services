"""The one definition of sales history: gba-server's confirmed sales net of buyer returns.

A confirmed sale is the fact of NativeSalesNetReportProjectionRepository and its siblings: a
non-deleted, invoiced Sale that is not an accounting carrier, with non-deleted valid lines of
positive quantity, dated by its invoice instant (Sale.ChangedToInvoice, stored in UTC). A buyer
return dated before the cut-off reduces the line it returns, in that line's own month and for
that line's client, never below zero. Months are Europe/Kyiv calendar months; windows are whole
Kyiv days passed as UTC instants.

A return of a sale older than the imported history has no line to reduce: the 1C import anchors
it to a placeholder line (import_placeholder_line, the shape and caption the sync writes). Such a
return is a negative fact of its own: minus the recorded return amount (SaleReturnItem.Amount,
EUR, the server's valuation), at SaleReturn.FromDate, for the client of the return's agreement
and the returned product. A month of a series never goes below zero (monthly_net_eur); what a
month could not absorb is dropped and counted by dropped_placeholder_eur.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

BUSINESS_ZONE = ZoneInfo("Europe/Kyiv")
SOURCE_LABEL = (
    "gba-server confirmed sale (dbo.Sale.ChangedToInvoice, Europe/Kyiv months) "
    "net of buyer returns on the returned line; a return anchored to an import placeholder "
    "is a negative fact of its own month at the recorded return amount"
)
SALE_INSTANT = "s.ChangedToInvoice"
RETURN_INSTANT = "sr.FromDate"
RETURN_AMOUNT = "CAST(sri.Amount AS decimal(28, 14))"
IMPORT_PLACEHOLDER_CAPTION = "1С"
ZONE_PARAM = "zone_change_"

_EUR = "CAST({qty} AS decimal(18, 8)) * CAST({price} AS decimal(28, 14))"
_SOURCE_WINDOW = "{instant} >= :source_history_start AND {instant} < :asof"
_WINDOW = "{instant} >= :source_history_start AND {instant} >= :history_start AND {instant} < :asof"


def confirmed_sale(alias: str = "s") -> str:
    return (
        f"{alias}.Deleted = 0 AND {alias}.ChangedToInvoice IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM dbo.DataSyncAccountingSourceTarget carrier "
        f"WHERE carrier.TargetEntityType = N'Sale' AND carrier.TargetEntityID = {alias}.ID "
        f"AND carrier.IsCurrent = 1 AND carrier.OwnershipType = 1)"
    )


def confirmed_line(alias: str = "oi") -> str:
    return f"{alias}.Deleted = 0 AND {alias}.IsValidForCurrentSale = 1 AND {alias}.Qty > 0"


def confirmed_return(item: str = "sri", document: str = "sr") -> str:
    return f"{item}.Deleted = 0 AND {document}.Deleted = 0 AND {document}.IsCanceled = 0"


def import_placeholder_line(alias: str = "oi") -> str:
    return (
        f"{alias}.Qty = 0 AND {alias}.PricePerItem = 0 AND {alias}.PricePerItemWithoutVat = 0 "
        f"AND {alias}.ExchangeRateAmount = 0 "
        f"AND LTRIM(RTRIM({alias}.Comment)) = N'{IMPORT_PLACEHOLDER_CAPTION}'"
    )


def placeholder_return(item: str = "sri", document: str = "sr", line: str = "oi") -> str:
    return (
        f"{confirmed_return(item, document)} AND {item}.Qty > 0 "
        f"AND {document}.SourceDocumentID IS NOT NULL AND {import_placeholder_line(line)}"
    )


def window(instant: str, whole_source: bool = False) -> str:
    return (_SOURCE_WINDOW if whole_source else _WINDOW).format(instant=instant)


def _offset_minutes(instant: datetime) -> int:
    offset = instant.replace(tzinfo=UTC).astimezone(BUSINESS_ZONE).utcoffset()
    return int(offset.total_seconds() // 60)


@lru_cache(maxsize=64)
def zone_offsets(start: datetime, end: datetime) -> tuple[tuple[int, ...], tuple[datetime, ...]]:
    cursor = start.replace(minute=0, second=0, microsecond=0)
    current = _offset_minutes(cursor)
    offsets = [current]
    changes: list[datetime] = []
    while cursor < end:
        next_day = cursor + timedelta(days=1)
        if _offset_minutes(next_day) == current:
            cursor = next_day
            continue
        while _offset_minutes(cursor + timedelta(hours=1)) == current:
            cursor += timedelta(hours=1)
        cursor += timedelta(hours=1)
        current = _offset_minutes(cursor)
        if cursor < end:
            offsets.append(current)
            changes.append(cursor)
    return tuple(offsets), tuple(changes)


def zone_params(start: datetime, end: datetime) -> dict[str, datetime]:
    _, changes = zone_offsets(start, end)
    return {f"{ZONE_PARAM}{index}": change for index, change in enumerate(changes)}


def local_month(column: str, start: datetime, end: datetime) -> str:
    offsets, changes = zone_offsets(start, end)
    if not changes:
        shift = str(offsets[0])
    else:
        branches = " ".join(
            f"WHEN {column} < :{ZONE_PARAM}{index} THEN {offset}"
            for index, offset in enumerate(offsets[:-1])
        )
        shift = f"CASE {branches} ELSE {offsets[-1]} END"
    return f"CONVERT(char(7), DATEADD(minute, {shift}, {column}), 126)"


def utc_instant(local_day: date | datetime | str) -> datetime:
    if isinstance(local_day, datetime):
        local_day = local_day.date()
    elif isinstance(local_day, str):
        local_day = date.fromisoformat(local_day[:10])
    local_midnight = datetime.combine(local_day, time.min, tzinfo=BUSINESS_ZONE)
    return local_midnight.astimezone(UTC).replace(tzinfo=None)


def sale_lines(whole_source: bool = False) -> str:
    sold = "CAST(oi.Qty AS decimal(38, 6))"
    kept = f"CASE WHEN {sold} > ISNULL(returned.qty, 0) THEN {sold} - ISNULL(returned.qty, 0) ELSE 0 END"
    return f"""
        SELECT oi.ID AS fact_id,
               ca.ClientID AS client_id,
               oi.ProductID AS product_id,
               {SALE_INSTANT} AS fact_at,
               {kept} AS qty,
               ISNULL(returned.qty, 0) AS returned_qty,
               oi.PricePerItem AS price,
               {_EUR.format(qty=kept, price="oi.PricePerItem")} AS eur,
               oi.Updated AS line_updated,
               s.Updated AS document_updated,
               returned.updated AS return_updated,
               0 AS placeholder_return
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        LEFT JOIN (
            SELECT sri.OrderItemID AS line_id,
                   SUM(CAST(sri.Qty AS decimal(38, 6))) AS qty,
                   MAX(CASE WHEN sri.Updated > sr.Updated THEN sri.Updated ELSE sr.Updated END)
                       AS updated
            FROM dbo.SaleReturnItem sri
            JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
            WHERE {confirmed_return("sri", "sr")}
                  AND sri.Qty > 0
                  AND {RETURN_INSTANT} < :asof
            GROUP BY sri.OrderItemID
        ) returned ON returned.line_id = oi.ID
        WHERE {confirmed_sale("s")}
              AND {confirmed_line("oi")}
              AND {window(SALE_INSTANT, whole_source)}"""


def placeholder_return_facts(whole_source: bool = False) -> str:
    returned = "CAST(sri.Qty AS decimal(38, 6))"
    return f"""
        SELECT sri.ID AS fact_id,
               ca.ClientID AS client_id,
               oi.ProductID AS product_id,
               {RETURN_INSTANT} AS fact_at,
               -{returned} AS qty,
               {returned} AS returned_qty,
               oi.PricePerItem AS price,
               -{RETURN_AMOUNT} AS eur,
               sri.Updated AS line_updated,
               sr.Updated AS document_updated,
               CASE WHEN sri.Updated > sr.Updated THEN sri.Updated ELSE sr.Updated END
                   AS return_updated,
               1 AS placeholder_return
        FROM dbo.SaleReturnItem sri
        JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
        JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
        JOIN dbo.ClientAgreement ca ON ca.ID = sr.ClientAgreementID
        WHERE {placeholder_return("sri", "sr", "oi")}
              AND oi.ProductID IS NOT NULL
              AND {window(RETURN_INSTANT, whole_source)}"""


def net_sales_facts(whole_source: bool = False) -> str:
    return f"""({sale_lines(whole_source)}
        UNION ALL{placeholder_return_facts(whole_source)}
    ) f"""


SALE_FACT = "f.placeholder_return = 0"


def monthly_net_eur() -> str:
    return "CASE WHEN SUM(f.eur) > 0 THEN SUM(f.eur) ELSE 0 END"


def placeholder_eur() -> str:
    return "-SUM(CASE WHEN f.placeholder_return = 1 THEN f.eur ELSE 0 END)"


def dropped_placeholder_eur() -> str:
    return "CASE WHEN SUM(f.eur) < 0 THEN -SUM(f.eur) ELSE 0 END"


def fact_month(start: datetime, end: datetime) -> str:
    return local_month("f.fact_at", start, end)
