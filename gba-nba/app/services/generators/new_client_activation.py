"""Generator: new_client_activation — recently-added clients with no (or ~no) orders → onboard
and land the first sale before the relationship goes cold."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.data import signals_repository as sig
from app.domain.models import Contact, Explanation, Task, TaskType
from app.services import scoring

TYPE = TaskType.NEW_CLIENT_ACTIVATION


def generate(manager_id: int, as_of: str, window_tag: str) -> list[Task]:
    rows = sig.new_clients_for_manager(manager_id, as_of)
    if not rows:
        return []
    due = datetime.now(UTC) + timedelta(days=get_settings().service_level_due_days * 2)

    tasks: list[Task] = []
    for r in rows:
        cid = int(r["client_id"])
        days = int(r["days_since_created"] or 0)
        n_orders = int(r["n_orders"] or 0)
        name = r.get("full_name") or r.get("name") or f"Client {cid}"

        u = scoring.new_client_urgency(days)
        # no monetary history yet; full confidence it's a genuinely new client
        prio = scoring.priority(u, 0.0, 1.0)
        order_note = "ще без замовлень" if n_orders == 0 else f"лише {n_orders} замовлення"

        tasks.append(Task(
            task_key=f"mgr:{manager_id}|client:{cid}|type:{TYPE.value}|win:{window_tag}",
            manager_id=manager_id, client_id=cid, client_name=name, task_type=TYPE,
            title="Активувати нового клієнта",
            reason=f"Новий клієнт ({days} дн), {order_note} — варто залучити до першої покупки",
            priority=prio, urgency=scoring.urgency_band(u),
            payload={"days_since_created": days, "n_orders": n_orders},
            signals={"days_since_created": days, "n_orders": n_orders},
            explanation=Explanation(
                factors=[f"клієнт доданий {days} дн тому", order_note,
                         "перша покупка закладає подальші повторні замовлення"],
                source_signal="new_client", confidence=1.0),
            contact=Contact(phone=r.get("phone"), email=r.get("email"),
                            preferred="phone" if r.get("phone") else "email"),
            due_date=due, ab_variant="newclient_v1",
        ))
    return tasks
