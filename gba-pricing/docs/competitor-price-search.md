# Anthropic competitor price search

`POST /competitors/search` is the internal, API-key-protected market-search endpoint used by
GBA Console through `gba-server`. It uses Claude server-side only; the Anthropic key is never sent
to the browser.

## Prompt

The exact production system prompt is the `COMPETITOR_SEARCH_PROMPT` constant in
`app/services/competitor_prompt.py`; the console keeps a read-only mirror of it. The user query,
the selected sources and the offer limit go in the user message. The system prompt treats the
query and all web pages as untrusted data.

## Search contract

- SDK: `anthropic` 2.x (API version `2026-10-21`), streaming `client.messages.stream`.
- Model: `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`), adaptive thinking left on, effort
  `COMPETITOR_EFFORT` (default `medium`), `COMPETITOR_MAX_TOKENS` (default 16000).
- Tools: Anthropic `web_search_20260209` and `web_fetch_20260209`, both restricted to the selected
  source domains, plus the client tool `return_competitor_prices`.
- Caps: `COMPETITOR_WEB_SEARCH_MAX_USES` (default 10), `COMPETITOR_WEB_FETCH_MAX_USES`
  (default 12), `COMPETITOR_MAX_OFFERS` (default 12, never more than 30).
- Turn shape: one agent turn with `tool_choice` auto, resumed on `pause_turn`. The model is asked
  to call `return_competitor_prices` once. If it answers in text instead, one reminder turn with
  the same tools asks for the call. The tool call is never forced and thinking is never disabled:
  the current models reject both.
- Time: `COMPETITOR_REQUEST_TIMEOUT` per API call (default 120 s) inside
  `COMPETITOR_TOTAL_BUDGET_SECONDS` for the whole scan (default 210 s, below the gba-server proxy
  timeout); past the budget the endpoint answers 504.
- Validation: `sanitize_offers` drops any offer whose source is not selected, whose URL host is
  not the source's own domain, whose price is not a positive number, or whose similarity is below
  0.8. There is no citation cross-check: a URL is accepted on its domain, not on proof that it
  appeared in a search result.
- Cache: Redis, 24 hours for a result with offers and 1 hour for an empty one, keyed by
  normalized query + selected sources + model + offer limit. Failures are not cached.
- Errors: 503 `competitor_scanner_not_configured` (no key) or `competitor_scanner_auth_failed`,
  429 `competitor_scanner_rate_limited`, 502 `competitor_scanner_upstream_error`,
  `competitor_scanner_unreachable`, `competitor_scanner_refused` or
  `competitor_scanner_no_structured_result`, 504 `competitor_search_timeout`.

## What the prices are

Every price is the public shelf price in UAH as the marketplace page shows it. The scan does not
establish whether that price includes VAT, and it does not convert currency. The response says so
explicitly:

- envelope: `currency: "UAH"`, `price_basis: "shelf"`, `vat_basis: "unknown"`;
- every offer: the same three fields next to `price_uah`.

These prices are not directly comparable with GBA prices, which are in EUR on the agreement's own
VAT basis. `gba-server` currently deserializes only the fields of `CompetitorPriceSearchResultDto`
and ignores the new ones until its DTO is extended.

## Competitor registry

| Priority | Source | Domain | Search access |
| ---: | --- | --- | --- |
| 1 | STRANS | `strans-shop.com.ua` | Public catalog, prices and stock |
| 2 | Cargo Parts | `cargo-parts.ua` | B2B login required for prices |
| 3 | Inter Cars Ukraine | `webshop-ua.intercars.eu` | Webshop may use browser verification |
| 4 | Омега | `omega.page` and subdomains | Public assortment; B2B login for prices |
| 5 | TIR Market | `tirmarket.com.ua` | Public site; may be temporarily unavailable |

Web search and web fetch are domain-restricted to the selected rows. They receive no credentials
and do not bypass authentication or bot protection.

## Environment

```dotenv
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-5-5
COMPETITOR_EFFORT=medium
COMPETITOR_MAX_TOKENS=16000
COMPETITOR_MAX_OFFERS=12
COMPETITOR_WEB_SEARCH_MAX_USES=10
COMPETITOR_WEB_FETCH_MAX_USES=12
COMPETITOR_REQUEST_TIMEOUT=120
COMPETITOR_TOTAL_BUDGET_SECONDS=210
```

An empty `ANTHROPIC_API_KEY` disables the feature (503). Do not add the key to a `NEXT_PUBLIC_*`,
`VITE_*`, committed `.env`, or frontend configuration.
