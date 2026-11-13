from datetime import UTC, datetime

import mongomock
import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.services import repeat_sales as repeat

AS_OF = "2026-10-22"
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
UID = "11111111-1111-1111-1111-111111111111"


def purchase(cid=1, pid=10, last="2026-10-08", cycle=14, stock=20, n=3, **extra):
    return {"client_id": cid, "product_id": pid, "last_buy": last, "first_buy": "2026-09-10",
            "previous_buy": "2026-09-24", "third_buy": "2026-09-10", "n_orders": n,
            "cycle_days": cycle, "last_quantity": 4, "available_qty": stock,
            "product_name": "Товар", "product_net_uid": None, **extra}


def client(cid=1, count=3, name="Клієнт"):
    return {"client_id": cid, "name": name, "retained_orders_cnt": count, "orders_cnt": count,
            "last_order": None, "turnover_eur": 10}


@pytest.fixture
def sources(monkeypatch):
    state = {"products": [], "churn": []}
    collection = mongomock.MongoClient(tz_aware=True).db.contacts
    monkeypatch.setattr(repeat.mongo, "seller_contact_events", lambda: collection)
    monkeypatch.setattr(repeat.sig, "_excluded", lambda *a: frozenset())
    monkeypatch.setattr(repeat.sig, "repeat_products_for_manager", lambda *a, **kw: state["products"])
    monkeypatch.setattr(repeat.sig, "churn_candidates_for_manager", lambda *a: state["churn"])
    return state, collection


def page(rows, **kw):
    return repeat.enrich_and_page(1, AS_OF, rows, now=NOW, **kw)


def test_regular_seasonal_new_short_already_ordered_and_stock_evidence(sources):
    state, _ = sources
    state["products"] = [purchase(1), purchase(2, last="2026-08-01", cycle=180),
                         purchase(4, cycle=None, n=2), purchase(5, last=AS_OF),
                         purchase(6, stock=0), purchase(7, stock=None),
                         purchase(8, last="2026-10-01")]
    state["churn"] = [{"client_id": 2}, {"client_id": 8}]
    result = page([client(cid, count=0 if cid == 3 else 3) for cid in range(1, 9)])
    rows = {r["client_id"]: r for r in result["clients"]}
    assert rows[1]["segments"] == ["reorder_due"]
    assert rows[2]["segments"] == ["reactivation"]  # seasonal average not due yet
    assert rows[3]["segments"] == ["new_client"]
    assert rows[3]["new_client_basis"] == "no_retained_purchase_in_available_history"
    assert rows[3]["repeat_evidence"]["products"] == []
    assert rows[3]["repeat_evidence"]["history_start"] == "2025-01-01"
    assert rows[4]["segments"] == []
    assert rows[4]["repeat_evidence"]["products"][0]["next_expected_on"] is None
    assert rows[5]["segments"] == []  # a retained order today resets the cycle
    assert rows[6]["repeat_evidence"]["products"][0]["availability_status"] == "out_of_stock"
    assert rows[6]["repeat_evidence"]["products"][0]["available_qty"] == 0
    assert rows[7]["repeat_evidence"]["products"][0]["availability_status"] == "unknown"
    assert rows[7]["repeat_evidence"]["products"][0]["available_qty"] is None
    assert rows[8]["segments"] == ["reorder_overdue", "reactivation"]
    assert result["segment_counts"] == {"all": 8, "reorder_due": 3, "reorder_overdue": 1,
                                        "reactivation": 2, "new_client": 1}
    evidence = rows[1]["repeat_evidence"]["products"][0]
    assert evidence["recent_purchase_dates"] == ["2026-10-08", "2026-09-24", "2026-09-10"]
    assert evidence["next_expected_on"] == AS_OF and evidence["last_quantity"] == 4


def test_counts_search_and_segments_precede_pagination(sources):
    state, _ = sources
    state["products"] = [purchase(cid, last="2026-10-01") for cid in range(1, 122)]
    rows = [client(cid, name=f"Компанія {cid}") for cid in range(1, 122)]
    rows.append(client(122, name="Інша"))
    result = page(rows, segment="reorder_overdue", search="  Компанія  ", offset=100, limit=20)
    assert result["total"] == result["population_count"] == 121
    assert result["count"] == 20 and result["clients"][0]["client_id"] == 101
    assert result["segment_counts"]["reorder_overdue"] == 121
    assert result["search"] == "Компанія"
    assert page(rows, offset=999)["clients"] == []


def test_contacts_scheduled_current_cycle_no_answer_and_next_cycle(sources):
    state, contacts = sources
    state["products"] = [purchase(cid, last="2026-10-01", cycle=14) for cid in range(1, 6)]
    for cid, result, at, next_at in [
        (1, "reached", "2026-10-20T09:00:00+00:00", "2026-10-30T09:00:00+00:00"),
        (2, "proposal", "2026-10-20T09:00:00+00:00", None),
        (3, "no_answer", "2026-10-20T09:00:00+00:00", None),
        (4, "reached", "2026-10-10T09:00:00+00:00", None),
        (5, "reached", "2026-10-23T09:00:00+00:00", None),
    ]:
        contacts.insert_one({"client_id": cid, "manager_id": 99, "result": result,
                             "at": datetime.fromisoformat(at), "event_id": str(cid),
                             "next_contact_at": datetime.fromisoformat(next_at) if next_at else None,
                             "next_action": "Подзвонити" if next_at else None})
    rows = {r["client_id"]: r for r in page([client(cid) for cid in range(1, 6)])["clients"]}
    assert rows[1]["segments"] == rows[2]["segments"] == []
    assert rows[1]["contact_cooldown"]["reason"] == "scheduled_contact"
    assert rows[2]["contact_cooldown"]["reason"] == "contacted_current_cycle"
    assert rows[3]["segments"] == rows[4]["segments"] == rows[5]["segments"] == ["reorder_overdue"]
    # Refresh sees the same persisted contact even after manager transfer; no task mutation required.
    assert page([client(2)])["clients"][0]["segments"] == []
    next_cycle = repeat.enrich_and_page(1, "2026-10-30", [client(2)], now=NOW)
    assert next_cycle["clients"][0]["segments"] == ["reorder_overdue"]


def test_short_coverage_and_source_failures_are_not_empty_success(sources, monkeypatch):
    result = repeat.enrich_and_page(1, "2025-02-01", [client(1, 0)], now=NOW)
    evidence = result["clients"][0]["repeat_evidence"]
    assert evidence["history_complete"] is False and evidence["history_start"] == "2025-01-01"
    monkeypatch.setattr(repeat.sig, "repeat_products_for_manager", lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("sql_unavailable")))
    with pytest.raises(RuntimeError, match="sql_unavailable"):
        page([client()])


def test_endpoint_paging_validation_inclusive_day_and_failure(sources, monkeypatch):
    calls = []
    monkeypatch.setattr(main.signals_repository, "manager_id_for_netuid", lambda uid: 1)
    monkeypatch.setattr(main.repeat_sales, "read_snapshot",
                        lambda mid, day: calls.append(day) or ([client(1, 0), client(2, 0)], [], set()))
    monkeypatch.setattr(main.signals_repository, "overdue_debts_for_manager", lambda *a, **kw: [])
    api = TestClient(main.app)
    params = {"manager_net_uid": UID, "as_of_date": AS_OF, "limit": 1, "offset": 1, "segment": "new_client"}
    response = api.get("/cockpit/clients", params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 1 and body["total"] == body["population_count"] == 2
    assert body["clients"][0]["client_id"] == 2 and calls == [AS_OF]
    assert body["as_of"] == AS_OF
    assert body["turnover_basis"] == "gross_invoiced_returns_not_subtracted"
    assert api.get("/cockpit/clients", params={**params, "segment": "bogus"}).status_code == 422
    assert api.get("/cockpit/clients", params={**params, "limit": 201}).status_code == 422
    monkeypatch.setattr(repeat, "_latest_contacts",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("mongo_down")))
    failed = api.get("/cockpit/clients", params=params)
    assert failed.status_code == 500 and failed.json()["detail"] == "cockpit_clients_failed"


def test_explicit_contact_deadline_expiry_overrides_inferred_cycle_cooldown():
    products = [repeat.product_evidence(purchase(last="2026-10-01"), NOW.date(), True)]
    event = {"at": datetime(2026,10,20,9,tzinfo=UTC), "result":"proposal",
             "next_contact_at": datetime(2026,10,22,11,tzinfo=UTC), "event_id":"event"}
    assert repeat._cooldown(event,products,NOW,NOW.date())["active"] is False
