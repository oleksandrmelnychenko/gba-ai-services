"""Competitor scanner tests — no network; the Anthropic agent run is monkeypatched."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import main
from app.services import competitor_search as comp

client = TestClient(main.app)


def _headers() -> dict[str, str]:
    if not main.settings.internal_api_key:
        return {}
    return {"X-Internal-Api-Key": main.settings.internal_api_key}


def _request(**overrides) -> dict:
    body = {
        "market": "UA",
        "product_net_uid": None,
        "query": "1387549 PACCAR фільтр",
        "sources": ["strans", "tir_market"],
    }
    body.update(overrides)
    return body


def _raw_offer(**overrides) -> dict:
    offer = {
        "source": "strans",
        "seller_name": "STRANS",
        "title": "Фільтр масляний PACCAR 1387549",
        "url": "https://strans-shop.com.ua/product/1387549",
        "price_uah": 1234.567,
        "original_price_uah": 1500.0,
        "availability": "in_stock",
        "delivery_text": "Київ, 1-2 дні",
        "similarity_score": 0.95,
    }
    offer.update(overrides)
    return offer


# --- endpoint validation ------------------------------------------------------------------


def test_rejects_non_ua_market():
    r = client.post("/competitors/search", json=_request(market="PL"), headers=_headers())
    assert r.status_code == 422


def test_rejects_short_query():
    r = client.post("/competitors/search", json=_request(query="x"), headers=_headers())
    assert r.status_code == 422


def test_rejects_unknown_and_duplicate_sources():
    for sources in ([], ["rozetka"], ["strans", "strans"]):
        r = client.post(
            "/competitors/search", json=_request(sources=sources), headers=_headers()
        )
        assert r.status_code == 422, sources


def test_rejects_malformed_product_net_uid():
    r = client.post(
        "/competitors/search", json=_request(product_net_uid="not-a-guid"), headers=_headers()
    )
    assert r.status_code == 422


def test_503_when_not_configured(monkeypatch):
    monkeypatch.setattr(main.settings, "anthropic_api_key", "", raising=False)
    r = client.post("/competitors/search", json=_request(), headers=_headers())
    assert r.status_code == 503
    assert r.json()["detail"] == "competitor_scanner_not_configured"


def test_endpoint_returns_contract_envelope(monkeypatch):
    def fake_run(query, sources, max_offers):
        return {"offers": [_raw_offer()], "ai_summary": "  Знайдено 1 пропозицію.  "}

    monkeypatch.setattr(comp, "_run_agent", fake_run)
    r = client.post("/competitors/search", json=_request(), headers=_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["market"] == "UA"
    assert body["currency"] == "UAH"
    assert body["query"] == "1387549 PACCAR фільтр"
    assert body["sources_scanned"] == ["strans", "tir_market"]
    assert body["ai_summary"] == "Знайдено 1 пропозицію."
    assert body["searched_at"].endswith("+00:00")
    offer = body["offers"][0]
    assert offer["price_uah"] == 1234.57
    assert offer["marketplace_name"] == "STRANS"
    # every field required by the .NET DTO must be present
    for key in (
        "source", "marketplace_name", "seller_name", "title", "url", "price_uah",
        "original_price_uah", "availability", "delivery_text", "similarity_score",
    ):
        assert key in offer


# --- sanitize_offers ----------------------------------------------------------------------


def test_sanitize_drops_wrong_domain_and_source():
    offers = comp.sanitize_offers(
        [
            _raw_offer(url="https://rozetka.com.ua/x"),          # wrong domain for source
            _raw_offer(source="omega"),                            # source not requested
            _raw_offer(source="rozetka"),                          # unknown source
            _raw_offer(url="ftp://strans-shop.com.ua/x"),          # non-http scheme
        ],
        ["strans", "tir_market"],
        12,
    )
    assert offers == []


def test_sanitize_drops_bad_prices_and_scores():
    offers = comp.sanitize_offers(
        [
            _raw_offer(price_uah=0),
            _raw_offer(price_uah=-5),
            _raw_offer(price_uah="1000"),
            _raw_offer(similarity_score=0.79),
            _raw_offer(similarity_score="high"),
        ],
        ["strans"],
        12,
    )
    assert offers == []


def test_sanitize_normalizes_fields():
    offers = comp.sanitize_offers(
        [
            _raw_offer(
                price_uah=999.999,
                original_price_uah=500.0,     # below price -> dropped to None
                availability="preorder",      # unknown enum -> unknown
                similarity_score=1.07,        # clamped to 1.0
                seller_name="   ",            # blank -> None
            )
        ],
        ["strans"],
        12,
    )
    assert len(offers) == 1
    o = offers[0]
    assert o["price_uah"] == 1000.0
    assert o["original_price_uah"] is None
    assert o["availability"] == "unknown"
    assert o["similarity_score"] == 1.0
    assert o["seller_name"] is None


def test_sanitize_dedupes_url_and_caps_and_sorts():
    offers = comp.sanitize_offers(
        [
            _raw_offer(source="tir_market", url="https://tirmarket.com.ua/p/1", similarity_score=1.0),
            _raw_offer(url="https://strans-shop.com.ua/p/2", similarity_score=0.85),
            _raw_offer(url="https://strans-shop.com.ua/p/2/", similarity_score=0.9),  # dup URL
            _raw_offer(url="https://strans-shop.com.ua/p/3", similarity_score=0.95),
        ],
        ["strans", "tir_market"],
        2,
    )
    assert len(offers) == 2
    # strans (priority 1) first, best similarity first within source
    assert [o["url"] for o in offers] == [
        "https://strans-shop.com.ua/p/3",
        "https://strans-shop.com.ua/p/2",
    ]


def test_sanitize_accepts_subdomain_for_omega():
    offers = comp.sanitize_offers(
        [_raw_offer(source="omega", url="https://b2b.omega.page/item/9")],
        ["omega"],
        12,
    )
    assert len(offers) == 1
    assert offers[0]["marketplace_name"] == "Омега"


def test_prompt_matches_console_mirror_marker():
    from app.services.competitor_prompt import COMPETITOR_SEARCH_PROMPT

    assert COMPETITOR_SEARCH_PROMPT.startswith("Ти — GBA Market Radar")
    assert "return_competitor_prices" in COMPETITOR_SEARCH_PROMPT
    assert COMPETITOR_SEARCH_PROMPT.endswith("для наступного кроку.")
