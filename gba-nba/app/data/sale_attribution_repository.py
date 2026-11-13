"""Documentary sale/payment facts. Never infer contact causality or revalue money.

A return of a sale older than the imported history is anchored by the 1C import to a
placeholder line (IMPORT_PLACEHOLDER_RETURN: the zero shape and caption the sync writes on an
imported return), often inside an unrelated later sale of the same agreement. It is a fact of
its own date, client and product, never a return of the sale that hosts the placeholder.
"""

from datetime import UTC
from zoneinfo import ZoneInfo

from app.core import history
from app.data.db import in_clause, query

# Same positive timestamp provenance as the retained-purchase source. Imported legacy
# clocks without one exact present lineage identity remain explicitly unverified.
_SALE_DATES = """
OUTER APPLY (
 SELECT MIN(l.DestinationDocumentDate) first_date, MAX(l.DestinationDocumentDate) last_date,
 COUNT(DISTINCT CONVERT(varchar(32),l.SourceSaleID,2) + ':' +
       CONVERT(varchar(1),l.ImportedForAmg)) source_count
 FROM dbo.DataSyncImportedSaleSourceLine l WHERE l.LocalSaleID=s.ID AND l.IsPresent=1
) lineage
CROSS APPLY (SELECT
 CASE WHEN s.IsImported=0 AND s.ChangedToInvoice IS NOT NULL THEN s.ChangedToInvoice
      ELSE s.Created END event_at,
 CASE WHEN s.IsImported=0 AND s.ChangedToInvoice IS NOT NULL THEN 'native_invoice_kyiv'
      WHEN s.IsImported=1 AND lineage.first_date=lineage.last_date
       AND lineage.source_count=1
       AND ABS(DATEDIFF_BIG(millisecond,lineage.first_date,s.Created))<=4
       THEN 'imported_lineage_kyiv' ELSE 'stored_sale_date_unverified' END date_basis
) basis
CROSS APPLY (SELECT
 CASE WHEN basis.date_basis<>'stored_sale_date_unverified'
      THEN CAST(basis.event_at AT TIME ZONE 'UTC' AT TIME ZONE 'FLE Standard Time' AS datetime2)
      ELSE basis.event_at END document_at, basis.date_basis
) dates
"""

IMPORT_PLACEHOLDER_LINE = (
    "oi.Qty=0 AND oi.PricePerItem=0 AND oi.PricePerItemWithoutVat=0 "
    "AND oi.ExchangeRateAmount=0 AND ISNULL(LTRIM(RTRIM(oi.Comment)),N'')=N'1С'"
)
IMPORT_PLACEHOLDER_RETURN = f"r.SourceDocumentID IS NOT NULL AND {IMPORT_PLACEHOLDER_LINE}"

_SALES = (
    """
SELECT s.ID sale_id, CONVERT(varchar(36), s.NetUid) sale_net_uid,
 ca.ClientID client_id, COALESCE(i.Number,n.Value,CONVERT(varchar(30),s.ID)) number,
 CASE WHEN s.ChangedToInvoice IS NOT NULL THEN 1 ELSE 0 END is_invoice,
 dates.document_at, dates.date_basis, s.ShipmentDate shipped_at, s.OrderID order_id,
 CONVERT(varchar(36),offer.NetUid) offer_net_uid, cur.Code document_currency,
 CASE WHEN s.Deleted=1 OR o.Deleted=1 OR ca.Deleted=1 THEN 1 ELSE 0 END canceled,
 CASE WHEN s.IsMerged=1 OR o.IsMerged=1 OR EXISTS(SELECT 1 FROM dbo.Sale sibling
 WHERE sibling.OrderID=s.OrderID AND sibling.ID<>s.ID AND sibling.Deleted=0)
 OR EXISTS(SELECT 1 FROM dbo.SaleMerged m WHERE m.Deleted=0
 AND (m.InputSaleID=s.ID OR m.OutputSaleID=s.ID)) THEN 1 ELSE 0 END merged,
 (SELECT COUNT(*) FROM dbo.OrderItem oi WHERE oi.OrderID=o.ID AND oi.Deleted=0
  AND oi.IsValidForCurrentSale=1 AND oi.Qty>0 AND ISNULL(oi.ReturnedQty,0)>=0
  AND oi.Qty>ISNULL(oi.ReturnedQty,0)) retained_lines
FROM dbo.Sale s JOIN dbo.[Order] o ON o.ID=s.OrderID
JOIN dbo.ClientAgreement ca ON ca.ID=s.ClientAgreementID
LEFT JOIN dbo.Agreement a ON a.ID=ca.AgreementID
LEFT JOIN dbo.Currency cur ON cur.ID=a.CurrencyID
LEFT JOIN dbo.SaleNumber n ON n.ID=s.SaleNumberID
LEFT JOIN dbo.SaleInvoiceNumber i ON i.ID=s.SaleInvoiceNumberID
LEFT JOIN dbo.ClientShoppingCart offer ON offer.ID=o.ClientShoppingCartID
"""
    + _SALE_DATES
)


def _day(value):
    return value.date().isoformat() if value is not None else None


def _return_day(value):
    # SaleReturn.FromDate is UTC, like NativeSalesNet's return calendar. Payment
    # FromDate has a stored document clock and must retain its separate policy.
    if value is None:
        return None
    utc = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return utc.astimezone(ZoneInfo("Europe/Kyiv")).date().isoformat()


def candidates(client_id: int, offset: int, limit: int) -> tuple[list[dict], int]:
    params = {
        "client": client_id,
        "start": history.source_history_start().isoformat(),
        "offset": offset,
        "limit": limit,
    }
    where = (
        " WHERE ca.ClientID=:client AND dates.document_at>=:start"
        " AND s.Deleted=0 AND o.Deleted=0 AND ca.Deleted=0"
    )
    total = query(
        "SELECT COUNT(*) total FROM dbo.Sale s JOIN dbo.[Order] o ON o.ID=s.OrderID "
        "JOIN dbo.ClientAgreement ca ON ca.ID=s.ClientAgreementID" + _SALE_DATES + where,
        params,
    )[0]["total"]
    rows = query(
        _SALES
        + where
        + " ORDER BY dates.document_at DESC,s.ID DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
        params,
    )
    return [_sale(row) for row in rows], int(total)


def _sale(row):
    return {
        "sale_id": int(row["sale_id"]),
        "sale_net_uid": row["sale_net_uid"].lower(),
        "client_id": int(row["client_id"]),
        "number": row["number"],
        "is_invoice": bool(row["is_invoice"]),
        "document_date": _day(row["document_at"]),
        "shipment_date": _day(row["shipped_at"]),
        "order_id": int(row["order_id"]),
        "offer_net_uid": (row["offer_net_uid"] or "").lower() or None,
        "document_currency": row["document_currency"],
        "canceled": bool(row["canceled"]),
        "merged": bool(row["merged"]),
        "retained_lines": int(row["retained_lines"]),
        "date_basis": row["date_basis"],
    }


def _returns(ids: str, args: dict) -> list[dict]:
    return query(
        f"""SELECT s.ID sale_id,r.ID return_id,r.FromDate document_at,
        ri.ID return_item_id,ri.Qty quantity,ri.IsMoneyReturned credit_processed,
        CASE WHEN ri.RecordedGrossAmountVersion=1 THEN ri.Amount END recorded_return_credit_eur,
        CASE WHEN r.Deleted=1 OR r.IsCanceled=1 OR ri.Deleted=1 THEN 1 ELSE 0 END canceled
        FROM dbo.Sale s JOIN dbo.OrderItem oi ON oi.OrderID=s.OrderID
        JOIN dbo.SaleReturnItem ri ON ri.OrderItemID=oi.ID JOIN dbo.SaleReturn r ON r.ID=ri.SaleReturnID
        WHERE s.ID IN {ids} AND NOT ({IMPORT_PLACEHOLDER_RETURN})
        ORDER BY r.FromDate,ri.ID""",
        args,
    )


def facts(sale_net_uids: list[str]) -> dict[str, dict]:
    if not sale_net_uids:
        return {}
    if len(sale_net_uids) > 100:
        raise ValueError("attribution_source_batch_too_large")
    clause, params = in_clause("sale", sale_net_uids)
    rows = query(_SALES + f" WHERE s.NetUid IN {clause}", params)
    result = {row["sale_net_uid"].lower(): _sale(row) for row in rows}
    if not result:
        return result
    ids, args = in_clause("id", [row["sale_id"] for row in rows])
    payments = query(
        f"""SELECT x.ID allocation_id,x.SaleID sale_id,p.ID payment_id,
        p.FromDate document_at,c.Code source_currency,x.Amount amount_eur,
        CASE WHEN x.Deleted=1 OR p.Deleted=1 OR p.IsCanceled=1 THEN 1 ELSE 0 END canceled
        FROM dbo.IncomePaymentOrderSale x JOIN dbo.IncomePaymentOrder p ON p.ID=x.IncomePaymentOrderID
        LEFT JOIN dbo.Currency c ON c.ID=p.CurrencyID WHERE x.SaleID IN {ids}
        ORDER BY p.FromDate,x.ID""",
        args,
    )
    returns = _returns(ids, args)
    by_id = {row["sale_id"]: row for row in result.values()}
    for row in by_id.values():
        row.update(payments=[], returns=[])
    for p in payments:
        by_id[p["sale_id"]]["payments"].append(
            {
                "allocation_id": int(p["allocation_id"]),
                "payment_id": int(p["payment_id"]),
                "document_date": _day(p["document_at"]),
                "source_currency": p["source_currency"],
                "currency": "EUR",
                "amount": float(p["amount_eur"]),
                "canceled": bool(p["canceled"]),
            }
        )
    for r in returns:
        by_id[r["sale_id"]]["returns"].append(
            {
                "return_id": int(r["return_id"]),
                "return_item_id": int(r["return_item_id"]),
                "document_date": _return_day(r["document_at"]),
                "quantity": float(r["quantity"]),
                "credit_processed": bool(r["credit_processed"]),
                "recorded_return_credit_eur": None
                if r["recorded_return_credit_eur"] is None
                else float(r["recorded_return_credit_eur"]),
                "canceled": bool(r["canceled"]),
            }
        )
    return result
