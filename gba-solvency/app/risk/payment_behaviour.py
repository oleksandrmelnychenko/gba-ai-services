from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.data.db import in_clause, query
from app.data.source_rules import debt_agreement
from app.data.synthetic_product import synthetic_product_ids

MIN_INVOICE_EUR = 20.0
MATERIAL_LINE_EUR = 5.0
LATE_DAYS = 7
MAX_GRACE_DAYS = 365
SETTLE_TOLERANCE_EUR = 0.5
SETTLE_TOLERANCE_SHARE = 0.01
TREND_CLIP_DAYS = 60.0
ID_CHUNK = 900

BEHAVIOUR_KEYS = (
    "worst_delay_12m_days",
    "share_late_invoices_12m",
    "delay_trend_3m_days",
    "matured_invoices_12m",
    "oldest_overdue_days",
)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def empty_behaviour() -> dict[str, float]:
    return {key: 0.0 for key in BEHAVIOUR_KEYS}


def settle_fifo(
    invoices: Sequence[dict[str, Any]], credits: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    ordered = sorted(invoices, key=lambda r: (r["date"], r["sale_id"]))
    pool = sorted(
        ([c["date"], float(c["eur"])] for c in credits if float(c["eur"]) > 0.0),
        key=lambda c: c[0],
    )
    out: list[dict[str, Any]] = []
    j = 0
    for inv in ordered:
        amount = float(inv["amount"])
        if amount <= 0.0:
            continue
        tolerance = max(SETTLE_TOLERANCE_EUR, SETTLE_TOLERANCE_SHARE * amount)
        remaining = amount
        settled: date | None = None
        while remaining > 1e-9 and j < len(pool):
            use = min(remaining, pool[j][1])
            remaining -= use
            pool[j][1] -= use
            paid_on = max(pool[j][0], inv["date"])
            if settled is None and remaining <= tolerance:
                settled = paid_on
            if pool[j][1] <= 1e-9:
                j += 1
        if settled is None and remaining <= tolerance:
            settled = inv["date"]
        out.append({**inv, "amount": amount, "open": max(remaining, 0.0), "settled": settled})
    return out


def behaviour_features(
    invoices: Sequence[dict[str, Any]], credits: Sequence[dict[str, Any]], as_of: date
) -> dict[str, float]:
    known_invoices = [r for r in invoices if r["date"] <= as_of]
    known_credits = [c for c in credits if c["date"] <= as_of]
    year_ago = as_of - timedelta(days=365)
    q1 = as_of - timedelta(days=91)
    q2 = as_of - timedelta(days=182)
    delays: list[float] = []
    recent: list[float] = []
    previous: list[float] = []
    for inv in settle_fifo(known_invoices, known_credits):
        if inv["legacy"] or inv["amount"] < MIN_INVOICE_EUR or inv["due"] > as_of:
            continue
        end = inv["settled"] if inv["settled"] is not None else as_of
        delay = float(max((end - inv["due"]).days, 0))
        if inv["due"] > year_ago:
            delays.append(delay)
        if inv["due"] > q1:
            recent.append(delay)
        elif inv["due"] > q2:
            previous.append(delay)
    out = empty_behaviour()
    if delays:
        out["worst_delay_12m_days"] = max(delays)
        out["share_late_invoices_12m"] = sum(d > LATE_DAYS for d in delays) / len(delays)
        out["matured_invoices_12m"] = float(len(delays))
    mean_recent = sum(recent) / len(recent) if recent else 0.0
    mean_previous = sum(previous) / len(previous) if previous else 0.0
    out["delay_trend_3m_days"] = min(
        max(mean_recent - mean_previous, -TREND_CLIP_DAYS), TREND_CLIP_DAYS
    )
    return out


def _chunks(ids: Sequence[int]) -> Iterable[list[int]]:
    for start in range(0, len(ids), ID_CHUNK):
        yield list(ids[start:start + ID_CHUNK])


def load_ledger(
    client_ids: Sequence[int], as_of: str
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    synthetic = sorted(synthetic_product_ids())
    syn_ph, syn_params = in_clause("syn", synthetic)
    history_start = get_settings().source_history_start_date
    invoices: dict[int, list[dict[str, Any]]] = defaultdict(list)
    credits: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for chunk in _chunks(list(client_ids)):
        ph, params = in_clause("cid", chunk)
        for r in query(
            f"""
            SELECT s.ID AS sale_id, ca.ClientID AS client_id, s.Created AS created,
                   a.NumberDaysDebt AS grace,
                   SUM(oi.Qty * oi.PricePerItem) AS amount,
                   MAX(CASE WHEN oi.ProductID IN {syn_ph} THEN 1 ELSE 0 END) AS legacy
            FROM dbo.Sale s
            JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
            JOIN dbo.Agreement a ON a.ID = ca.AgreementID
            JOIN dbo.OrderItem oi ON oi.OrderID = s.OrderID AND oi.IsValidForCurrentSale = 1
            WHERE ca.ClientID IN {ph}
                  AND s.Created < DATEADD(day, 1, :asof)
            GROUP BY s.ID, ca.ClientID, s.Created, a.NumberDaysDebt
            """,
            {"asof": as_of, **params, **syn_params},
        ):
            day = _as_date(r["created"])
            legacy = bool(r["legacy"]) or day < history_start
            grace = 0 if legacy else min(max(int(r["grace"] or 0), 0), MAX_GRACE_DAYS)
            invoices[int(r["client_id"])].append(
                {
                    "sale_id": int(r["sale_id"]),
                    "date": day,
                    "due": day + timedelta(days=grace),
                    "amount": float(r["amount"] or 0.0),
                    "legacy": legacy,
                }
            )
        for r in query(
            f"""
            SELECT p.ClientID AS client_id, p.FromDate AS paid_on, p.EuroAmount AS eur
            FROM dbo.IncomePaymentOrder p
            WHERE p.ClientID IN {ph}
                  AND p.Deleted = 0
                  AND p.IsCanceled = 0
                  AND p.FromDate < DATEADD(day, 1, :asof)
            """,
            {"asof": as_of, **params},
        ):
            credits[int(r["client_id"])].append(
                {"date": _as_date(r["paid_on"]), "eur": float(r["eur"] or 0.0)}
            )
        for r in query(
            f"""
            SELECT sr.ClientID AS client_id, sr.FromDate AS returned_on,
                   SUM(sri.Qty * oi.PricePerItem) AS eur
            FROM dbo.SaleReturn sr
            JOIN dbo.SaleReturnItem sri ON sri.SaleReturnID = sr.ID AND sri.Deleted = 0
            JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
            WHERE sr.ClientID IN {ph}
                  AND sr.Deleted = 0
                  AND sr.IsCanceled = 0
                  AND sr.FromDate < DATEADD(day, 1, :asof)
            GROUP BY sr.ID, sr.ClientID, sr.FromDate
            """,
            {"asof": as_of, **params},
        ):
            credits[int(r["client_id"])].append(
                {"date": _as_date(r["returned_on"]), "eur": float(r["eur"] or 0.0)}
            )
    return invoices, credits


def oldest_material_overdue_days(client_ids: Sequence[int], as_of: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for chunk in _chunks(list(client_ids)):
        ph, params = in_clause("cid", chunk)
        for r in query(
            f"""
            SELECT client_id, MAX(od) AS oldest
            FROM (
                SELECT cid.ClientID AS client_id,
                       DATEDIFF(day, d.Created, :asof) - a.NumberDaysDebt AS od,
                       dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof) AS e
                FROM dbo.ClientInDebt cid
                JOIN dbo.Debt d ON d.ID = cid.DebtID
                {debt_agreement(":asof")}
                WHERE cid.Deleted = 0
                      AND d.Deleted = 0
                      AND cid.ClientID IN {ph}
                      AND d.Created <= :asof
            ) t
            WHERE e >= :material
            GROUP BY client_id
            """,
            {"asof": as_of, "material": MATERIAL_LINE_EUR, **params},
        ):
            out[int(r["client_id"])] = float(max(int(r["oldest"] or 0), 0))
    return out


def behaviour_many(client_ids: Sequence[int], as_of: str) -> dict[int, dict[str, float]]:
    ids = list(dict.fromkeys(int(c) for c in client_ids))
    if not ids:
        return {}
    cutoff = date.fromisoformat(as_of)
    invoices, credits = load_ledger(ids, as_of)
    oldest = oldest_material_overdue_days(ids, as_of)
    out: dict[int, dict[str, float]] = {}
    for cid in ids:
        feats = behaviour_features(invoices.get(cid, []), credits.get(cid, []), cutoff)
        feats["oldest_overdue_days"] = oldest.get(cid, 0.0)
        out[cid] = feats
    return out
