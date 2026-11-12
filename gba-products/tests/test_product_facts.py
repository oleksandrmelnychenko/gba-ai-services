from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.data import cache
from app.services import history_policy, product_facts

client = TestClient(main.app)
_headers = {"X-Internal-Api-Key": main.settings.internal_api_key} if main.settings.internal_api_key else {}
AS_OF = "2026-07-10"


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttl: dict[str, int] = {}

    def ping(self):
        return True

    def delete(self, key):
        self.hashes.pop(key, None)

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def expire(self, key, ttl):
        self.ttl[key] = ttl

    def rename(self, source, target):
        self.hashes[target] = self.hashes.pop(source)
        self.ttl[target] = self.ttl.pop(source)

    def hmget(self, key, fields):
        return [self.hashes.get(key, {}).get(field) for field in fields]


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(cache, "_get_client", lambda: fake)
    return fake


def _monthly_row(pid, ym, units, revenue, returned="0", unresolved=0, ambiguous=0, orders=1):
    return {
        "product_id": pid, "ym": ym, "units": Decimal(units), "returned_units": Decimal("0.00000000"),
        "order_count": orders, "revenue_eur": Decimal(revenue),
        "known_returned_value_eur": Decimal(returned), "unresolved_amount_count": unresolved,
        "arithmetic_failures": 0, "ambiguous_lines": ambiguous,
    }


def _regional_row(pid, region_id, name, units, revenue, returned="0"):
    return {
        "product_id": pid, "region_id": region_id, "region_name": name,
        "regional_units": Decimal(units), "returned_units": Decimal("1.00000000"),
        "regional_order_count": 2, "regional_client_count": 1, "revenue_eur": Decimal(revenue),
        "known_returned_value_eur": Decimal(returned), "unresolved_amount_count": 0,
        "arithmetic_failures": 0, "ambiguous_lines": 0,
    }


def _stub_profile(monkeypatch, *, rows=None, meta=None):
    async def fake_portfolio(as_of):
        return {"as_of": as_of, "model_version": "products-test-v1",
                **history_policy.portfolio_metadata(as_of, main.settings), "rows": rows or []}

    monkeypatch.setattr(main, "_portfolio", fake_portfolio)
    monkeypatch.setattr(main.sig, "product_meta", lambda product_ids, as_of: meta or {})


def test_encode_decode_round_trip_keeps_exact_decimals_and_types(fake_redis):
    rows = [
        _monthly_row(42, "2026-05", "3.50000000000000", "35.0000000000000000000000", returned="1.25000000000000"),
        _monthly_row(42, "2026-06", "0.00000000000000", "0.0000000000000000000000", orders=0),
        _monthly_row(7, "2026-06", "1.00000000000000", "9.9900000000000000000000"),
    ]
    assert cache.replace_hash("k", product_facts.encode(rows, AS_OF)) is True
    restored = product_facts.read("k", 42, AS_OF)
    assert restored == rows[:2]
    assert [str(row["revenue_eur"]) for row in restored] == [str(row["revenue_eur"]) for row in rows[:2]]
    assert all(type(row["units"]) is Decimal and type(row["order_count"]) is int for row in restored)
    assert product_facts.read("k", 999, AS_OF) == []
    assert fake_redis.ttl["k"] == main.settings.cache_ttl


def test_missing_or_foreign_artefact_is_reported_as_unavailable(fake_redis, monkeypatch):
    assert product_facts.read("absent", 42, AS_OF) is None
    cache.replace_hash("k", product_facts.encode([_monthly_row(42, "2026-05", "1", "1")], AS_OF))
    assert product_facts.read("k", 42, "2026-07-11") is None
    monkeypatch.setattr(main.settings, "model_version", "products-other")
    assert product_facts.read("k", 42, AS_OF) is None


def test_encode_rejects_rows_it_cannot_restore_exactly():
    with pytest.raises(ValueError):
        product_facts.encode([{"product_id": 1, "ym": "2026-05", "units": 1.5}], AS_OF)
    with pytest.raises(ValueError):
        product_facts.encode([{"product_id": 1, "ym": "2026-05", "units": 1},
                              {"product_id": 2, "ym": "2026-05", "units": Decimal("1")}], AS_OF)
    with pytest.raises(ValueError):
        product_facts.encode([{"product_id": 0, "ym": "2026-05"}], AS_OF)


def test_cache_keys_follow_generation_window_and_as_of(monkeypatch):
    monkeypatch.setattr(cache, "refresh_generation", lambda: "daily-a")
    first = product_facts.monthly_key(AS_OF)
    assert product_facts.regional_key(AS_OF, 365) != product_facts.regional_key(AS_OF, 180)
    assert product_facts.monthly_key("2026-07-11") != first
    monkeypatch.setattr(cache, "refresh_generation", lambda: "daily-b")
    assert product_facts.monthly_key(AS_OF) != first


def test_analytics_and_regions_serve_from_artefact_equal_to_live_query(fake_redis, monkeypatch):
    monthly = [
        _monthly_row(42, "2025-09", "9.00000000000000", "90.0000000000000000000000"),
        _monthly_row(42, "2026-05", "3.50000000000000", "35.0050000000000000000000", returned="5.00000000000000"),
        _monthly_row(42, "2026-07", "2.00000000000000", "25.0000000000000000000000"),
        _monthly_row(7, "2026-07", "1.00000000000000", "1.0000000000000000000000"),
    ]
    regional = [
        _regional_row(42, 3, "KV", "4.00000000000000", "40.0000000000000000000000"),
        _regional_row(42, None, None, "1.50000000000000", "70.0000000000000000000000", returned="2.50000000000000"),
        _regional_row(7, 3, "KV", "1.00000000000000", "1.0000000000000000000000"),
    ]
    profile = {"rows": [{"product_id": 42, "qty_on_hand": 7.0, "unit_cost_eur": 4.0}],
               "meta": {42: {"product_id": 42, "name": "Test product"}}}

    def live_monthly(product_id, window_start, as_of):
        return main.canonical.financial_rows(
            [row for row in monthly if row["product_id"] == product_id and row["ym"] >= window_start[:7]])

    def live_regional(as_of, window_days, product_ids=None, region_id=None):
        return main.sig.finalize_regional_rows([row for row in regional if row["product_id"] in product_ids])

    _stub_profile(monkeypatch, **profile)
    monkeypatch.setattr(main.sig, "monthly_product_sales", live_monthly)
    monkeypatch.setattr(main.sig, "regional_product_sales", live_regional)
    paths = [("/product/42/analytics", {"as_of_date": AS_OF, "months": 12}),
             ("/product/42/analytics", {"as_of_date": AS_OF, "months": 3}),
             ("/product/42/regions", {"as_of_date": AS_OF, "window_days": 365}),
             ("/product/42/regions", {"as_of_date": AS_OF, "window_days": 365, "limit": 1})]
    live = [client.get(path, params=params, headers=_headers) for path, params in paths]
    assert all(response.status_code == 200 for response in live)

    monkeypatch.undo()
    monkeypatch.setattr(cache, "_get_client", lambda: fake_redis)
    monkeypatch.setattr(main.memory, "release_free_memory", lambda: False)
    monkeypatch.setattr(main, "_facts_failed_at", {})
    _stub_profile(monkeypatch, **profile)
    calls = {"monthly": 0, "regional": 0}

    def bulk_monthly(as_of, months):
        calls["monthly"] += 1
        assert months == product_facts.MONTHLY_MONTHS
        return monthly

    def bulk_regional(as_of, window_days):
        calls["regional"] += 1
        return regional

    def forbidden(*args, **kwargs):
        raise AssertionError("live per-product query must not run when the artefact exists")

    monkeypatch.setattr(main.sig, "monthly_sales_all_products", bulk_monthly)
    monkeypatch.setattr(main.sig, "regional_sales_all_products", bulk_regional)
    monkeypatch.setattr(main.sig, "monthly_product_sales", forbidden)
    monkeypatch.setattr(main.sig, "regional_product_sales", forbidden)
    served = [client.get(path, params=params, headers=_headers) for path, params in paths]
    served += [client.get(path, params=params, headers=_headers) for path, params in paths]

    assert [response.json() for response in served] == [response.json() for response in live] * 2
    assert calls == {"monthly": 1, "regional": 1}
    assert served[1].json()["sales_series"][0]["month"] == "2026-05"


def test_ambiguous_return_fails_only_the_affected_product(fake_redis, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(cache, "_get_client", lambda: fake_redis)
    monkeypatch.setattr(main.memory, "release_free_memory", lambda: False)
    monkeypatch.setattr(main, "_facts_failed_at", {})
    _stub_profile(monkeypatch, meta={42: {"product_id": 42}, 7: {"product_id": 7}})
    monkeypatch.setattr(main.sig, "monthly_sales_all_products", lambda as_of, months: [
        _monthly_row(42, "2026-07", "1", "10", ambiguous=1), _monthly_row(7, "2026-07", "1", "10")])

    assert client.get("/product/42/analytics", params={"as_of_date": AS_OF}, headers=_headers).status_code == 500
    assert client.get("/product/7/analytics", params={"as_of_date": AS_OF}, headers=_headers).status_code == 200


def test_failed_build_falls_back_to_live_query_and_backs_off(fake_redis, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(cache, "_get_client", lambda: fake_redis)
    monkeypatch.setattr(main, "_facts_failed_at", {})
    _stub_profile(monkeypatch, meta={42: {"product_id": 42}})
    attempts = []

    def broken(as_of, months):
        attempts.append(as_of)
        raise RuntimeError("sql unavailable")

    monkeypatch.setattr(main.sig, "monthly_sales_all_products", broken)
    monkeypatch.setattr(main.sig, "monthly_product_sales", lambda *args: [])
    for _ in range(3):
        assert client.get("/product/42/analytics", params={"as_of_date": AS_OF}, headers=_headers).status_code == 200
    assert attempts == [AS_OF]


def test_redis_down_uses_live_query_without_building(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(cache, "_get_client", lambda: None)
    monkeypatch.setattr(main.sig, "monthly_sales_all_products",
                        lambda *args: (_ for _ in ()).throw(AssertionError("must not build without Redis")))
    rows = asyncio.run(main._product_fact_rows("k", 42, AS_OF, lambda: main.product_facts.build_monthly(AS_OF)))
    assert rows is None


def test_refresh_builds_both_artefacts_for_the_default_window(fake_redis, monkeypatch):
    monkeypatch.setattr(main.memory, "release_free_memory", lambda: False)
    monkeypatch.setattr(main.sig, "monthly_sales_all_products",
                        lambda as_of, months: [_monthly_row(42, "2026-07", "1", "10")])
    monkeypatch.setattr(main.sig, "regional_sales_all_products",
                        lambda as_of, window_days: [_regional_row(42, 3, "KV", "1", "10")])
    result = main._build_and_cache_product_facts(AS_OF)
    assert result["monthly"]["stored"] and result["regional"]["stored"]
    assert result["regional"]["key"] == product_facts.regional_key(AS_OF, main.settings.dead_window_days)
    assert product_facts.available(product_facts.monthly_key(AS_OF), AS_OF)


def test_facts_are_due_when_missing_or_older_than_the_refresh_interval(fake_redis, monkeypatch):
    assert product_facts.due(AS_OF, 365, 21600) is True
    monkeypatch.setattr(main.memory, "release_free_memory", lambda: False)
    monkeypatch.setattr(main.sig, "monthly_sales_all_products", lambda as_of, months: [])
    monkeypatch.setattr(main.sig, "regional_sales_all_products", lambda as_of, window_days: [])
    main._build_and_cache_product_facts(AS_OF)
    assert product_facts.due(AS_OF, 365, 21600) is False
    assert product_facts.due(AS_OF, 365, 0) is True
    assert product_facts.due(AS_OF, 180, 21600) is True
    assert product_facts.read(product_facts.monthly_key(AS_OF), 42, AS_OF) == []
