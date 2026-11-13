"""Competitor scanner tests — no network; the Anthropic agent run is monkeypatched."""
from __future__ import annotations

import pytest
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
    def fake_run(query, sources, max_offers, **_):
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


class _FakeUsage:
    def __init__(self, input_tokens, output_tokens, searches=0, fetches=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 7
        self.cache_creation_input_tokens = None
        self.server_tool_use = type(
            "ServerToolUse", (), {"web_search_requests": searches, "web_fetch_requests": fetches}
        )()


class _FakeBlock:
    def __init__(self, payload):
        self.type = "tool_use"
        self.name = comp.RETURN_TOOL_NAME
        self.input = payload


class _FakeMessage:
    def __init__(self, stop_reason, content, usage):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = usage
        self.model = "fake-model"


class _FakeStream:
    def __init__(self, message, events=3, on_event=None):
        self._message = message
        self._events = events
        self._on_event = on_event
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def __iter__(self):
        for index in range(self._events):
            if self._on_event is not None:
                self._on_event(index)
            yield index

    def get_final_message(self):
        return self._message


class _FakeClient:
    def __init__(self, streams):
        self._streams = list(streams)
        self.calls = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._streams[len(self.calls) - 1]


def _install_fake_client(monkeypatch, fake):
    import anthropic

    monkeypatch.setattr(comp, "_client", lambda: (anthropic, fake))


def _capture_log(monkeypatch):
    events = []
    monkeypatch.setattr(comp.log, "info", lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr(comp.log, "warning", lambda event, **kw: events.append((event, kw)))
    return events


def test_result_cached_per_query_and_sources_for_24h(monkeypatch, competitor_cache):
    runs = []

    def fake_run(query, sources, max_offers, **_):
        runs.append((query, tuple(sources)))
        return {"offers": [_raw_offer()], "ai_summary": "ok"}

    monkeypatch.setattr(comp, "_run_agent", fake_run)
    first = client.post("/competitors/search", json=_request(), headers=_headers()).json()
    second = client.post(
        "/competitors/search",
        json=_request(query="  1387549   paccar ФІЛЬТР ", sources=["tir_market", "strans"]),
        headers=_headers(),
    )
    assert second.status_code == 200
    body = second.json()
    assert len(runs) == 1
    assert body["query"] == "1387549   paccar ФІЛЬТР"
    assert body["sources_scanned"] == ["tir_market", "strans"]
    assert body["offers"] == first["offers"]
    assert body["searched_at"] == first["searched_at"]
    assert [ttl for _, ttl in competitor_cache["ttl"].items()] == [comp.RESULT_CACHE_TTL_S]
    assert comp.RESULT_CACHE_TTL_S == 86400

    client.post("/competitors/search", json=_request(sources=["strans"]), headers=_headers())
    client.post("/competitors/search", json=_request(query="інший запит"), headers=_headers())
    assert len(runs) == 3


def test_empty_result_is_cached_for_one_hour_and_failures_are_not_cached(
    monkeypatch, competitor_cache
):
    monkeypatch.setattr(comp, "_run_agent", lambda *a, **k: {"offers": []})
    r = client.post("/competitors/search", json=_request(), headers=_headers())
    assert r.status_code == 200 and r.json()["offers"] == []
    assert list(competitor_cache["ttl"].values()) == [comp.EMPTY_RESULT_CACHE_TTL_S]

    def failing(*a, **k):
        raise comp.CompetitorSearchError(502, "competitor_scanner_upstream_error")

    monkeypatch.setattr(comp, "_run_agent", failing)
    r = client.post("/competitors/search", json=_request(query="другий"), headers=_headers())
    assert r.status_code == 502
    assert len(competitor_cache["ttl"]) == 1


def test_agent_logs_token_usage_per_call_and_totals(monkeypatch, competitor_cache):
    paused = _FakeMessage("pause_turn", [], _FakeUsage(1000, 200, searches=3, fetches=1))
    final = _FakeMessage(
        "tool_use",
        [_FakeBlock({"offers": [_raw_offer()], "ai_summary": "ok"})],
        _FakeUsage(1500, 300, searches=1, fetches=2),
    )
    fake = _FakeClient([_FakeStream(paused), _FakeStream(final)])
    _install_fake_client(monkeypatch, fake)
    events = _capture_log(monkeypatch)

    result = comp.search_competitors("1387549 PACCAR", ["strans"])

    assert len(result["offers"]) == 1
    assert len(fake.calls) == 2
    assert all(0 < call["timeout"] <= main.settings.competitor_request_timeout for call in fake.calls)
    usage_events = [kw for event, kw in events if event == "competitor_search_usage"]
    assert [u["call"] for u in usage_events] == [1, 2]
    assert usage_events[0]["input_tokens"] == 1000 and usage_events[0]["output_tokens"] == 200
    assert usage_events[0]["web_search_requests"] == 3
    assert usage_events[1]["cache_creation_input_tokens"] == 0
    done = [kw for event, kw in events if event == "competitor_search_done"][0]
    assert done["api_calls"] == 2
    assert done["input_tokens"] == 2500 and done["output_tokens"] == 500
    assert done["cache_read_input_tokens"] == 14
    assert done["web_search_requests"] == 4 and done["web_fetch_requests"] == 3


def test_stream_stops_when_budget_expires_mid_stream(monkeypatch, competitor_cache):
    import threading

    cancel = threading.Event()
    final = _FakeMessage("tool_use", [_FakeBlock({"offers": []})], _FakeUsage(1, 1))
    stream = _FakeStream(final, events=5, on_event=lambda index: index == 1 and cancel.set())
    fake = _FakeClient([stream])
    _install_fake_client(monkeypatch, fake)
    _capture_log(monkeypatch)

    import pytest

    with pytest.raises(comp.CompetitorSearchError) as excinfo:
        comp._run_agent("1387549 PACCAR", ["strans"], 12, cancel=cancel)
    assert excinfo.value.status_code == 504
    assert stream.closed is True
    assert len(fake.calls) == 1


def test_agent_makes_no_api_call_after_deadline(monkeypatch, competitor_cache):
    import time

    import pytest

    fake = _FakeClient([])
    _install_fake_client(monkeypatch, fake)
    with pytest.raises(comp.CompetitorSearchError) as excinfo:
        comp._run_agent("1387549 PACCAR", ["strans"], 12, deadline=time.monotonic() - 1)
    assert excinfo.value.status_code == 504
    assert fake.calls == []


def test_endpoint_answers_504_within_total_budget_and_cancels_worker(
    monkeypatch, competitor_cache
):
    import time

    seen = {}

    def slow_run(query, sources, max_offers, *, deadline=None, cancel=None, usage=None):
        seen["cancelled"] = cancel.wait(5)
        seen["deadline_in"] = deadline - time.monotonic()
        raise comp.CompetitorSearchError(504, "competitor_search_timeout")

    monkeypatch.setattr(comp, "_run_agent", slow_run)
    monkeypatch.setattr(main.settings, "competitor_total_budget_seconds", 0.2, raising=False)
    started = time.monotonic()
    r = client.post("/competitors/search", json=_request(), headers=_headers())
    elapsed = time.monotonic() - started
    assert r.status_code == 504
    assert r.json()["detail"] == "competitor_search_timeout"
    assert elapsed < 2.0
    for _ in range(100):
        if "cancelled" in seen:
            break
        time.sleep(0.02)
    assert seen["cancelled"] is True
    assert competitor_cache["ttl"] == {}


def test_default_total_budget_is_below_the_gba_server_proxy_timeout():
    from app.core.config import Settings

    assert Settings.model_fields["competitor_total_budget_seconds"].default < 240


def test_api_timeout_maps_to_504(monkeypatch, competitor_cache):
    import anthropic
    import httpx
    import pytest

    class _Raising:
        messages = None

        def stream(self, **kwargs):
            raise anthropic.APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))

    fake = _Raising()
    fake.messages = fake
    _install_fake_client(monkeypatch, fake)
    _capture_log(monkeypatch)
    monkeypatch.setattr(comp.log, "error", lambda *a, **k: None)
    with pytest.raises(comp.CompetitorSearchError) as excinfo:
        comp._run_agent("1387549 PACCAR", ["strans"], 12)
    assert excinfo.value.status_code == 504


def test_envelope_and_offers_declare_uah_shelf_price_with_unknown_vat_basis(
    monkeypatch, competitor_cache
):
    monkeypatch.setattr(
        comp, "_run_agent", lambda *a, **k: {"offers": [_raw_offer()], "ai_summary": "ok"}
    )
    first = client.post("/competitors/search", json=_request(), headers=_headers()).json()
    cached = client.post("/competitors/search", json=_request(), headers=_headers()).json()
    for body in (first, cached):
        assert body["currency"] == "UAH"
        assert body["price_basis"] == "shelf"
        assert body["vat_basis"] == "unknown"
        assert body["offers"]
        for offer in body["offers"]:
            assert offer["currency"] == "UAH"
            assert offer["price_basis"] == "shelf"
            assert offer["vat_basis"] == "unknown"
    stored = next(iter(competitor_cache["data"].values()))
    assert "vat_basis" not in stored["offers"][0]


def test_reminder_call_never_forces_tool_choice_or_disables_thinking(
    monkeypatch, competitor_cache
):
    text_only = _FakeMessage(
        "end_turn", [type("Text", (), {"type": "text", "text": "огляд"})()], _FakeUsage(10, 5)
    )
    final = _FakeMessage(
        "tool_use", [_FakeBlock({"offers": [_raw_offer()]})], _FakeUsage(10, 5)
    )
    fake = _FakeClient([_FakeStream(text_only), _FakeStream(final)])
    _install_fake_client(monkeypatch, fake)

    result = comp.search_competitors("1387549 PACCAR", ["strans"])

    assert len(result["offers"]) == 1
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert "tool_choice" not in call
        assert "thinking" not in call
        assert "temperature" not in call
        assert call["model"] == main.settings.anthropic_model
        assert call["output_config"] == {"effort": main.settings.competitor_effort}
        assert [tool.get("type") for tool in call["tools"][:2]] == [
            "web_search_20260209",
            "web_fetch_20260209",
        ]
        assert call["tools"][2]["name"] == comp.RETURN_TOOL_NAME
    reminder = fake.calls[1]["messages"]
    assert reminder[:1] == fake.calls[0]["messages"][:1]
    assert reminder[-1]["role"] == "user"
    assert comp.RETURN_TOOL_NAME in reminder[-1]["content"]


def test_refusal_is_reported_and_not_cached(monkeypatch, competitor_cache):
    refused = _FakeMessage("refusal", [], _FakeUsage(10, 0))
    fake = _FakeClient([_FakeStream(refused)])
    _install_fake_client(monkeypatch, fake)

    with pytest.raises(comp.CompetitorSearchError) as raised:
        comp.search_competitors("1387549 PACCAR", ["strans"])

    assert raised.value.status_code == 502
    assert raised.value.detail == "competitor_scanner_refused"
    assert len(fake.calls) == 1
    assert competitor_cache["data"] == {}


def test_default_model_is_the_current_sonnet():
    from app.core.config import Settings

    assert Settings(_env_file=None).anthropic_model == "claude-sonnet-5-5"
