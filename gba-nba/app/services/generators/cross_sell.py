"""Generator: cross_sell — products gba-reco suggests that the client doesn't buy yet.

Only fires for clients with enough history (reco is meaningless for cold-start). Uses the
reco service's DISCOVERY items (new-to-client products). Degrades gracefully if reco is down.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.clients import reco_client
from app.core.config import get_settings
from app.data import signals_repository as sig
from app.domain.models import Contact, Explanation, Task, TaskType
from app.services import scoring

TYPE = TaskType.CROSS_SELL
_MAX_PRODUCTS = 5
_MIN_SCORE = 0.05
_RECO_REQUEST_N = 25


def generate(manager_id: int, as_of: str, window_tag: str) -> list[Task]:
    if not reco_client.is_healthy():
        return []  # reco offline → no cross-sell this run (graceful)

    s = get_settings()
    clients = sig.active_clients_for_manager(
        manager_id, as_of, recent_days=s.cross_sell_recent_days, min_orders=s.cross_sell_min_orders)
    if not clients:
        return []
    client_ids = [int(c["client_id"]) for c in clients]
    feats = sig.client_features(client_ids, as_of)
    clients.sort(key=lambda c: (feats.get(int(c["client_id"]), {}).get("monetary") or 0.0),
                 reverse=True)
    clients = clients[:s.cross_sell_max_clients]
    due = datetime.now(UTC) + timedelta(days=s.service_level_due_days * 3)

    tasks: list[Task] = []
    for c in clients:
        cid = int(c["client_id"])
        recs = reco_client.recommend(cid, top_n=_RECO_REQUEST_N, as_of_date=as_of,
                                     path="/recommend/copurchase",
                                     timeout=s.reco_crosssell_timeout)
        # cross-sell = NEW products (discovery), not repurchase
        discovery = [r for r in recs if r.get("source") == "discovery" and r.get("score", 0) >= _MIN_SCORE]
        if not discovery:
            continue
        discovery = discovery[:_MAX_PRODUCTS]
        pids = [int(r["product_id"]) for r in discovery]
        names = sig.product_names(pids)
        top = discovery[0]
        top_name = names.get(int(top["product_id"]), f"товар {top['product_id']}")
        cf = feats.get(cid, {})
        monetary_cid = float(cf.get("monetary") or 0.0)
        top_score = float(top.get("score", 0))

        u = scoring.crosssell_urgency(top_score)
        conf = top_score
        # priority = 100 * P(buy a discovered product | task) from the model (top_score + candidate
        # count + shared client features)
        feat = {"top_score": round(top_score, 3), "candidates": len(discovery),
                "monetary": monetary_cid, "recency_days": cf.get("recency_days"),
                "order_count": cf.get("order_count", 0)}
        prio, p_out, ev, ev_score = scoring.score_task_priority(TYPE.value, feat)

        name = c.get("full_name") or c.get("name") or f"Client {cid}"
        products = [{"product_id": int(r["product_id"]), "name": names.get(int(r["product_id"]), ""),
                     "score": round(float(r.get("score", 0)), 3), "source": "cross_sell"}
                    for r in discovery]

        tasks.append(Task(
            task_key=f"mgr:{manager_id}|client:{cid}|type:{TYPE.value}|win:{window_tag}",
            manager_id=manager_id, client_id=cid, client_name=name, task_type=TYPE,
            title="Допродаж: нові товари для клієнта",
            reason=f"Рекомендуємо «{top_name}»" + (f" та ще {len(products) - 1}" if len(products) > 1 else "")
                   + " — беруть схожі клієнти, у цього ще немає",
            priority=prio, p_outcome=p_out, expected_value=ev, ev_score=ev_score,
            urgency=scoring.urgency_band(u),
            payload={"products": products},
            signals={"top_score": round(top_score, 3), "candidates": len(discovery),
                     "monetary": monetary_cid, "recency_days": cf.get("recency_days"),
                     "order_count": cf.get("order_count", 0)},
            explanation=Explanation(
                factors=[f"«{top_name}» рекомендовано (score {float(top.get('score', 0)):.2f})",
                         f"{len(discovery)} нових товарів від моделі рекомендацій",
                         "купують схожі клієнти, у цього клієнта ще немає"],
                source_signal="reco_discovery", confidence=conf),
            contact=Contact(phone=c.get("phone"), email=c.get("email"),
                            preferred="phone" if c.get("phone") else "email"),
            due_date=due, ab_variant="crosssell_v1",
        ))
    return tasks
