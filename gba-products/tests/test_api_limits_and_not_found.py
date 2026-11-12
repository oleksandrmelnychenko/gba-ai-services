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
        assert response.json() == {"detail": "product_not_found"}


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
