"""The one definition of a purchase, mirroring gba-server's confirmed sale.

A purchase is a line of a confirmed sale that the client kept: a non-deleted, invoiced Sale
that is not an accounting carrier, with a non-deleted valid line of positive quantity, dated by
its invoice instant (Sale.ChangedToInvoice, stored in UTC). Buyer returns dated before the
cut-off are netted on the line they return; a line returned in full is not a purchase
(PURCHASED drops it through one uncorrelated set of fully returned lines, which keeps the
per-request queries fast; KEPT_UNITS is the same netting as a quantity).
Cut-offs are whole Europe/Kyiv days passed as UTC instants, and SOLD_AT is the Kyiv wall
clock of the invoice instant. Every history, co-purchase, popularity and "already bought"
query of the service is built from SPINE and PURCHASED.

An open order is a non-deleted sale created in the recent window and not invoiced before the
cut-off. Its lines are not purchases; they are what the client already has in the basket.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.history import source_history_start

BUSINESS_ZONE = ZoneInfo("Europe/Kyiv")
SQL_BUSINESS_ZONE = "FLE Standard Time"
SALE_INSTANT = "s.ChangedToInvoice"
SOLD_AT = (
    f"CAST({SALE_INSTANT} AT TIME ZONE 'UTC' "
    f"AT TIME ZONE '{SQL_BUSINESS_ZONE}' AS datetime2(3))"
)

SPINE = """dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.ClientAgreement ca ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID"""


def confirmed_sale(alias: str = "s") -> str:
    return (
        f"{alias}.Deleted = 0 AND {alias}.ChangedToInvoice IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM dbo.DataSyncAccountingSourceTarget carrier "
        f"WHERE carrier.TargetEntityType = N'Sale' AND carrier.TargetEntityID = {alias}.ID "
        f"AND carrier.IsCurrent = 1 AND carrier.OwnershipType = 1)"
    )


def confirmed_line(alias: str = "oi") -> str:
    return (
        f"{alias}.Deleted = 0 AND {alias}.IsValidForCurrentSale = 1 AND {alias}.Qty > 0 "
        f"AND {alias}.ProductID IS NOT NULL"
    )


def confirmed_return(item: str = "sri", document: str = "sr") -> str:
    return (
        f"{item}.Deleted = 0 AND {item}.Qty > 0 "
        f"AND {document}.Deleted = 0 AND {document}.IsCanceled = 0"
    )


def returned_units(line: str = "oi", upper: str = ":sold_to") -> str:
    return (
        f"ISNULL((SELECT SUM(CAST(sri.Qty AS decimal(38, 6))) "
        f"FROM dbo.SaleReturnItem sri JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID "
        f"WHERE sri.OrderItemID = {line}.ID AND {confirmed_return('sri', 'sr')} "
        f"AND sr.FromDate < {upper}), 0)"
    )


KEPT_UNITS = f"(CAST(oi.Qty AS decimal(38, 6)) - {returned_units()})"

FULLY_RETURNED_LINES = (
    f"(SELECT sri.OrderItemID AS line_id "
    f"FROM dbo.SaleReturnItem sri JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID "
    f"JOIN dbo.OrderItem roi ON roi.ID = sri.OrderItemID "
    f"WHERE {confirmed_return('sri', 'sr')} AND sr.FromDate < :sold_to "
    f"GROUP BY sri.OrderItemID, roi.Qty "
    f"HAVING SUM(CAST(sri.Qty AS decimal(38, 6))) >= CAST(roi.Qty AS decimal(38, 6)))"
)

CONFIRMED = f"{confirmed_sale('s')} AND {confirmed_line('oi')}"

PURCHASED = (
    f"{CONFIRMED} "
    f"AND {SALE_INSTANT} >= :sold_from AND {SALE_INSTANT} < :sold_to "
    f"AND NOT EXISTS (SELECT 1 FROM {FULLY_RETURNED_LINES} returned WHERE returned.line_id = oi.ID)"
)


ORDERED_IN_WINDOW = "o.Created >= :sold_from"

OPEN_ORDER = (
    f"s.Deleted = 0 AND ({SALE_INSTANT} IS NULL OR {SALE_INSTANT} >= :sold_to) "
    f"AND o.Created >= :open_from AND o.Created < :sold_to AND {confirmed_line('oi')}"
)


def sellable_storage(alias: str = "st") -> str:
    return (
        f"{alias}.Deleted = 0 AND {alias}.ForDefective = 0 "
        f"AND ({alias}.AvailableForReSale = 1 OR {alias}.IsResale = 1)"
    )


def today() -> str:
    return datetime.now(BUSINESS_ZONE).date().isoformat()


def local_moment(value: date | datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = value.strip().replace("Z", "")
    try:
        return datetime.combine(date.fromisoformat(text), time.min)
    except ValueError:
        return datetime.fromisoformat(text).replace(tzinfo=None)


def utc_instant(value: date | datetime | str) -> datetime:
    return local_moment(value).replace(tzinfo=BUSINESS_ZONE).astimezone(UTC).replace(tzinfo=None)


def window(
    as_of: date | datetime | str, start: date | datetime | str | None = None,
) -> dict[str, datetime]:
    floor = local_moment(source_history_start())
    lower = max(local_moment(start), floor) if start is not None else floor
    return {"sold_from": utc_instant(lower), "sold_to": utc_instant(as_of)}


def trailing_window(as_of: date | datetime | str, days: int) -> dict[str, datetime]:
    return window(as_of, local_moment(as_of) - timedelta(days=int(days)))


def trailing_year(as_of: date | datetime | str) -> datetime:
    moment = local_moment(as_of)
    try:
        return moment.replace(year=moment.year - 1)
    except ValueError:
        return moment.replace(year=moment.year - 1, day=28)


def open_window(as_of: date | datetime | str, days: int) -> dict[str, datetime]:
    moment = local_moment(as_of)
    return {
        "open_from": utc_instant(moment - timedelta(days=int(days))),
        "sold_to": utc_instant(moment),
    }


def forward_window(as_of: date | datetime | str, days: int) -> dict[str, datetime]:
    start = local_moment(as_of)
    return {"sold_from": utc_instant(start), "sold_to": utc_instant(start + timedelta(days=int(days)))}
