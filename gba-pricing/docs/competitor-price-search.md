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
- Search cap: `COMPETITOR_SEARCH_MAX_USES` (default 10, enough for five prioritized sources and
  normalized article-number variants).
- Sources, in business-priority order: STRANS, Cargo Parts, Inter Cars Ukraine, Омега, TIR Market.
- Access policy: only STRANS currently exposes public prices reliably. Cargo Parts and Омега may
  require a B2B session; Inter Cars may apply browser verification; unavailable or hidden prices
  are never inferred.
- Output: a two-step turn (web search first, then a forced strict
  `return_competitor_prices` tool call), followed by Pydantic validation.
- Evidence gate: an offer is returned only when its canonical URL occurs in Anthropic's actual web
  search result/citation blocks. Model-only URLs are discarded.
- Cache: Redis, 15 minutes by default, keyed by model + normalized query + selected sources.

## Competitor registry

| Priority | Source | Domain | Search access |
| ---: | --- | --- | --- |
| 1 | STRANS | `strans-shop.com.ua` | Public catalog, prices and stock |
| 2 | Cargo Parts | `cargo-parts.ua` | B2B login required for prices |
| 3 | Inter Cars Ukraine | `webshop-ua.intercars.eu` | Webshop may use browser verification |
| 4 | Омега | `omega.page` / `my.omega.page` | Public assortment; B2B login for prices |
| 5 | TIR Market | `tirmarket.com.ua` | Public site; may be temporarily unavailable |

Anthropic Web Search is domain-restricted to the selected rows. It does not receive credentials,
does not bypass authentication or bot protection, and omits any offer whose numeric UAH price is
not visible in cited evidence.

## Environment

Use the same server-only Anthropic secret already used by `gba-ecommerce` image search:

```dotenv
ANTHROPIC_API_KEY=...
# Or mount the existing Anthropic__ApiKey Docker secret at this path:
ANTHROPIC_API_KEY_FILE=/run/secrets/AnthropicApiKey
COMPETITOR_SEARCH_MODEL=claude-sonnet-5
COMPETITOR_SEARCH_MAX_USES=10
COMPETITOR_SEARCH_MAX_OFFERS=18
COMPETITOR_SEARCH_TIMEOUT_SECONDS=90
COMPETITOR_SEARCH_CACHE_TTL=900
```

Do not add the key to a `NEXT_PUBLIC_*`, `VITE_*`, committed `.env`, or frontend configuration.
