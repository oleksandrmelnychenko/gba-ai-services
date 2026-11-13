from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.services import history_policy

client = TestClient(main.app)
_headers = {"X-Internal-Api-Key": main.settings.internal_api_key} if main.settings.internal_api_key else {}
_LIMITED = ["/assortment/stock", "/assortment/health", "/assortment/regions", "/assortment/margin",
            "/assortment/returns", "/product/5/regions", "/product/5/substitutes"]


def _fail(*args, **kwargs):
    raise AssertionError("limit must be rejected before any work")


@pytest.mark.parametrize("path", _LIMITED)
@pytest.mark.parametrize("limit", [0, 1001, 100000])
def test_limit_outside_cap_is_rejected_before_any_work(monkeypatch, path, limit):
    monkeypatch.setattr(main, "_portfolio", _fail)
    monkeypatch.setattr(main.cache, "get", _fail)
    response = client.get(path, params={"limit": limit}, headers=_headers)
    assert response.status_code == 422


def test_cap_matches_what_gba_server_may_send(monkeypatch):
    assert main._MAX_LIMIT == 1000
    rows = [{"product_id": pid, "band": "healthy", "health": float(pid)} for pid in range(1, 1201)]

    async def fake_portfolio(as_of):
        return {"as_of": as_of, **history_policy.portfolio_metadata(as_of, main.settings), "rows": rows}

    monkeypatch.setattr(main, "_portfolio", fake_portfolio)
    monkeypatch.setattr(main.sig, "product_meta", lambda product_ids, as_of: {})
    response = client.get("/assortment/health", params={"limit": 1000}, headers=_headers)
    assert response.status_code == 200 and response.json()["count"] == 1000


def _stub(monkeypatch, rows, meta):
    async def fake_portfolio(as_of):
        return {"as_of": as_of, "model_version": "products-test-v1",
                **history_policy.portfolio_metadata(as_of, main.settings), "rows": rows}

    monkeypatch.setattr(main, "_portfolio", fake_portfolio)
    monkeypatch.setattr(main.sig, "product_meta", lambda product_ids, as_of: meta)
    monkeypatch.setattr(main.sig, "monthly_product_sales", lambda *args: [])


def test_unknown_product_is_404_on_profile_and_analytics(monkeypatch):
    _stub(monkeypatch, [], {})
    for path in ("/product/987654321", "/product/987654321/analytics"):
        response = client.get(path, headers=_headers)
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["error"] == detail["code"] == "product_not_found"
        assert detail["product_id"] == 987654321
        assert detail["reason"].startswith("Товар не знайдено")


def test_catalog_product_without_signals_stays_200_found_false(monkeypatch):
    _stub(monkeypatch, [], {77: {"product_id": 77, "name": "Catalog only"}})
    response = client.get("/product/77", headers=_headers)
    assert response.status_code == 200
    assert response.json()["found"] is False and response.json()["name"] == "Catalog only"


def test_memoised_portfolio_is_validated_once_per_cache_key(monkeypatch):
    as_of = main._today()
    key = main.cache.make_key("assortment", "portfolio", as_of)
    build = {"as_of": as_of, "rows": [], "count": 0}
    checks = []
    monkeypatch.setattr(main, "_portfolio_cache_compatible", lambda candidate: checks.append(1) or True)
    monkeypatch.setattr(main, "_portfolio_memo", None)
    monkeypatch.setattr(main.cache, "get", lambda cache_key: build)
    import asyncio
    for _ in range(5):
        assert asyncio.run(main._portfolio(as_of)) is build
    assert len(checks) == 1
    monkeypatch.setattr(main.cache, "refresh_generation", lambda: "daily-next")
    asyncio.run(main._portfolio(as_of))
    assert len(checks) == 2


def _row(pid, abc, revenue, gross, units):
    return {"product_id": pid, "band": "dead", "health": 1.0, "abc": abc, "xyz": "Z", "lifecycle": "dead",
            "revenue_eur": revenue, "gross_revenue_eur": gross, "classification_units": units,
            "financial_basis": {"revenue_eur": revenue}}


def test_product_profile_discloses_stock_observation_and_abc_status(monkeypatch):
    _stub(monkeypatch, [_row(5, "unknown", 0.0, 0.0, 0.0)], {5: {"product_id": 5, "name": "Idle"}})
    body = client.get("/product/5", headers=_headers).json()
    assert body["stock_as_of"] == main._today()
    assert body["stock_basis"] == "current" and body["stock_matches_as_of"] is True
    assert body["abc"] == "unknown"
    assert body["abc_status"] == "no_sales" and body["abc_label"] == "Без продажів за період"


def test_abc_status_separates_no_sales_from_returned_sales_and_keeps_classes(monkeypatch):
    rows = [_row(1, "A", 10.0, 10.0, 2.0), _row(2, "unknown", 0.0, 0.0, 0.0),
            _row(3, "unknown", -4.0, 6.0, 1.0)]
    _stub(monkeypatch, rows, {})
    tasks = client.get("/assortment/health", params={"stocked_only": False}, headers=_headers).json()["tasks"]
    assert {t["product_id"]: (t["abc"], t["abc_status"], t["abc_label"]) for t in tasks} == {
        1: ("A", "classified", "A"),
        2: ("unknown", "no_sales", "Без продажів за період"),
        3: ("unknown", "non_positive_contribution", "Продажі перекриті поверненнями або коригуваннями"),
    }
    cases = (("no_sales", [2]), ("unknown", [2, 3]), ("non_positive_contribution", [3]), ("a", [1]))
    for value, expected in cases:
        response = client.get("/assortment/health", params={"stocked_only": False, "abc": value},
                              headers=_headers)
        assert sorted(t["product_id"] for t in response.json()["tasks"]) == expected
    assert main._abc_overview(rows)["by_abc_status"] == {
        "classified": 1, "no_sales": 1, "non_positive_contribution": 1}
