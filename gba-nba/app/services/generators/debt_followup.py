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
    monetary = sig.client_monetary(client_ids, as_of)
    due = datetime.now(UTC) + timedelta(days=s.service_level_due_days)

    tasks: list[Task] = []
    for r in rows:
        cid = int(r["client_id"])
        days_past = int(r["max_days_past_terms"] or 0)
        amount = float(r["overdue_amount"] or 0)
        c = contacts.get(cid, {})
        name = c.get("full_name") or c.get("name") or f"Client {cid}"

        u = scoring.debt_urgency(days_past)
        # value = cash AT RISK (the overdue amount), not annual turnover — chase the biggest debts first
        v = scoring.value_from_monetary(amount)
        conf = 1.0  # debt is a hard fact, full confidence
        prio = scoring.priority(u, v, conf)

        tasks.append(Task(
            task_key=f"mgr:{manager_id}|client:{cid}|type:{TYPE.value}|win:{window_tag}",
            manager_id=manager_id, client_id=cid, client_name=name, task_type=TYPE,
            title="Нагадати про оплату заборгованості",
            reason=f"Прострочка {int(r['max_overdue_days'])} дн (на {days_past} дн понад умови), "
                   f"борг {amount:.0f}",
            priority=prio, urgency=scoring.urgency_band(u),
            payload={"debt": {"overdue_amount": amount, "max_overdue_days": int(r["max_overdue_days"] or 0),
                              "days_past_terms": days_past, "debt_lines": int(r["debt_lines"] or 0)}},
            signals={"days_past_terms": days_past, "overdue_amount": amount,
                     "monetary": monetary.get(cid, 0.0)},
            explanation=Explanation(
                factors=[f"прострочка {days_past} дн понад умови договору",
                         f"сума боргу {amount:.0f}",
                         f"річний оборот {monetary.get(cid, 0.0):.0f}"],
                source_signal="debt", confidence=conf),
            contact=Contact(phone=c.get("phone"), email=c.get("email"),
                            preferred="phone" if c.get("phone") else "email"),
            due_date=due, ab_variant="debt_v1",
        ))
    return tasks
