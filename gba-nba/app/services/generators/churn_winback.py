"""Generator: churn_winback — clients whose order rate dropped sharply → re-engage tasks."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.data import signals_repository as sig
from app.domain.models import Contact, Explanation, Task, TaskType
from app.services import scoring

TYPE = TaskType.CHURN_WINBACK


def generate(manager_id: int, as_of: str, window_tag: str) -> list[Task]:
    rows = sig.churn_candidates_for_manager(manager_id, as_of)
    if not rows:
        return []
    client_ids = [int(r["client_id"]) for r in rows]
    contacts = sig.contacts_for_clients(client_ids)
    monetary = sig.client_monetary(client_ids, as_of)
    due = datetime.now(UTC) + timedelta(days=get_settings().service_level_due_days * 2)

    tasks: list[Task] = []
    for r in rows:
        cid = int(r["client_id"])
        recent = int(r["recent_orders"] or 0)
        prior = int(r["prior_orders"] or 0)
        silence = int(r["silence_days"] or 0)
        drop_ratio = (recent / prior) if prior else 0.0

        u = scoring.churn_urgency(drop_ratio, silence)
        v = scoring.value_from_monetary(monetary.get(cid, 0.0))
        conf = min(1.0, 0.5 + 0.05 * prior)  # more baseline history → more confident it's real churn
        prio = scoring.priority(u, v, conf)

        c = contacts.get(cid, {})
        name = c.get("full_name") or c.get("name") or f"Client {cid}"
        drop_pct = int(round((1 - drop_ratio) * 100))

        tasks.append(Task(
            task_key=f"mgr:{manager_id}|client:{cid}|type:{TYPE.value}|win:{window_tag}",
            manager_id=manager_id, client_id=cid, client_name=name, task_type=TYPE,
            title="Утримати клієнта: активність впала",
            reason=f"Замовлення впали на ~{drop_pct}% (було {prior} → стало {recent}); "
                   f"мовчання {silence} дн",
            priority=prio, urgency=scoring.urgency_band(u),
            payload={"churn": {"recent_orders": recent, "prior_orders": prior,
                              "drop_ratio": round(drop_ratio, 2), "silence_days": silence}},
            signals={"drop_ratio": round(drop_ratio, 2), "silence_days": silence,
                     "monetary": monetary.get(cid, 0.0)},
            explanation=Explanation(
                factors=[f"активність впала на ~{drop_pct}% ({prior}→{recent} замовлень)",
                         f"не замовляв {silence} дн",
                         f"річний оборот {monetary.get(cid, 0.0):.0f}"],
                source_signal="churn", confidence=conf),
            contact=Contact(phone=c.get("phone"), email=c.get("email"),
                            preferred="phone" if c.get("phone") else "email"),
            due_date=due, ab_variant="churn_v1",
        ))
    return tasks
