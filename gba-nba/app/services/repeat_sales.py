"""Read-only seller segments with source evidence; existing cycle policy, no new ranking."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.core import history
from app.core.config import get_settings
from app.data import mongo
from app.data import signals_repository as sig

ClientSegment = Literal["all", "reorder_due", "reorder_overdue", "reactivation", "new_client"]
SEGMENTS = ("all", "reorder_due", "reorder_overdue", "reactivation", "new_client")
KYIV = ZoneInfo("Europe/Kyiv")
_SUCCESSFUL_CONTACT = {"reached", "proposal", "declined", "promised_payment"}


def _day(value: date | datetime | str) -> date:
    # Document dates have deliberately unspecified timezone provenance.
    return date.fromisoformat(str(value)[:10])


def _utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    # Mongo's persisted event clock is UTC, including legacy naive BSON decoding.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _latest_contacts(client_ids: list[int], end: datetime) -> dict[int, dict]:
    if not client_ids:
        return {}
    rows = mongo.seller_contact_events().aggregate([
        {"$match": {"client_id": {"$in": client_ids}, "at": {"$lt": end}}},
        {"$sort": {"at": -1, "sort_id": -1}},
        {"$group": {"_id": "$client_id", "event": {"$first": "$$ROOT"}}},
    ], allowDiskUse=True)
    return {int(row["_id"]): row["event"] for row in rows}


def product_evidence(row: dict, as_of: date, coverage_complete: bool) -> dict:
    cycle = float(row["cycle_days"]) if row.get("cycle_days") is not None else None
    last = _day(row["last_buy"])
    expected = last + timedelta(days=math.ceil(cycle)) if cycle is not None else None
    qty = float(row["available_qty"]) if row.get("available_qty") is not None else None
    status = "unknown" if qty is None else "available" if qty > 0 else "out_of_stock"
    history_status = "short" if cycle is None else "sufficient" if coverage_complete else "partial"
    reason = (f"Підтверджених покупок: {int(row['n_orders'])}; середній інтервал — {cycle:.1f} дн. "
              "Орієнтир за минулими покупками, сезонність окремо не прогнозується."
              if cycle is not None else "Замало покупок або різних дат для обґрунтованого циклу.")
    if status == "out_of_stock":
        reason += " Немає вільного залишку на доступних складах."
    elif status == "unknown":
        reason += " Поточну наявність не підтверджено."
    else:
        reason += " Наявність загальна; перед продажем перевіряється склад і договір."
    return {"product_id": int(row["product_id"]), "product_net_uid": row.get("product_net_uid"),
            "name": row.get("product_name") or str(row["product_id"]),
            "last_purchase_on": last.isoformat(), "last_quantity": float(row["last_quantity"]),
            "recent_purchase_dates": [_day(row[key]).isoformat()
                                      for key in ("last_buy", "previous_buy", "third_buy") if row.get(key)],
            "purchase_count": int(row["n_orders"]), "cycle_days": cycle,
            "next_expected_on": expected.isoformat() if expected else None,
            "overdue_days": (as_of - expected).days if expected else None,
            "available_qty": qty, "availability_status": status,
            "history_status": history_status, "reason": reason}


def _cooldown(event: dict | None, products: list[dict], now: datetime, as_of: date,
              cycle_start: date | str | None = None) -> dict:
    event = event or {}
    contacted = _utc(event.get("at"))
    next_contact = _utc(event.get("next_contact_at"))
    reason = None
    if next_contact is not None and next_contact > now:
        reason = "scheduled_contact"
    elif next_contact is None and contacted is not None and event.get("result") in _SUCCESSFUL_CONTACT:
        due = [p for p in products if p["cycle_days"] is not None and p["overdue_days"] >= 0]
        if cycle_start is not None:
            if contacted.astimezone(KYIV).date() >= _day(cycle_start):
                reason = "contacted_current_cycle"
        elif due:
            # The contact must cover every presently due product cycle. There is no fixed TTL.
            def covers(p: dict) -> bool:
                cycle = p["cycle_days"]
                last = _day(p["last_purchase_on"])
                elapsed = (as_of - last).days
                cycle_start = last + timedelta(days=math.ceil(cycle * math.floor(elapsed / cycle)))
                return contacted.astimezone(KYIV).date() >= cycle_start
            if all(covers(p) for p in due):
                reason = "contacted_current_cycle"
    return {"active": reason is not None, "reason": reason, "event_id": event.get("event_id"),
            "contacted_at": contacted.isoformat() if contacted else None,
            "next_contact_at": next_contact.isoformat() if next_contact else None,
            "next_action": event.get("next_action"), "result": event.get("result")}


def read_snapshot(manager_id: int, as_of: str) -> tuple[list[dict], list[dict], set[int]]:
    """One SQL snapshot for book identity, purchase stats, repeat evidence and churn."""
    settings = get_settings()
    facts = sig.repeat_products_for_manager(manager_id, as_of, min_cycle_days=settings.reorder_min_cycle_days,
                                            include_day=True, include_book=True)
    clients = {}
    products = []
    end = history.require_as_of(as_of) + timedelta(days=1)
    churn = {int(row["client_id"])
             for row in sig.churn_candidates_for_manager(manager_id, end.isoformat())}
    fields = ("client_id", "client_net_uid", "name", "full_name", "phone", "email", "last_order",
              "orders_cnt", "retained_orders_cnt", "turnover_eur")
    for fact in facts:
        cid = int(fact["client_id"])
        clients[cid] = {key: fact.get(key) for key in fields}
        clients[cid].update({"_due_count": fact["reorder_due_count"],
                             "_overdue_count": fact["reorder_overdue_count"],
                             "_cooldown_cycle_start": fact["cooldown_cycle_start"]})
        if fact.get("product_id") is not None:
            products.append(fact)
    rows = sorted(clients.values(), key=lambda r: (-float(r["turnover_eur"] or 0),
                                                   r.get("name") or "", r["client_id"]))
    return rows, products, churn


def enrich_and_page(manager_id: int, as_of: str, rows: list[dict], *, segment: ClientSegment = "all",
                    search: str = "", offset: int = 0, limit: int = 50,
                    now: datetime | None = None, facts: tuple[list[dict], set[int]] | None = None) -> dict:
    """Counts cover the entire owned, searched population, before segment/pagination.

    Source failures propagate. An unavailable Mongo/SQL source cannot become a successful empty book.
    """
    day = history.require_as_of(as_of)
    coverage = history.rolling_days(day, 365)
    settings = get_settings()
    excluded = sig._excluded(as_of)
    if facts is None:
        raw_products = sig.repeat_products_for_manager(
            manager_id, as_of, min_cycle_days=settings.reorder_min_cycle_days, include_day=True)
        churn = {int(row["client_id"]) for row in sig.churn_candidates_for_manager(
            manager_id, (day + timedelta(days=1)).isoformat())}
    else:
        raw_products, churn = facts
    products_by_client: dict[int, list[dict]] = defaultdict(list)
    for row in raw_products:
        if int(row["product_id"]) not in excluded:
            products_by_client[int(row["client_id"])].append(
                product_evidence(row, day, coverage.history_complete))
    end = datetime.combine(day + timedelta(days=1), time.min, KYIV).astimezone(UTC)
    now = now or datetime.now(UTC)
    effective_now = now if now.astimezone(KYIV).date() == day else end - timedelta(microseconds=1)
    contacts = _latest_contacts([int(r["client_id"]) for r in rows], min(end, now))
    enriched = []
    search = search.strip()
    for original in rows:
        row = dict(original)
        cid = int(row["client_id"])
        products = products_by_client[cid]
        due = [p for p in products if p["cycle_days"] is not None
               and p["overdue_days"] >= 0
               and (day - _day(p["last_purchase_on"])).days
               <= p["cycle_days"] * settings.reorder_max_overdue_mult]
        segments = []
        overdue_count = row.pop("_overdue_count", sum(p["overdue_days"] > 0 for p in due))
        due_count = row.pop("_due_count", sum(p["overdue_days"] == 0 for p in due))
        if overdue_count or due_count:
            segments.append("reorder_overdue" if overdue_count else "reorder_due")
        if cid in churn:
            segments.append("reactivation")
        no_purchases = int(row.get("retained_orders_cnt", row.get("orders_cnt", 0))) == 0
        if no_purchases:
            segments.append("new_client")
        cooldown = _cooldown(contacts.get(cid), due, effective_now, day,
                             row.pop("_cooldown_cycle_start", None))
        if cooldown["active"]:
            segments = []
        # Existing reorder ranking: elapsed/cycle descending, top five product explanations.
        products.sort(key=lambda p: (
            p in due,
            (day - _day(p["last_purchase_on"])).days / p["cycle_days"] if p["cycle_days"] else -1,
            -p["product_id"]), reverse=True)
        row.update({"segments": segments, "repeat_evidence": {
            "products": products[:5], "history_start": history.source_history_start().isoformat(),
            "history_complete": coverage.history_complete, "date_basis": "source_document_date",
            "reason": ("Немає підтверджених неповернених покупок у доступній історії; "
                       "це не дата появи клієнта."
                       if no_purchases else "Цикл розраховано за підтвердженими неповерненими покупками. "
                       "Скасовані продажі, чернетки й дублікати об’єднання виключено.")
                      + (" Останній рік покрито повністю; цикл враховує всю доступну історію."
                         if coverage.history_complete
                         else " Історія за останній рік неповна.")},
            "contact_cooldown": cooldown,
            "new_client_basis": "no_retained_purchase_in_available_history" if no_purchases else None})
        # Existing last_order is date-only too: never let the browser infer a source timezone.
        if row.get("last_order"):
            row["last_order"] = _day(row["last_order"]).isoformat()
        if not search or search.casefold() in " ".join(str(row.get(k) or "") for k in (
                "name", "full_name", "phone", "email", "client_id")).casefold():
            enriched.append(row)
    counts = {key: len(enriched) if key == "all" else sum(key in r["segments"] for r in enriched)
              for key in SEGMENTS}
    selected = enriched if segment == "all" else [r for r in enriched if segment in r["segments"]]
    page = selected[offset:offset + limit]
    return {"clients": page, "count": len(page), "total": len(selected),
            "population_count": len(enriched), "segment_counts": counts,
            "segment": segment, "search": search, "offset": offset, "limit": limit}
