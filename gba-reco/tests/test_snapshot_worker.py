from __future__ import annotations

from datetime import datetime

import pytest

from app.core.config import get_settings
from app.data import cache
from app.data import sales_repository as repo
from app.services.recommendations import recommender, service, snapshot, worker

AS_OF = "2026-03-01"
A1 = "11111111-1111-4111-8111-111111111111"
A2 = "22222222-2222-4222-8222-222222222222"
A3 = "33333333-3333-4333-8333-333333333333"
A4 = "44444444-4444-4444-8444-444444444444"
A5 = "55555555-5555-4555-8555-555555555555"


@pytest.fixture(autouse=True)
def no_ubiquity(monkeypatch):
    monkeypatch.setattr(get_settings(), "ubiquity_exclude_pct", 1.0)


def _data(negatives=None) -> snapshot.SnapshotData:
    jan, feb = datetime(2026, 1, 10), datetime(2026, 2, 10)
    rows = [
        (1, A1, 100, jan, 10), (1, A1, 100, jan, 11), (1, A1, 101, feb, 10),
        (2, A2, 200, jan, 10), (2, A2, 200, jan, 11), (2, A2, 200, jan, 12), (2, A2, 201, feb, 13),
        (3, A3, 300, jan, 10), (3, A3, 300, jan, 11), (3, A3, 300, jan, 12), (3, A3, 300, jan, 13),
        (4, A4, 400, jan, 11), (4, A4, 400, jan, 12),
        (1, A1, 900, datetime(2026, 3, 2), 12),
    ]
    return snapshot.SnapshotData(
        as_of=AS_OF,
        floor="2025-01-01",
        rows=rows,
        synthetic=frozenset(),
        product_rows=[(pid, False, True, pid) for pid in (10, 11, 12, 13)],
        availability=[(10, 1, 5.0), (11, 1, 5.0), (12, 1, 5.0), (13, 1, 0.0), (12, 2, 3.0)],
        storages=[
            {"id": 1, "deleted": False, "defective": False, "locale": "uk",
             "for_resale": True, "is_resale": False},
            {"id": 2, "deleted": False, "defective": False, "locale": "uk",
             "for_resale": False, "is_resale": False},
        ],
        agreements=[(1, A1, 1, False), (1, A5, 2, True), (2, A2, 1, False),
                    (3, A3, 1, False), (4, A4, 1, False)],
        groups=[(10, 7), (11, 7), (12, 8), (13, 8)],
        negatives=negatives or {},
    )


def _ids(result):
    return [(r.product_id, r.source_detail.value) for r in result.recommendations]


def test_personal_list_ranks_history_then_neighbour_discovery():
    context = snapshot.Snapshot(_data()).context(1, None)
    result = context.personal_result(25)
    assert _ids(result) == [
        (10, "repurchase_history"), (11, "repurchase_history"), (12, "similar_clients"),
    ]
    assert result.segment == "LIGHT"
    assert result.as_of_date == AS_OF
    assert result.discovery_count == 1


def test_future_orders_and_out_of_stock_products_never_surface():
    context = snapshot.Snapshot(_data()).context(1, None)
    assert 12 not in context.owned
    assert 13 not in {r.product_id for r in context.personal_result(25).recommendations}
    assert _ids(context.copurchase_result(25)) == [(12, "copurchase")]


def test_negative_feedback_is_excluded_from_both_lists():
    context = snapshot.Snapshot(_data({1: frozenset({12})})).context(1, None)
    assert 12 not in {r.product_id for r in context.personal_result(25).recommendations}
    assert context.copurchase_result(25).count == 0
    other = snapshot.Snapshot(_data({1: frozenset({12})})).context(4, None)
    assert 12 in {r.product_id for r in other.personal_result(25).recommendations}


def test_agreement_scope_uses_its_own_history_and_stock_profile():
    snap = snapshot.Snapshot(_data())
    scoped = snap.context(1, A1).personal_result(25)
    assert scoped.client_agreement_net_id == A1
    assert [r.product_id for r in scoped.recommendations][:2] == [10, 11]
    empty = snap.context(1, A5)
    assert empty.frequency == {}
    assert {r.product_id for r in empty.personal_result(25).recommendations} == {12}
    assert snap.targets([1]) == [(1, None), (1, A1), (1, A5)]


def test_without_discovery_only_history_is_returned():
    result = snapshot.Snapshot(_data()).context(1, None).personal_result(25, include_discovery=False)
    assert _ids(result) == [(10, "repurchase_history"), (11, "repurchase_history")]


@pytest.fixture
def warm(monkeypatch):
    entries: dict[str, dict] = {}
    monkeypatch.setattr(cache, "health", lambda: True)
    monkeypatch.setattr(cache, "get", lambda key: entries.get(key))
    monkeypatch.setattr(cache, "set", lambda key, value, ttl=None: entries.__setitem__(key, value) or True)
    monkeypatch.setattr(worker, "active_clients", lambda *args: [1])
    monkeypatch.setattr(worker, "_check_identity_churn", lambda *args: None)
    monkeypatch.setattr(snapshot, "load", lambda as_of: snapshot.Snapshot(_data()))
    return entries


def test_snapshot_worker_writes_dated_and_latest_keys_and_resumes_by_token(warm, monkeypatch):
    result = worker.run(AS_OF, refresh_token="cycle-1", engine="snapshot")
    assert result["engine"] == "snapshot"
    assert result["contexts"] == result["ok"] == result["copurchase_ok"] == 3
    assert result["failed"] == result["copurchase_failed"] == 0
    for uid in (None, A1, A5):
        for key in (
            cache.make_key(1, AS_OF, 25, True, client_agreement_net_id=uid),
            cache.make_copurchase_key(1, AS_OF, 25, client_agreement_net_id=uid),
        ):
            assert warm[key]["_fleet_refresh"] == "cycle-1"
            assert warm[cache.to_latest(key, AS_OF)] == warm[key]
    dropped = cache.make_copurchase_key(1, AS_OF, 25, client_agreement_net_id=A1)
    warm.pop(dropped)
    again = worker.run(AS_OF, refresh_token="cycle-1", engine="snapshot")
    assert again["reused"] == 5
    assert again["failed"] == again["copurchase_failed"] == 0
    assert warm[dropped]["_fleet_refresh"] == "cycle-1"
    fresh = worker.run(AS_OF, refresh_token="cycle-2", engine="snapshot")
    assert fresh["reused"] == 0


def test_snapshot_worker_counts_failed_writes(warm, monkeypatch):
    monkeypatch.setattr(cache, "set", lambda *args, **kw: False)
    result = worker.run(AS_OF, engine="snapshot")
    assert result["ok"] == result["copurchase_ok"] == 0
    assert result["failed"] == result["copurchase_failed"] == 3


def test_latest_payload_is_served_when_the_dated_key_is_missing(warm, monkeypatch):
    worker.run(AS_OF, engine="snapshot")
    monkeypatch.setattr(repo, "client_exists", lambda cid: True)
    monkeypatch.setattr(repo, "active_agreements", lambda cid: [A1])
    monkeypatch.setattr(recommender, "recommend", lambda **kw: pytest.fail("must serve the latest payload"))
    monkeypatch.setattr(get_settings(), "serve_latest_fallback", True)
    served = service.get_recommendations(1, as_of_date="2026-03-02", top_n=25)
    assert served.cached is True
    assert served.as_of_date == AS_OF
    assert [r.product_id for r in served.recommendations] == [10, 11, 12]


def test_latest_payload_is_never_served_for_an_earlier_date_or_when_disabled(warm, monkeypatch):
    worker.run(AS_OF, engine="snapshot")
    key = cache.make_key(1, "2026-02-20", 25, True)
    monkeypatch.setattr(get_settings(), "serve_latest_fallback", True)
    assert service.read_cached(key, "2026-02-20") is None
    monkeypatch.setattr(get_settings(), "serve_latest_fallback", False)
    assert service.read_cached(cache.make_key(1, "2026-03-02", 25, True), "2026-03-02") is None


def test_dated_key_pattern_only_matches_dated_payload_keys():
    version = cache._MODEL_VERSION
    assert cache._DATED_KEY.match(f"reco:{version}:7:2026-03-01:25:1").groups() == (version, "2026-03-01")
    assert cache._DATED_KEY.match(f"copurchase:{version}:7:2026-03-01:25:a{A1}:s5")
    assert cache._DATED_KEY.match(f"reco:{version}:7:latest:25:1").group(2) == "latest"
    assert cache._DATED_KEY.match(f"reco:neg:{A1}") is None
    assert cache._DATED_KEY.match("reco:worker:active_clients") is None


def test_copurchase_seeds_are_ranked_by_frequency_then_recency_then_id():
    data = _data()
    data.rows += [(1, A1, 102, datetime(2026, 2, 20), 13), (1, A1, 103, datetime(2026, 2, 20), 12)]
    context = snapshot.Snapshot(data).context(1, None)
    assert context.seed_products() == [10, 12, 13, 11]


def test_multi_group_products_resolve_to_one_stable_group():
    data = _data()
    data.groups = [(10, 7), (10, 9), (11, 7), (12, 8), (13, 8)]
    assert snapshot.Snapshot(data).group[10] == 9


def test_ubiquity_window_is_anchored_to_as_of(monkeypatch):
    monkeypatch.setattr(get_settings(), "ubiquity_exclude_pct", 0.70)
    snap = snapshot.Snapshot(_data())
    assert snap.ubiquitous == frozenset({10, 11, 12})
    stale = _data()
    stale.rows = [(c, a, o, datetime(2025, 2, 1), p) for c, a, o, _, p in stale.rows]
    assert snapshot.Snapshot(stale).ubiquitous == frozenset()


def test_lists_carry_the_precision_measured_for_their_kind_and_length():
    from app.services.eval import measured

    context = snapshot.Snapshot(_data()).context(1, None)
    assert context.personal_result(25).precision_estimate == measured.precision("personal", 25)
    assert context.personal_result(10).precision_estimate == measured.precision("personal", 10)
    assert context.copurchase_result(25).precision_estimate == measured.precision("copurchase", 25)


def test_group_cap_and_discovery_source_are_parameters():
    data = _data()
    data.groups = [(10, 7), (11, 7), (12, 7), (13, 7)]
    snap = snapshot.Snapshot(data, snapshot.Params(repurchase_count=20, max_per_group=1))
    assert [r.product_id for r in snap.context(1, None).personal_result(25).recommendations][:1] == [10]
    assert 11 not in {
        r.product_id for r in snap.context(1, None).personal_result(25).recommendations
        if r.source.value == "repurchase"
    }
    snap.params = snapshot.Params(repurchase_count=20, max_per_group=12, discovery_source="copurchase")
    labels = {r.source_detail.value for r in snap.context(1, None).personal_result(25).recommendations}
    assert "similar_clients" not in labels


def test_latest_payload_is_marked_stale_and_expires_after_the_allowed_age(warm, monkeypatch):
    worker.run(AS_OF, engine="snapshot")
    monkeypatch.setattr(repo, "client_exists", lambda cid: True)
    monkeypatch.setattr(repo, "active_agreements", lambda cid: [A1])
    monkeypatch.setattr(get_settings(), "serve_latest_fallback", True)
    same_day = service.get_recommendations(1, as_of_date=AS_OF, top_n=25)
    assert (same_day.stale, same_day.as_of_date) == (False, AS_OF)
    next_day = service.get_recommendations(1, as_of_date="2026-03-03", top_n=25)
    assert (next_day.stale, next_day.cached, next_day.as_of_date) == (True, True, AS_OF)
    assert next_day.degraded is False
    assert get_settings().latest_fallback_max_age_days == 2
    key = cache.make_key(1, "2026-03-04", 25, True)
    assert service.read_cached(key, "2026-03-04") is None
    monkeypatch.setattr(get_settings(), "latest_fallback_max_age_days", 3)
    assert service.read_cached(key, "2026-03-04")["as_of_date"] == AS_OF


def _sibling_data() -> snapshot.SnapshotData:
    data = _data()
    data.rows.append((1, A1, 102, datetime(2026, 2, 20), 15))
    data.product_rows.append((15, False, True, 15))
    data.availability += [(14, 1, 4.0), (15, 1, 2.0)]
    return data


def test_sibling_card_with_stock_stands_in_for_the_bought_card():
    data = _sibling_data()
    data.availability = [row for row in data.availability if row[0] != 11]
    data.siblings = {11: 11, 14: 11}
    context = snapshot.Snapshot(data).context(1, None)
    result = context.personal_result(25)
    assert (14, "repurchase_history") in _ids(result)
    assert 11 not in {r.product_id for r in result.recommendations}
    assert 14 in context.owned_live


def test_one_card_per_article_and_a_sibling_of_a_bought_card_is_never_new():
    data = _sibling_data()
    data.siblings = {10: 10, 15: 10, 12: 12, 16: 12}
    data.availability.append((16, 1, 9.0))
    snap = snapshot.Snapshot(data)
    owner = snap.context(1, None).personal_result(25)
    ids = [r.product_id for r in owner.recommendations]
    assert len({snap.article.get(pid, pid) for pid in ids}) == len(ids)
    assert sum(pid in (10, 15) for pid in ids) == 1
    assert sum(pid in (12, 16) for pid in ids) == 1
    data.rows.append((1, A1, 103, datetime(2026, 2, 21), 16))
    data.product_rows.append((16, False, True, 16))
    bought_twin = snapshot.Snapshot(data).context(1, None)
    discovery = {
        r.product_id for r in bought_twin.personal_result(25).recommendations
        if r.source.value == "discovery"
    }
    assert not discovery & {12, 16}
    assert not {pid for pid, _ in bought_twin.cross_sell(25)} & {12, 16}


def test_products_in_an_open_order_are_left_out_for_that_client_only():
    data = _data()
    data.open_lines = [(1, 12)]
    data.siblings = {12: 12, 17: 12}
    data.availability.append((17, 1, 3.0))
    snap = snapshot.Snapshot(data)
    mine = snap.context(1, None)
    assert not {r.product_id for r in mine.personal_result(25).recommendations} & {12, 17}
    assert mine.copurchase_result(25).count == 0
    other = snap.context(4, None)
    assert 12 in {r.product_id for r in other.personal_result(25).recommendations}


def test_stock_on_a_not_for_sale_storage_is_not_sellable():
    data = _data()
    data.storages.append({"id": 3, "deleted": False, "defective": True, "locale": "uk",
                          "for_resale": True, "is_resale": True})
    data.availability = [row for row in data.availability if row[0] != 12] + [(12, 3, 50.0)]
    snap = snapshot.Snapshot(data)
    assert 12 not in snap.stock_global
    assert 12 not in snap.stock(A1)
    assert 12 not in {r.product_id for r in snap.context(1, None).personal_result(25).recommendations}


def test_sibling_groups_follow_the_server_rule():
    auto = [(1, "A-1", "Фільтр"), (2, "A-1", "Фільтр"), (3, "A-1", "Трос"),
            (4, "B-2", "Кран"), (5, "B-2", "Кран"), (6, "C-3", "Замок")]
    manual = [("B-2 | Z-9", 5), ("B-2 | Z-9", 9), ("lonely", 10)]
    articles = repo.group_siblings(auto, manual)
    assert articles == {1: 1, 2: 1, 4: 4, 5: 4, 9: 4}
    assert repo.with_siblings({2, 7}, articles) == {1, 2, 7}
    assert repo.with_siblings(set(), articles) == set()


def test_a_purchase_under_another_agreement_is_never_labelled_new():
    data = _data()
    data.rows.append((1, A5, 150, datetime(2026, 2, 15), 12))
    snap = snapshot.Snapshot(data)
    first = snap.context(1, A1)
    assert 12 not in first.owned_live and 12 in first.client_owned_live
    assert (12, "repurchase_history") in _ids(first.personal_result(25))
    assert first.personal_result(25).discovery_count == 0
    assert first.copurchase_result(25).count == 0


def test_service_cards_are_never_recommended():
    data = _data()
    data.services = frozenset({12})
    snap = snapshot.Snapshot(data)
    for cid in (1, 4):
        context = snap.context(cid, None)
        assert 12 not in {r.product_id for r in context.personal_result(25).recommendations}
        assert 12 not in {pid for pid, _ in context.cross_sell(25)}
