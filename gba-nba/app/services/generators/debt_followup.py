"""Generator: debt_followup — overdue clients of a manager → call-to-collect tasks."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.data import signals_repository as sig
from app.domain.models import (
    Contact,
    Explanation,
    Task,
    TaskType,
)
from app.services import scoring

TYPE = TaskType.DEBT_FOLLOWUP


def generate(manager_id: int, as_of: str, window_tag: str) -> list[Task]:
    s = get_settings()
    rows = sig.overdue_debts_for_manager(manager_id, as_of, max_age_days=s.debt_max_age_days,
                                         min_amount=s.debt_min_amount)
    if not rows:
        return []
    client_ids = [int(r["client_id"]) for r in rows]
    contacts = sig.contacts_for_clients(client_ids)
    feats = sig.client_features(client_ids, as_of)
    due = datetime.now(UTC) + timedelta(days=s.service_level_due_days)

    tasks: list[Task] = []
    for r in rows:
        cid = int(r["client_id"])
        days_past = int(r["max_days_past_terms"] or 0)
        max_overdue = int(r["max_overdue_days"] or 0)
        amount = float(r["overdue_amount"] or 0)
        debt_lines = int(r["debt_lines"] or 0)
        cf = feats.get(cid, {})
        monetary = float(cf.get("monetary") or 0.0)
        c = contacts.get(cid, {})
        name = c.get("full_name") or c.get("name") or f"Client {cid}"

        u = scoring.debt_urgency(days_past)
        # priority now = 100 * P(repayment | task) — the model ranks debts by repayment-likelihood,
        # not biggest-overdue (the old value term was inverted). urgency_band stays the cash-at-risk tier.
        feat = {"days_past_terms": days_past, "overdue_amount": amount,
                "max_overdue_days": max_overdue, "debt_lines": debt_lines,
                "monetary": monetary, "recency_days": cf.get("recency_days"),
                "order_count": cf.get("order_count", 0)}
        prio, p_out, ev, ev_score = scoring.score_task_priority(TYPE.value, feat)

        tasks.append(Task(
            task_key=f"mgr:{manager_id}|client:{cid}|type:{TYPE.value}|win:{window_tag}",
            manager_id=manager_id, client_id=cid, client_name=name, task_type=TYPE,
            title="Нагадати про оплату заборгованості",
            reason=f"Прострочка {max_overdue} дн (на {days_past} дн понад умови), "
                   f"борг {amount:.0f}",
            priority=prio, p_outcome=p_out, expected_value=ev, ev_score=ev_score,
            urgency=scoring.urgency_band(u),
            payload={"debt": {"overdue_amount": amount, "max_overdue_days": max_overdue,
                              "days_past_terms": days_past, "debt_lines": debt_lines}},
            signals={"days_past_terms": days_past, "overdue_amount": amount,
                     "max_overdue_days": max_overdue, "debt_lines": debt_lines,
                     "monetary": monetary, "recency_days": cf.get("recency_days"),
                     "order_count": cf.get("order_count", 0)},
            explanation=Explanation(
                factors=[f"прострочка {days_past} дн понад умови договору",
                         f"сума боргу {amount:.0f}",
                         f"річний оборот {monetary:.0f}"],
                source_signal="debt", confidence=1.0),
            contact=Contact(phone=c.get("phone"), email=c.get("email"),
                            preferred="phone" if c.get("phone") else "email"),
            due_date=due, ab_variant="debt_v1",
        ))
    return tasks
