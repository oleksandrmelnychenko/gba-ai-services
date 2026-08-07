"""Competitor price scanner — Claude web-search agent over the whitelisted UA marketplaces.

Contract mirrors gba-server ClientPricingService.ValidateCompetitorResponse exactly:
market=UA, currency=UAH, query echoed trimmed, searched_at ISO, sources_scanned set-equals
the requested sources, offers<=30 with cent-precision prices and similarity in [0.8, 1].
Everything the model returns is sanitized here so an invalid offer is dropped, never proxied.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.competitor_prompt import COMPETITOR_SEARCH_PROMPT

log = get_logger("competitor_search")

SOURCE_DOMAINS: dict[str, str] = {
    "strans": "strans-shop.com.ua",
    "cargo_parts": "cargo-parts.ua",
    "intercars": "webshop-ua.intercars.eu",
    "omega": "omega.page",
    "tir_market": "tirmarket.com.ua",
}
SOURCE_MARKETPLACE_NAMES: dict[str, str] = {
    "strans": "STRANS",
    "cargo_parts": "Cargo Parts",
    "intercars": "Inter Cars Ukraine",
    "omega": "Омега",
    "tir_market": "TIR Market",
}
SOURCE_PRIORITY: dict[str, int] = {
    "strans": 1,
    "cargo_parts": 2,
    "intercars": 3,
    "omega": 4,
    "tir_market": 5,
}
AVAILABILITIES = frozenset({"in_stock", "limited", "out_of_stock", "unknown"})

RETURN_TOOL_NAME = "return_competitor_prices"

_OFFER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"type": "string", "enum": sorted(SOURCE_DOMAINS)},
        "seller_name": {"type": ["string", "null"]},
        "title": {"type": "string"},
        "url": {"type": "string"},
        "price_uah": {"type": "number"},
        "original_price_uah": {"type": ["number", "null"]},
        "availability": {"type": "string", "enum": sorted(AVAILABILITIES)},
        "delivery_text": {"type": ["string", "null"]},
        "similarity_score": {"type": "number"},
    },
    "required": ["source", "title", "url", "price_uah", "availability", "similarity_score"],
}

RETURN_TOOL: dict[str, Any] = {
    "name": RETURN_TOOL_NAME,
    "description": (
        "Поверни фінальний результат ринкової розвідки рівно один раз. "
        "Кожна пропозиція мусить походити з результатів web search: абсолютний URL "
        "дозволеного домену та явна числова ціна в UAH. Жодних вигаданих даних."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "offers": {"type": "array", "items": _OFFER_SCHEMA},
            "ai_summary": {"type": ["string", "null"]},
        },
        "required": ["offers"],
    },
}


class CompetitorSearchError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _client():
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise CompetitorSearchError(503, "competitor_scanner_not_configured")
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover
        raise CompetitorSearchError(503, "anthropic_sdk_not_installed") from exc
    return anthropic, anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        timeout=float(settings.competitor_request_timeout),
        max_retries=1,
    )


def _user_prompt(query: str, sources: list[str], max_offers: int) -> str:
    ordered = sorted(sources, key=lambda s: SOURCE_PRIORITY[s])
    lines = [
        f"- priority {SOURCE_PRIORITY[s]}: {SOURCE_MARKETPLACE_NAMES[s]} "
        f"(домен {SOURCE_DOMAINS[s]}, source={s})"
        for s in ordered
    ]
    return (
        f"Запит користувача: {query}\n\n"
        "Вибрані джерела для перевірки:\n" + "\n".join(lines) + "\n\n"
        f"Максимальна кількість пропозицій: {max_offers}.\n"
        f"Виклич {RETURN_TOOL_NAME} рівно один раз із фінальним результатом."
    )


def _extract_tool_input(response: Any) -> dict[str, Any] | None:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == RETURN_TOOL_NAME:
            payload = block.input
            return payload if isinstance(payload, dict) else None
    return None


def _run_agent(query: str, sources: list[str], max_offers: int) -> dict[str, Any]:
    settings = get_settings()
    anthropic, client = _client()
    allowed_domains = [SOURCE_DOMAINS[s] for s in sources]
    tools = [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": settings.competitor_web_search_max_uses,
            "allowed_domains": allowed_domains,
        },
        {
            # Search snippets rarely carry prices on these shops — the agent must open
            # the product card to confirm a price, per the доказовість rules.
            "type": "web_fetch_20260209",
            "name": "web_fetch",
            "max_uses": settings.competitor_web_fetch_max_uses,
            "allowed_domains": allowed_domains,
            "max_content_tokens": 25000,
        },
        RETURN_TOOL,
    ]
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _user_prompt(query, sources, max_offers)}
    ]
    deadline = time.monotonic() + settings.competitor_total_budget_seconds

    def _remaining() -> None:
        if time.monotonic() > deadline:
            raise CompetitorSearchError(504, "competitor_search_timeout")

    try:
        response = None
        for _ in range(5):
            _remaining()
            # Streaming: a web-search agentic turn can run for minutes; a non-streaming
            # call trips the SDK read timeout even though the server is still working.
            with client.messages.stream(
                model=settings.anthropic_model,
                max_tokens=8000,
                system=COMPETITOR_SEARCH_PROMPT,
                tools=tools,
                messages=messages,
            ) as stream:
                response = stream.get_final_message()
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            break

        payload = _extract_tool_input(response) if response is not None else None
        if payload is None and response is not None:
            # The model answered in text instead of calling the tool — force the call once.
            _remaining()
            messages.append({"role": "assistant", "content": response.content})
            messages.append({
                "role": "user",
                "content": f"Тепер виклич {RETURN_TOOL_NAME} рівно один раз із фінальним результатом.",
            })
            with client.messages.stream(
                model=settings.anthropic_model,
                max_tokens=4000,
                system=COMPETITOR_SEARCH_PROMPT,
                thinking={"type": "disabled"},
                tools=[RETURN_TOOL],
                tool_choice={"type": "tool", "name": RETURN_TOOL_NAME},
                messages=messages,
            ) as stream:
                forced = stream.get_final_message()
            payload = _extract_tool_input(forced)
    except CompetitorSearchError:
        raise
    except anthropic.AuthenticationError as exc:
        log.error("competitor_search_auth_failed", error=str(exc))
        raise CompetitorSearchError(503, "competitor_scanner_auth_failed") from exc
    except anthropic.RateLimitError as exc:
        raise CompetitorSearchError(429, "competitor_scanner_rate_limited") from exc
    except anthropic.APIStatusError as exc:
        log.error("competitor_search_api_error", status=exc.status_code, error=str(exc))
        raise CompetitorSearchError(502, "competitor_scanner_upstream_error") from exc
    except anthropic.APIConnectionError as exc:
        log.error("competitor_search_connection_error", error=str(exc))
        raise CompetitorSearchError(502, "competitor_scanner_unreachable") from exc

    if payload is None:
        raise CompetitorSearchError(502, "competitor_scanner_no_structured_result")
    return payload


def _clean_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if not text:
        return None
    return text[:limit]


def _clean_money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = round(float(value), 2)
    if amount <= 0 or amount != amount or amount in (float("inf"), float("-inf")):
        return None
    return amount


def _url_matches_source(url: str, source: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    domain = SOURCE_DOMAINS[source]
    return host == domain or host.endswith("." + domain)


def sanitize_offers(raw_offers: Any, sources: list[str], max_offers: int) -> list[dict[str, Any]]:
    """Drop anything that would fail gba-server's offer validation; dedupe and rank."""
    if not isinstance(raw_offers, list):
        return []
    allowed = set(sources)
    seen_urls: set[str] = set()
    offers: list[dict[str, Any]] = []
    for raw in raw_offers:
        if not isinstance(raw, dict):
            continue
        source = raw.get("source")
        if source not in allowed:
            continue
        title = _clean_text(raw.get("title"), 200)
        url = raw.get("url")
        if not title or not isinstance(url, str) or not _url_matches_source(url, source):
            continue
        price = _clean_money(raw.get("price_uah"))
        if price is None:
            continue
        original = _clean_money(raw.get("original_price_uah"))
        if original is not None and original < price:
            original = None
        similarity = raw.get("similarity_score")
        if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
            continue
        similarity = round(float(similarity), 2)
        if similarity < 0.8:
            continue
        similarity = min(similarity, 1.0)
        availability = raw.get("availability")
        if availability not in AVAILABILITIES:
            availability = "unknown"
        url_key = url.strip().lower().rstrip("/")
        if url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        offers.append({
            "source": source,
            "marketplace_name": SOURCE_MARKETPLACE_NAMES[source],
            "seller_name": _clean_text(raw.get("seller_name"), 120),
            "title": title,
            "url": url.strip(),
            "price_uah": price,
            "original_price_uah": original,
            "availability": availability,
            "delivery_text": _clean_text(raw.get("delivery_text"), 160),
            "similarity_score": similarity,
        })
    offers.sort(key=lambda o: (SOURCE_PRIORITY[o["source"]], -o["similarity_score"], o["price_uah"]))
    return offers[: min(max_offers, 30)]


def search_competitors(
    query: str,
    sources: list[str],
    product_net_uid: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    query = query.strip()
    started = time.monotonic()
    payload = _run_agent(query, sources, settings.competitor_max_offers)
    offers = sanitize_offers(payload.get("offers"), sources, settings.competitor_max_offers)
    ai_summary = _clean_text(payload.get("ai_summary"), 400)
    log.info(
        "competitor_search_done",
        query=query,
        product_net_uid=product_net_uid,
        sources=sources,
        offers_raw=len(payload.get("offers") or []) if isinstance(payload.get("offers"), list) else 0,
        offers_kept=len(offers),
        elapsed_s=round(time.monotonic() - started, 1),
    )
    return {
        "market": "UA",
        "currency": "UAH",
        "query": query,
        "searched_at": datetime.now(timezone.utc).isoformat(),
        "sources_scanned": sources,
        "ai_summary": ai_summary,
        "offers": offers,
    }
