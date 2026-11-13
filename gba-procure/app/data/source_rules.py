"""The one definition of demand, mirroring gba-server's confirmed sale.

A confirmed sale is the native sales reports' fact (NativeSalesNetReportProjectionRepository
and siblings): a non-deleted, invoiced Sale that is not an accounting carrier, with non-deleted
valid lines of positive quantity, dated by its invoice instant (Sale.ChangedToInvoice, stored
in UTC). Demand is what was shipped and kept: a line's quantity minus what the buyer returned
to a sellable storage before the cut-off, never below zero, on the line's own Europe/Kyiv day.
A return to a defective storage does not put the unit back on the shelf, so it stays demand.
Windows are whole Kyiv days passed as UTC instants.

A return of a sale older than the imported history has no line to reduce: the 1C import anchors
it to a placeholder line (import_placeholder_line, the shape and caption the sync writes). Such a
return to a sellable storage is booked on its own Europe/Kyiv day (SaleReturn.FromDate) for the
returned product: it reduces what the lines of that product kept on that day, never below zero.
Units the day could not absorb are dropped, exactly as a line returned beyond its quantity is;
placeholder_return_outcome reports them.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.history import as_date, source_history_start

BUSINESS_ZONE = ZoneInfo("Europe/Kyiv")
SQL_BUSINESS_ZONE = "FLE Standard Time"
SALE_INSTANT = "s.ChangedToInvoice"
RETURN_INSTANT = "sr.FromDate"
IMPORT_PLACEHOLDER_CAPTION = "1С"


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


def sellable_storage(alias: str = "st") -> str:
    return (
        f"{alias}.Deleted = 0 AND {alias}.ForDefective = 0 "
        f"AND ({alias}.AvailableForReSale = 1 OR {alias}.IsResale = 1)"
    )


def local_day(column: str) -> str:
    return (
        f"CAST({column} AT TIME ZONE 'UTC' "
        f"AT TIME ZONE '{SQL_BUSINESS_ZONE}' AS date)"
    )


def utc_instant(local_day_value: date | datetime | str) -> datetime:
    local_midnight = datetime.combine(as_date(local_day_value), time.min, tzinfo=BUSINESS_ZONE)
    return local_midnight.astimezone(UTC).replace(tzinfo=None)


def trailing_window(as_of: date | datetime | str, history_days: int) -> dict[str, datetime]:
    end = as_date(as_of)
    start = max(end - timedelta(days=int(history_days)), source_history_start())
    return {"demand_from": utc_instant(start), "demand_to": utc_instant(end)}


def forward_window(as_of: date | datetime | str, window_days: int) -> dict[str, datetime]:
    start = as_date(as_of)
    return {
        "demand_from": utc_instant(start),
        "demand_to": utc_instant(start + timedelta(days=int(window_days))),
    }


def utc_of_local_day(day: str) -> str:
    return (
        f"CAST(CAST({day} AS datetime2) AT TIME ZONE '{SQL_BUSINESS_ZONE}' "
        f"AT TIME ZONE 'UTC' AS datetime2)"
    )


def sale_lines(marker: str = ":") -> str:
    lower, upper = f"{marker}demand_from", f"{marker}demand_to"
    return f"""
        SELECT oi.ID AS line_id,
               oi.ProductID AS product_id,
               {SALE_INSTANT} AS sold_at,
               {local_day(SALE_INSTANT)} AS sold_day,
               CASE WHEN CAST(oi.Qty AS decimal(38, 6)) > ISNULL(returned.qty, 0)
                    THEN CAST(oi.Qty AS decimal(38, 6)) - ISNULL(returned.qty, 0)
                    ELSE 0 END AS units,
               oi.PricePerItem AS price,
               oi.Updated AS line_updated,
               returned.updated AS return_updated
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        OUTER APPLY (
            SELECT SUM(CAST(sri.Qty AS decimal(38, 6))) AS qty,
                   MAX(sri.Updated) AS updated
            FROM dbo.SaleReturnItem sri
            JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
            JOIN dbo.Storage rst ON rst.ID = sri.StorageID
            WHERE sri.OrderItemID = oi.ID
                  AND {confirmed_return("sri", "sr")}
                  AND sri.Qty > 0
                  AND {RETURN_INSTANT} < {upper}
                  AND {sellable_storage("rst")}
        ) returned
        WHERE {confirmed_sale("s")}
              AND {confirmed_line("oi")}
              AND {SALE_INSTANT} >= {lower}
              AND {SALE_INSTANT} < {upper}"""


def placeholder_returns(marker: str = ":") -> str:
    lower, upper = f"{marker}demand_from", f"{marker}demand_to"
    day = local_day(RETURN_INSTANT)
    return f"""
        SELECT oi.ProductID AS product_id,
               local.returned_day,
               {utc_of_local_day("local.returned_day")} AS day_from,
               {utc_of_local_day("DATEADD(day, 1, local.returned_day)")} AS day_to,
               SUM(CAST(sri.Qty AS decimal(38, 6))) AS units,
               COUNT_BIG(*) AS lines,
               MAX(sri.Updated) AS updated
        FROM dbo.SaleReturnItem sri
        JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
        JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
        JOIN dbo.Storage rst ON rst.ID = sri.StorageID
        CROSS APPLY (SELECT {day} AS returned_day) local
        WHERE {placeholder_return("sri", "sr", "oi")}
              AND oi.ProductID IS NOT NULL
              AND {sellable_storage("rst")}
              AND {RETURN_INSTANT} >= {lower}
              AND {RETURN_INSTANT} < {upper}
        GROUP BY oi.ProductID, local.returned_day"""


def _day_lines(marker: str = ":") -> str:
    return f"""({placeholder_returns(marker)}
        ) g
        JOIN ({sale_lines(marker)}
        ) l ON l.product_id = g.product_id AND l.sold_at >= g.day_from AND l.sold_at < g.day_to"""


def absorbed_placeholder_returns(marker: str = ":") -> str:
    prior = (
        "ISNULL(SUM(l.units) OVER (PARTITION BY g.product_id, g.returned_day "
        "ORDER BY l.sold_at, l.line_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0)"
    )
    return f"""
        SELECT x.line_id,
               CASE WHEN x.returned - x.prior >= x.units THEN x.units
                    WHEN x.returned > x.prior THEN x.returned - x.prior
                    ELSE 0 END AS units,
               x.updated
        FROM (
            SELECT l.line_id, l.units, g.units AS returned, g.updated, {prior} AS prior
            FROM {_day_lines(marker)}
        ) x"""


def demand_lines(marker: str = ":") -> str:
    return f"""(
        SELECT l.line_id, l.product_id, l.sold_at, l.sold_day,
               l.units - ISNULL(absorbed.units, 0) AS units,
               l.price, l.line_updated,
               CASE WHEN absorbed.updated > ISNULL(l.return_updated, absorbed.updated)
                    THEN absorbed.updated
                    ELSE ISNULL(l.return_updated, absorbed.updated) END AS return_updated
        FROM ({sale_lines(marker)}
        ) l
        LEFT JOIN ({absorbed_placeholder_returns(marker)}
        ) absorbed ON absorbed.line_id = l.line_id
    ) d"""


def placeholder_return_outcome(marker: str = ":") -> str:
    kept = "ISNULL(kept.units, 0)"
    return f"""(
        SELECT g.product_id, g.returned_day, g.lines, g.units AS returned_units,
               CASE WHEN {kept} < g.units THEN {kept} ELSE g.units END AS absorbed_units,
               CASE WHEN {kept} < g.units THEN g.units - {kept} ELSE 0 END AS dropped_units
        FROM ({placeholder_returns(marker)}
        ) g
        OUTER APPLY (
            SELECT SUM(l.units) AS units
            FROM ({sale_lines(marker)}
            ) l
            WHERE l.product_id = g.product_id AND l.sold_at >= g.day_from AND l.sold_at < g.day_to
        ) kept
    ) r"""
