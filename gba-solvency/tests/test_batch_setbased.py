from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.core import serving_context
from app.data import cache
from app.data import solvency_repository as repo_module
from app.domain.models import CapType, DataSufficiency, Rating, Risk90dBand
from app.risk.dataset import FEATURE_COLUMNS
from app.services.solvency import service

AS_OF = "2026-06-01"
GATE = repo_module.ClientGate


def _fail(*_args, **_kwargs):
    raise AssertionError("per-client lookup reached from the batch path")


def _features(**overrides: float) -> dict[str, float]:
    features = dict.fromkeys(FEATURE_COLUMNS, 0.0)
    features.update({"recency_days": 10.0, "grace_days": 14.0})
    features.update(overrides)
    return features


@pytest.fixture(autouse=True)
def _fixed_serving_day(monkeypatch):
    monkeypatch.setattr(
        serving_context, "utc_now", lambda: datetime(2026, 6, 1, 9, tzinfo=UTC)
    )


@pytest.fixture
def world(monkeypatch):
    """A fake source: clients 1..3 are buyers, 4 is a provider, 5 is in the hopeless group."""
    state = {
        "gates": {
            1: GATE(True, False), 2: GATE(True, False), 3: GATE(True, False),
            4: GATE(False, False), 5: GATE(True, True),
        },
        "features": {
            1: _features(),
            2: _features(total_debt_eur=500.0, overdue_eur_1_30=500.0, n_open_debt_lines=2.0),
            3: _features(recency_days=3000.0, grace_days=0.0),
            5: _features(),
        },
        "currency": {1: [], 2: [{"currency_id": 2, "turnover_eur": 10}], 3: [], 5: []},
        "store": {},
        "calls": {"gates": 0, "mget": 0, "features_many": 0, "currency_many": 0, "set_many": 0},
    }
    state["currency"][1] = [
        {"currency_id": 2, "turnover_eur": 10}, {"currency_id": 3, "turnover_eur": 5},
    ]

    def client_gates(ids):
        state["calls"]["gates"] += 1
        return {cid: state["gates"][cid] for cid in ids if cid in state["gates"]}

    def get_many(keys):
        state["calls"]["mget"] += 1
        return [state["store"].get(key) for key in keys]

    def set_many(items):
        state["calls"]["set_many"] += 1
        state["store"].update(items)

    def features_many(ids, *_a):
        state["calls"]["features_many"] += 1
        return {cid: state["features"][cid] for cid in ids}

    def currency_many(ids, *_a):
        state["calls"]["currency_many"] += 1
        return {cid: state["currency"][cid] for cid in ids}

    monkeypatch.setattr(service.repo, "client_gates", client_gates)
    monkeypatch.setattr(service.repo, "client_exists", lambda cid: cid in state["gates"])
    monkeypatch.setattr(service.repo, "has_buyer_role", lambda cid: state["gates"][cid].is_buyer)
    monkeypatch.setattr(
        service.repo, "has_hopeless_debt_role", lambda cid: state["gates"][cid].hopeless_debt
    )
    monkeypatch.setattr(service.repo, "turnover_eur_by_currency_many", currency_many)
    monkeypatch.setattr(
        service.repo, "turnover_eur_by_currency", lambda cid, *_a: state["currency"][cid]
    )
    monkeypatch.setattr(service.risk_dataset, "features_many", features_many)
    monkeypatch.setattr(
        service.risk_dataset, "features_one", lambda cid, *_a: state["features"][cid]
    )
    monkeypatch.setattr(service.cache, "get_many", get_many)
    monkeypatch.setattr(service.cache, "set_many", set_many)
    monkeypatch.setattr(service.cache, "get", lambda key: state["store"].get(key))
    monkeypatch.setattr(
        service.cache, "set", lambda key, value: state["store"].__setitem__(key, value)
    )
    return state


def test_batch_resolves_gate_and_cache_in_one_round_trip_each(world, monkeypatch):
    for name in ("client_exists", "has_buyer_role", "has_hopeless_debt_role",
                 "turnover_eur_by_currency"):
        monkeypatch.setattr(service.repo, name, _fail)
    monkeypatch.setattr(service.cache, "get", _fail)
    monkeypatch.setattr(service.cache, "set", _fail)
    monkeypatch.setattr(service.risk_dataset, "features_one", _fail)

    results, errors = service.score_batch([3, 99, 1, 4, 2, 5], None, 12, True)

    assert [r.client_id for r in results] == [3, 1, 4, 2, 5]
    assert errors == [{"client_id": 99, "error": "client_id not found: 99"}]
    assert world["calls"] == {
        "gates": 1, "mget": 1, "features_many": 1, "currency_many": 1, "set_many": 1,
    }
    assert len(world["store"]) == 4

    results_again, errors_again = service.score_batch([3, 99, 1, 4, 2, 5], None, 12, True)
    assert [r.model_dump() for r in results_again] == [r.model_dump() for r in results]
    assert errors_again == errors
    assert world["calls"]["features_many"] == 1 and world["calls"]["currency_many"] == 1
    assert world["calls"]["gates"] == 2 and world["calls"]["mget"] == 2


@pytest.mark.parametrize("use_cache", [False, True])
def test_batch_equals_single_for_every_kind_of_client(world, use_cache):
    ids = [1, 2, 3, 4, 5]
    batch, errors = service.score_batch(ids, None, 12, use_cache)
    singles = [service.score_client(cid, None, None, 12, use_cache) for cid in ids]

    assert errors == []
    assert [r.model_dump(mode="json") for r in batch] == [
        r.model_dump(mode="json") for r in singles
    ]
    by_id = {r.client_id: r for r in batch}
    assert by_id[1].currency_breakdown is not None and len(by_id[1].currency_breakdown) == 2
    assert by_id[2].currency_breakdown is None
    assert by_id[2].risk_90d.band == Risk90dBand.HIGH
    assert by_id[3].data_sufficiency == DataSufficiency.INSUFFICIENT
    assert by_id[4].applicable is False
    with pytest.raises(LookupError, match="client_id not found: 99"):
        service.score_client(99, None, None, 12, use_cache)
    assert service.score_batch([99], None, 12, use_cache)[1] == [
        {"client_id": 99, "error": "client_id not found: 99"}
    ]


def test_batch_without_cache_never_touches_redis(world, monkeypatch):
    monkeypatch.setattr(service.cache, "get_many", _fail)
    results, _ = service.score_batch([1, 2], None, 12, False)
    assert len(results) == 2 and world["store"] == {} and world["calls"]["set_many"] == 1


def test_hopeless_debt_group_gets_fixed_policy_rating(world):
    clean = service.score_client(1, None, None, 12, False)
    hopeless = service.score_client(5, None, None, 12, False)

    assert (clean.score, clean.rating, clean.policy_reason_code) == (100, Rating.A, None)
    assert (hopeless.score, hopeless.rating, hopeless.pd) == (0, Rating.D, 1.0)
    assert hopeless.policy_reason_code == "hopeless_debt_role"
    assert hopeless.policy_reason == "Клієнт у групі безнадійної заборгованості"
    assert hopeless.caps_applied == [CapType.HOPELESS_DEBT_ROLE]
    assert hopeless.applicable is True and hopeless.data_sufficiency == DataSufficiency.OK
    assert hopeless.contributions == []
    assert hopeless.risk_90d is not None
    assert hopeless.forward_risk_reason == "replaced_by_operational_90d"
    assert hopeless.current_model_run_id == clean.current_model_run_id


def test_hopeless_debt_policy_overrides_insufficient_data(world):
    world["features"][5] = world["features"][3]
    assert service.score_client(3, None, None, 12, False).score is None
    for result in (
        service.score_client(5, None, None, 12, False),
        service.score_batch([5], None, 12, False)[0][0],
    ):
        assert (result.score, result.rating) == (0, Rating.D)
        assert result.data_sufficiency == DataSufficiency.OK


@pytest.mark.parametrize("path", ["single", "batch"])
def test_cached_score_is_dropped_when_policy_group_changes(world, path):
    def score(cid):
        if path == "single":
            return service.score_client(cid, None, None, 12, True)
        return service.score_batch([cid], None, 12, True)[0][0]

    assert score(1).rating == Rating.A
    world["gates"][1] = GATE(True, True)
    moved_in = score(1)
    assert moved_in.rating == Rating.D and moved_in.policy_reason_code == "hopeless_debt_role"
    world["gates"][1] = GATE(True, False)
    moved_out = score(1)
    assert moved_out.rating == Rating.A and moved_out.policy_reason_code is None


def test_cache_entry_written_before_policy_fields_existed_still_hydrates(world):
    first = service.score_client(1, None, None, 12, True)
    key = next(iter(world["store"]))
    for field in ("policy_reason_code", "policy_reason"):
        world["store"][key].pop(field)
    world["features"][1] = _features(total_debt_eur=900.0)
    assert service.score_client(1, None, None, 12, True) == first


def _headers() -> dict[str, str]:
    key = main.settings.internal_api_key
    return {"X-Internal-Api-Key": key} if key else {}


def test_slim_batch_is_opt_in_and_drops_explainability(world, monkeypatch):
    monkeypatch.setattr(main, "_service", lambda: service)
    client = TestClient(main.app)
    ids = [1, 2, 3, 4, 5, 99]

    full = client.post("/score/batch", json={"client_ids": ids, "use_cache": False},
                       headers=_headers())
    slim = client.post("/score/batch", json={"client_ids": ids, "use_cache": False, "slim": True},
                       headers=_headers())
    explicit = client.post(
        "/score/batch", json={"client_ids": ids, "use_cache": False, "slim": False},
        headers=_headers(),
    )

    assert full.status_code == slim.status_code == 200
    assert explicit.json() == full.json()
    full_body, slim_body = full.json(), slim.json()
    assert "slim" not in full_body and "meta" not in full_body
    assert set(full_body) == {"results", "errors", "count", "failed"}
    assert all("contributions" in row and "model_version" in row for row in full_body["results"])

    assert slim_body["slim"] is True
    assert (slim_body["count"], slim_body["failed"]) == (5, 1)
    assert slim_body["errors"] == full_body["errors"]
    slim_fields = {
        "client_id", "applicable", "score", "rating", "pd", "risk_90d", "data_sufficiency",
        "data_sufficiency_reason", "caps_applied", "policy_reason_code", "policy_reason",
    }
    for slim_row, full_row in zip(slim_body["results"], full_body["results"], strict=True):
        assert set(slim_row) == slim_fields
        assert slim_row == {field: full_row[field] for field in slim_fields}
    first = full_body["results"][0]
    assert slim_body["meta"] == {
        field: first[field]
        for field in ("state_basis", "business_timezone", "fx_date", "as_of_date",
                      "window_months", "model_version", "source_history_start",
                      "effective_start", "history_complete")
    }
    assert len(slim.content) < len(full.content) / 2


def test_slim_batch_with_no_scored_rows_has_null_meta(world, monkeypatch):
    monkeypatch.setattr(main, "_service", lambda: service)
    response = TestClient(main.app).post(
        "/score/batch", json={"client_ids": [99], "slim": True}, headers=_headers()
    )
    assert response.json() == {
        "results": [], "count": 0, "failed": 1, "slim": True, "meta": None,
        "errors": [{"client_id": 99, "error": "client_id not found: 99"}],
    }


def test_set_based_queries_stay_under_the_parameter_limit(monkeypatch):
    statements: list[int] = []

    def fake_query(sql, params=None):
        statements.append(len(params or {}))
        assert sql.count(":cid") == sum(1 for name in params if name.startswith("cid"))
        return []

    monkeypatch.setattr(repo_module, "query", fake_query)
    monkeypatch.setattr(repo_module, "synthetic_product_ids", lambda: {1, 2})
    ids = list(range(1, 2501))

    assert repo_module.client_gates(ids) == {}
    turnover = repo_module.turnover_eur_by_currency_many(ids, AS_OF, 12, AS_OF)

    assert len(statements) == 6
    assert max(statements) <= 1000
    assert turnover == {cid: [] for cid in ids}


def test_client_gates_maps_rows_and_omits_unknown_ids(monkeypatch):
    monkeypatch.setattr(repo_module, "query", lambda *_a: [
        {"client_id": 7, "is_buyer": 1, "hopeless_debt": 0},
        {"client_id": 8, "is_buyer": 0, "hopeless_debt": 0},
        {"client_id": 9, "is_buyer": 1, "hopeless_debt": 1},
    ])
    assert repo_module.client_gates([7, 8, 9, 10, 7]) == {
        7: GATE(True, False), 8: GATE(False, False), 9: GATE(True, True),
    }


class _FakeRedis:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.round_trips = 0

    def mget(self, keys):
        self.round_trips += 1
        return [self.data.get(key) for key in keys]

    def pipeline(self, transaction=False):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, owner: _FakeRedis):
        self.owner = owner
        self.pending: list[tuple[str, int, str]] = []

    def setex(self, key, ttl, value):
        self.pending.append((key, ttl, value))

    def execute(self):
        self.owner.round_trips += 1
        for key, _ttl, value in self.pending:
            self.owner.data[key] = value


def test_cache_get_many_and_set_many_use_one_round_trip(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(cache, "_get_client", lambda: fake)

    cache.set_many({"a": {"v": 1}, "b": {"v": 2}})
    fake.data["broken"] = "{not json"
    assert cache.get_many(["b", "missing", "a", "broken"]) == [{"v": 2}, None, {"v": 1}, None]
    assert fake.round_trips == 2
    assert json.loads(fake.data["a"]) == {"v": 1}
    assert cache.get_many([]) == [] and fake.round_trips == 2


def test_cache_get_many_degrades_to_misses_without_redis(monkeypatch):
    monkeypatch.setattr(cache, "_get_client", lambda: None)
    assert cache.get_many(["a", "b"]) == [None, None]
    cache.set_many({"a": {}})
