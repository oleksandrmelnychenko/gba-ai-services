"""Positive, document-level facts for task reconciliation; absence is never settlement.

Small client batches only. Zero balances are deliberately included, deleted/missing debt rows
are not evidence. A debt whose only client attachment is soft-deleted is reported with a zero
balance: gba-server and the sync close a paid document that way and leave Debt.Total unchanged. Orders with no remaining purchased quantity cannot resolve a sales suggestion.
"""
from datetime import UTC, date, timedelta

from app.core import history
from app.data.db import in_clause, query
from app.data.purchase_history import retained_purchase_cte


def source_facts(client_ids: list[int], as_of: str) -> dict[int, dict]:
    result = {}
    client_ids = sorted(set(client_ids))
    end = (date.fromisoformat(as_of) + timedelta(days=1)).isoformat()
    for start in range(0, len(client_ids), 300):
        clause, params = in_clause("client", client_ids[start:start + 300])
        params |= {"start": history.source_history_start().isoformat(), "end": end}
        owners = query(f"""
            SELECT c.ID AS client_id, c.MainManagerID AS manager_id
            FROM dbo.Client c
            JOIN dbo.[User] u ON u.ID = c.MainManagerID AND u.Deleted = 0
            WHERE c.ID IN {clause} AND c.Deleted = 0
        """, params)
        debts = query(f"""
            SELECT cid.ClientID AS client_id, d.ID AS debt_id,
                   CASE WHEN MAX(CASE WHEN cid.Deleted = 0 THEN 1 ELSE 0 END) = 1
                        THEN MAX(d.Total) ELSE 0 END AS balance
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID AND d.Deleted = 0
            WHERE cid.ClientID IN {clause} AND d.Created < :end
            GROUP BY cid.ClientID, d.ID
        """, params)
        purchase_cte = retained_purchase_cte(f"c.ID IN {clause}", "start", "end")
        orders = query(purchase_cte + """
            , ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY client_id, product_id ORDER BY purchased_at DESC, order_id DESC
                ) AS rn FROM retained_purchases
            )
            SELECT client_id, product_id, order_id, purchased_at, quantity,
                   timestamp_basis, purchased_at_utc
            FROM ranked WHERE rn = 1
        """, params)
        returns = query(purchase_cte + """
            , returned AS (
                SELECT p.client_id, oi.ProductID AS product_id,
                       p.order_id, SUM(oi.ReturnedQty) AS returned_quantity,
                       MAX(oi.Updated) AS returned_at,
                       ROW_NUMBER() OVER (PARTITION BY p.client_id, oi.ProductID
                           ORDER BY MAX(oi.Updated) DESC, p.order_id DESC) AS rn
                FROM purchase_orders p
                JOIN dbo.OrderItem oi ON oi.OrderID = p.order_id AND oi.Deleted = 0
                WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
                      AND oi.ReturnedQty > 0 AND oi.ReturnedQty <= oi.Qty AND oi.Updated < :end
                GROUP BY p.client_id, oi.ProductID, p.order_id
            )
            SELECT client_id, product_id, order_id, returned_quantity, returned_at
            FROM returned WHERE rn = 1
        """, params)
        for row in owners:
            result[int(row["client_id"])] = {"manager_id": int(row["manager_id"]),
                                             "debts": {}, "orders": {}, "returns": {}}
        for row in debts:
            if int(row["client_id"]) in result:
                result[int(row["client_id"])]["debts"][str(row["debt_id"])] = str(row["balance"])
        for row in orders:
            if int(row["client_id"]) in result:
                result[int(row["client_id"])]["orders"][str(row["product_id"])] = {
                    "order_id": int(row["order_id"]),
                    "purchased_at": row["purchased_at"].isoformat(),
                    "timestamp_basis": row["timestamp_basis"],
                    "purchased_at_utc": (row["purchased_at_utc"].replace(tzinfo=UTC).isoformat()
                                         if row["purchased_at_utc"] is not None else None),
                }
        for row in returns:
            if int(row["client_id"]) in result:
                result[int(row["client_id"])]["returns"][str(row["product_id"])] = {
                    "order_id": int(row["order_id"]), "quantity": str(row["returned_quantity"]),
                    "returned_at": row["returned_at"].isoformat(),
                }
    return result
