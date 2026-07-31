# Anthropic competitor price search

`POST /competitors/search` is the internal, API-key-protected market-search endpoint used by
GBA Console through `gba-server`. It uses Claude server-side only; the Anthropic key is never sent
to the browser.

## Prompt

The exact production system prompt is the `SYSTEM_PROMPT` constant in
`app/services/competitor_prompt.py`. That file is the single source of truth so prompt reviews and
code reviews cannot drift apart.

The user query, selected sources, product NetUID and result limit are appended as JSON inside a
`<request_data>` boundary. The system prompt explicitly treats both that JSON and all web pages as
untrusted data.

## Search contract

- Model: `COMPETITOR_SEARCH_MODEL` (default `claude-sonnet-5`).
- Tool: Anthropic `web_search_20260318`, localized to Ukraine.
- Search cap: `COMPETITOR_SEARCH_MAX_USES` (default 5).
- Sources: Prom.ua, Rozetka, Hotline, Avto.pro, or the wider Ukrainian web.
- Output: a two-step turn (web search first, then a forced strict
  `return_competitor_prices` tool call), followed by Pydantic validation.
- Evidence gate: an offer is returned only when its canonical URL occurs in Anthropic's actual web
  search result/citation blocks. Model-only URLs are discarded.
- Cache: Redis, 15 minutes by default, keyed by model + normalized query + selected sources.

## Environment

Use the same server-only Anthropic secret already used by `gba-ecommerce` image search:

```dotenv
ANTHROPIC_API_KEY=...
# Or mount the existing Anthropic__ApiKey Docker secret at this path:
ANTHROPIC_API_KEY_FILE=/run/secrets/AnthropicApiKey
COMPETITOR_SEARCH_MODEL=claude-sonnet-5
COMPETITOR_SEARCH_MAX_USES=5
COMPETITOR_SEARCH_MAX_OFFERS=18
COMPETITOR_SEARCH_TIMEOUT_SECONDS=90
COMPETITOR_SEARCH_CACHE_TTL=900
```

Do not add the key to a `NEXT_PUBLIC_*`, `VITE_*`, committed `.env`, or frontend configuration.
