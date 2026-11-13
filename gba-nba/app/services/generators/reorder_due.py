"""Generator: reorder_due — clients with products past their purchase cycle → reorder tasks.

One task per CLIENT (not per product): bundle the client's due products, most purchased first,
into one actionable call task, so the inbox isn't flooded with per-SKU rows.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from app.core import history
from app.core.config import get_settings
from app.data import signals_repository as sig
from app.domain.models import Contact, Explanation, Task, TaskType
from app.services import repeat_sales, scoring

TYPE = TaskType.REORDER_DUE
_MAX_PRODUCTS_PER_TASK = 5


def _likely_first(item: dict) -> tuple:
    """Products the client buys most often lead the task; the most overdue one is the least
    likely to be bought again (backtest 2026-01..2026-10: lead product bought within 30 days
    11% when most overdue leads, 19% when the most purchased leads)."""
    return (-int(item["n_orders"]), float(item["elapsed_days"]), int(item["product_id"]))


def generate(manager_id: int, as_of: str, window_tag: str) -> list[Task]:
    s = get_settings()
    rows = sig.reorder_candidates_for_manager(
        manager_id, as_of, min_cycle_days=s.reorder_min_cycle_days,
        max_overdue_mult=s.reorder_max_overdue_mult)
    excl = sig.ubiquitous_product_ids(s.ubiquity_exclude_pct, as_of)
    if excl:
        rows = [r for r in rows if int(r["product_id"]) not in excl]
    if not rows:
        return []

    by_client: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_client[int(r["client_id"])].append(r)

    client_ids = list(by_client.keys())
    contacts = sig.contacts_for_clients(client_ids)
    feats = sig.client_features(client_ids, as_of)
    all_pids = [int(r["product_id"]) for r in rows]
    names = sig.product_names(all_pids)
    due = datetime.now(UTC) + timedelta(days=s.service_level_due_days * 2)

    tasks: list[Task] = []
    for cid, items in by_client.items():
        for it in items:
            cyc = float(it["cycle_days"] or 0)
            it["_overdue_ratio"] = (float(it["elapsed_days"]) / cyc) if cyc > 0 else 1.0
        items.sort(key=_likely_first)
        top = items[:_MAX_PRODUCTS_PER_TASK]
        lead = top[0]
        cf = feats.get(cid, {})
        monetary_cid = float(cf.get("monetary") or 0.0)

        u = scoring.reorder_urgency(float(lead["elapsed_days"]), float(lead["cycle_days"] or 0))
        # priority = 100 * P(re-buy of the lead product | task); the lead row mirrors the dataset's
        # one-row-per-(client,product) reorder candidate (same elapsed/cycle/ratio/n_orders features).
        feat = {"elapsed_days": float(lead["elapsed_days"]),
                "cycle_days": float(lead["cycle_days"] or 0),
                "overdue_ratio": float(lead["_overdue_ratio"]),
                "n_orders": int(lead["n_orders"]),
                "monetary": monetary_cid, "recency_days": cf.get("recency_days"),
                "order_count": cf.get("order_count", 0)}
        prio, p_out, ev, ev_score = scoring.score_task_priority(TYPE.value, feat)
        us = sorted(scoring.reorder_urgency(float(it["elapsed_days"]), float(it["cycle_days"] or 0))
                    for it in items)
        u_band = us[min(int(0.75 * len(us)), len(us) - 1)] if us else u

        c = contacts.get(cid, {})
        name = c.get("full_name") or c.get("name") or f"Client {cid}"
        products = [{
            "product_id": int(it["product_id"]), "name": names.get(int(it["product_id"]), ""),
            "cycle_days": round(float(it["cycle_days"] or 0), 1),
            "elapsed_days": int(it["elapsed_days"]), "n_orders": int(it["n_orders"]),
            "source": "reorder",
        } for it in top]

        coverage = history.rolling_days(as_of, 365)
        for product, source in zip(products, top, strict=True):
            if source.get("last_buy") is not None:
                product.update(repeat_sales.product_evidence(
                    source, coverage.as_of, coverage.history_complete))

        lead_name = names.get(int(lead["product_id"]), f"товар {lead['product_id']}")
        tasks.append(Task(
            task_key=f"mgr:{manager_id}|client:{cid}|type:{TYPE.value}|win:{window_tag}",
            manager_id=manager_id, client_id=cid, client_name=name, task_type=TYPE,
            title="Час дозамовити: запропонувати поповнення",
            reason=f"«{lead_name}»: цикл ~{float(lead['cycle_days'] or 0):.0f} дн, "
                   f"минуло {int(lead['elapsed_days'])} дн"
                   + (f" (+{len(top) - 1} позицій)" if len(top) > 1 else ""),
            priority=prio, p_outcome=p_out, expected_value=ev, ev_score=ev_score,
            urgency=scoring.urgency_band(u_band),
            payload={"products": products, "history_start": history.source_history_start().isoformat(),
                     "history_complete": coverage.history_complete, "date_basis": "source_document_date"},
            signals={"elapsed_days": float(lead["elapsed_days"]),
                     "cycle_days": float(lead["cycle_days"] or 0),
                     "overdue_ratio": float(lead["_overdue_ratio"]),
                     "n_orders": int(lead["n_orders"]), "products_due": len(items),
                     "monetary": monetary_cid, "recency_days": cf.get("recency_days"),
                     "order_count": cf.get("order_count", 0)},
            explanation=Explanation(
                factors=[f"«{lead_name}» минуло {int(lead['elapsed_days'])} дн "
                         f"при циклі ~{float(lead['cycle_days'] or 0):.0f} дн",
                         f"{len(items)} позицій готові до повторного замовлення",
                         f"купували цей товар {int(lead['n_orders'])} раз(и)"],
                source_signal="reorder_due", confidence=min(1.0, 0.4 + 0.1 * float(lead["n_orders"]))),
            contact=Contact(phone=c.get("phone"), email=c.get("email"),
                            preferred="phone" if c.get("phone") else "email"),
            due_date=due, ab_variant="reorder_v1",
        ))
    return tasks
