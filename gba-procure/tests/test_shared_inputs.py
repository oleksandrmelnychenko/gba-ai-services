"""One set-based input read per run must reproduce the per-producer plans exactly."""

import pytest
from test_purchase_cost_cohort import make_cohort

from app.data import masters
from app.services.replenishment import inputs as inputs_mod
from app.services.replenishment import policy, worker

AS_OF = "2026-09-08"
PRODUCTS = {7: [1, 2, 3], 8: [3, 4], 9: []}
ON_HAND = {1: 4.0, 3: 1.0, 4: 50.0}
RESERVED = {1: 1.0}
ON_ORDER = {2: 1.0, 4: 1.0, 99: 5.0}
DEMAND = {
    pid: [{"d": f"2026-08-{day:02d}", "units": units} for day in range(1, 29)]
    for pid, units in ((1, 2), (2, 1), (3, 3), (4, 0.5))
}
SALES = {1: 12.0, 2: 9.0, 3: 30.0, 4: 2.0}
LEAD = {7: [10.0, 14.0, 12.0], 8: []}
CURRENCY = {8: 3}
NAMES = {7: "Seven", 8: "", 9: None}
TERMS = {7: {2: {"producer_id": 7, "product_id": 2, "moq": 50, "order_multiple": 10}}}
PROFILES = {8: {"producer_id": 8, "lead_time_override_days": 21}}
FACTORS = {7: {"C": 1.2}}


@pytest.fixture
def fake_sources(monkeypatch):
    calls: dict[str, int] = {}

    def counted(name, fn):
        def wrapper(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return fn(*args, **kwargs)

        return wrapper

    def pick(source):
        return lambda ids, *rest: {pid: source[pid] for pid in ids if pid in source}

    repo = policy.repo
    settings = policy.get_settings().model_copy(
        update={"use_masters": True, "use_feedback": True, "lead_time_min_samples": 3}
    )
    for module in (policy, worker, inputs_mod, policy.lead_time_svc):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(repo, "all_producers", counted("all_producers", lambda *a: [7, 8]))
    monkeypatch.setattr(repo, "manufacturer_producers", lambda: [9])
    monkeypatch.setattr(repo, "producer_names", lambda ids: {7: "Seven"})
    monkeypatch.setattr(repo, "producer_names_raw", counted("names_raw", lambda ids: dict(NAMES)))
    monkeypatch.setattr(repo, "producer_name", counted("producer_name", lambda pid: NAMES.get(pid)))
    monkeypatch.setattr(repo, "on_hand", counted("on_hand", pick(ON_HAND)))
    monkeypatch.setattr(repo, "reserved", counted("reserved", pick(RESERVED)))
    monkeypatch.setattr(repo, "on_order", counted("on_order", pick(ON_ORDER)))
    monkeypatch.setattr(repo, "on_order_all", counted("on_order_all", lambda as_of: dict(ON_ORDER)))
    monkeypatch.setattr(repo, "product_daily_demand_bulk", counted("demand", pick(DEMAND)))
    monkeypatch.setattr(repo, "product_meta", lambda ids: {})
    monkeypatch.setattr(
        repo, "products_for_producer", counted("products_for_producer", lambda pid, *a: PRODUCTS[pid])
    )
    monkeypatch.setattr(
        repo,
        "products_by_producer_all",
        counted("products_all", lambda *a, **k: {k_: v for k_, v in PRODUCTS.items() if v}),
    )
    monkeypatch.setattr(
        repo, "producer_lead_times", counted("lead_times", lambda pid, *a: list(LEAD.get(pid, [])))
    )
    monkeypatch.setattr(
        repo, "producer_lead_times_all", counted("lead_times_all", lambda *a, **k: dict(LEAD))
    )
    monkeypatch.setattr(
        repo, "producer_agreement_currency", counted("currency", lambda pid, *a: CURRENCY.get(pid))
    )
    monkeypatch.setattr(
        repo, "producer_agreement_currencies_all", counted("currency_all", lambda *a, **k: dict(CURRENCY))
    )
    monkeypatch.setattr(policy.cost_repo, "sale_prices_eur", counted("sales", pick(SALES)))
    monkeypatch.setattr(policy.classify_svc, "get_abc_map", counted("abc", lambda *a: {1: "A", 3: "B"}))
    monkeypatch.setattr(
        masters, "producer_profile", counted("profile", lambda pid, **k: PROFILES.get(pid))
    )
    monkeypatch.setattr(
        masters,
        "product_terms_for",
        counted("terms", lambda pid, ids, **k: {p: t for p, t in TERMS.get(pid, {}).items() if p in ids}),
    )
    monkeypatch.setattr(
        masters, "producer_profiles_all", counted("profiles_all", lambda **k: dict(PROFILES))
    )
    monkeypatch.setattr(masters, "product_terms_all", counted("terms_all", lambda **k: dict(TERMS)))
    monkeypatch.setattr(masters, "ping", lambda: True)
    monkeypatch.setattr(masters, "require_read_access", lambda: None)
    monkeypatch.setattr(
        policy.feedback, "learned_factors", counted("factors", lambda pid, *a: FACTORS.get(pid, {}))
    )
    monkeypatch.setattr(
        policy.feedback, "learned_factors_all", counted("factors_all", lambda *a: dict(FACTORS))
    )
    cohort = make_cohort([1, 2, 3, 4], costs={(7, 1): ["5"], (7, 3): ["10"], (8, 3): ["8"], (8, 4): [None]})
    monkeypatch.setattr(worker, "read_purchase_cost_cohort", lambda *a: cohort)
    monkeypatch.setattr(
        policy.repo,
        "procurement_source_readiness",
        lambda *a: {"ready": True, "source_fingerprint": "fixture", "producer_count": 2},
    )
    store: dict[str, dict] = {}
    monkeypatch.setattr(worker.cache, "get", lambda key: store.get(key))
    monkeypatch.setattr(worker.cache, "set", lambda key, value, **k: store.__setitem__(key, value) or True)
    monkeypatch.setattr(worker.cache, "delete", lambda key: store.pop(key, None) is not None)
    monkeypatch.setattr(worker.cache, "mark_cart_not_ready", lambda *a, **k: None)
    monkeypatch.setattr(worker.cache, "get_cart_not_ready", lambda *a: None)
    monkeypatch.setattr(worker.cache, "clear_cart_not_ready", lambda *a: True)
    return cohort, calls, store


def _dump(plan):
    return plan.model_dump(mode="json")


@pytest.mark.shared_inputs
def test_shared_inputs_reproduce_every_per_producer_plan_and_the_cart(fake_sources):
    cohort, calls, _ = fake_sources
    shared = inputs_mod.load_plan_inputs(AS_OF, [7, 8, 9], PRODUCTS)
    for only_needed in (True, False):
        for producer in (7, 8, 9):
            legacy = policy.build_plan(
                producer, AS_OF, only_needed=only_needed, cost_cohort=cohort, product_ids=PRODUCTS[producer]
            )
            fast = policy.build_plan(
                producer,
                AS_OF,
                only_needed=only_needed,
                cost_cohort=cohort,
                product_ids=PRODUCTS[producer],
                inputs=shared,
            )
            assert _dump(fast) == _dump(legacy)
    assert any(item.moq == 50 for item in policy.build_plan(
        7, AS_OF, cost_cohort=cohort, product_ids=PRODUCTS[7], inputs=shared
    ).items)
    legacy_cart = policy.build_cart_plan(AS_OF, cost_cohort=cohort, products_by_producer=PRODUCTS)
    plans = {
        producer: policy.build_plan(
            producer, AS_OF, cost_cohort=cohort, product_ids=PRODUCTS[producer], inputs=shared
        )
        for producer in (7, 8, 9)
    }
    before = {producer: _dump(plan) for producer, plan in plans.items()}
    fast_cart = policy.build_cart_plan(
        AS_OF, cost_cohort=cohort, products_by_producer=PRODUCTS, producer_plans=plans
    )
    assert _dump(fast_cart) == _dump(legacy_cart)
    assert fast_cart.item_count > 0
    assert {producer: _dump(plan) for producer, plan in plans.items()} == before


@pytest.mark.shared_inputs
def test_inputs_outside_their_scope_fall_back_to_per_producer_reads(fake_sources):
    cohort, calls, _ = fake_sources
    shared = inputs_mod.load_plan_inputs(AS_OF, [7], {7: [1, 2]})
    calls.clear()
    policy.build_plan(7, AS_OF, cost_cohort=cohort, product_ids=PRODUCTS[7], inputs=shared)
    assert calls["on_order"] == 1 and calls["lead_times"] == 1
    calls.clear()
    policy.build_plan(8, AS_OF, cost_cohort=cohort, product_ids=PRODUCTS[8], inputs=shared)
    assert calls["on_order"] == 1


@pytest.mark.shared_inputs
def test_worker_run_reads_sources_once_and_assembles_cart_from_built_plans(fake_sources, monkeypatch):
    cohort, calls, store = fake_sources
    built = []
    real_build = policy.build_plan

    def counting_build(producer_id, *args, **kwargs):
        built.append(producer_id)
        return real_build(producer_id, *args, **kwargs)

    monkeypatch.setattr(policy, "build_plan", counting_build)
    stats = worker.run(as_of=AS_OF, warm_cart_key=True)
    assert stats["producers"] == 3 and stats["ok"] == 3 and stats["failed"] == 0
    assert sorted(built) == [7, 8, 9]
    for name in ("on_order", "lead_times", "currency", "profile", "terms", "factors",
                 "products_for_producer", "producer_name"):
        assert name not in calls, name
    for name in ("on_order_all", "lead_times_all", "currency_all", "profiles_all", "terms_all",
                 "factors_all", "products_all", "names_raw"):
        assert calls[name] == 1, name
    assert calls["on_hand"] == 1 and calls["reserved"] == 1 and calls["sales"] == 1
    cart = store[worker.cart_cache_key(AS_OF)]
    assert stats["cart_items"] == cart["item_count"] > 0
    assert cart["_source_fingerprint"] == "fixture"
    expected = policy.build_cart_plan(AS_OF, cost_cohort=cohort, products_by_producer=PRODUCTS)
    assert cart["built_at"].endswith("Z") and cart["stale"] is False
    ignored = ("_source_fingerprint", "built_at")
    assert {k: v for k, v in cart.items() if k not in ignored} == {
        k: v for k, v in _dump(expected).items() if k not in ignored
    }


@pytest.mark.shared_inputs
def test_worker_resume_assembles_cart_from_cached_and_rebuilt_plans(fake_sources, monkeypatch):
    cohort, calls, store = fake_sources
    worker.run(as_of=AS_OF, warm_cart_key=True)
    first_cart = dict(store[worker.cart_cache_key(AS_OF)])
    store.pop(worker.cache.make_key("producer", 8, AS_OF))
    store.pop(worker.cart_cache_key(AS_OF))
    built = []
    real_build = policy.build_plan
    monkeypatch.setattr(
        policy, "build_plan", lambda pid, *a, **k: built.append(pid) or real_build(pid, *a, **k)
    )
    stats = worker.run(as_of=AS_OF, warm_cart_key=True, skip_existing=True)
    assert stats["skipped"] == 2 and stats["ok"] == 1 and built == [8]
    assert store[worker.cart_cache_key(AS_OF)] == first_cart


@pytest.mark.shared_inputs
def test_required_masters_failure_in_shared_read_marks_cart_not_ready(fake_sources, monkeypatch):
    cohort, calls, store = fake_sources
    markers = []
    monkeypatch.setattr(worker.cache, "mark_cart_not_ready", lambda *a, **k: markers.append(a))

    def denied(**kwargs):
        raise masters.MastersReadError("masters_terms_read_failed")

    monkeypatch.setattr(masters, "product_terms_all", denied)
    with pytest.raises(masters.MastersReadError):
        worker.run(as_of=AS_OF, warm_cart_key=True)
    assert markers and markers[0][1] == "masters_terms_read_failed"
    assert not any(":producer:" in key or ":cart:" in key for key in store)
