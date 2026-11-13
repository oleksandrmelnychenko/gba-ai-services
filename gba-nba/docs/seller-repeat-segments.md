# Seller client segments (Desk 1415)

`GET /cockpit/clients` supports `segment`, trimmed `search`, `offset` and `limit` (default 50,
maximum 200). `count` is page length; `total` is the complete selected segment/search count;
`population_count` and `segment_counts.all` cover the searched owned book before segment selection.
Segment counts can overlap. Search is case-insensitive name/full name/phone/email/client ID.
Source failures return HTTP 500, not a successful empty population.

The selected Kyiv business date is inclusive. The shared retained purchase source accepts only a
single active confirmed nonmerged sale, current nondeleted order/item/agreement/client, positive
retained quantity after returns. Verified native invoice time or imported source-line event time
is converted to Kyiv business dates. Unverified imported dates are retained as source-document
dates without claiming a UTC instant. `Client.Created` is never treated as customer acquisition.
The `new_client` segment means **no retained purchases in available history**, not genuinely new.

The existing reorder rule is unchanged: at least 3 distinct purchase documents, mean gap
(first→last)/(count−1), floor 7 days (configured), elapsed between 1 and 3 cycles (configured).
Due is the expected date itself; overdue is later. Reactivation uses the existing 90/365 day
frequency comparison, with source-boundary-adjusted window lengths. Cycle dates are explanatory
estimates, not a seasonal forecasting model. Reorder generator payloads expose the same evidence.
Automatic new-client task generation remains retired; this change does not run generation.

One SQL snapshot supplies the owned book, repeat evidence, order counts, turnover and churn.
All product rows contribute to segment totals and contact-cycle boundaries before evidence is
bounded to five products per client (eligible reorder products first, existing elapsed/cycle rank).
Client ordering remains turnover descending/name/ID. `history_start` is the configured source
floor; cycles use all history from this floor. `history_complete` means at least the complete
trailing 365 day reference period is available, not lifetime coverage. Short histories are marked.

Availability is current free `ProductAvailability.Amount` across active non-defective warehouses,
not an agreement-specific quote; missing/deleted product metadata is unknown, never a fabricated0.
Out-of-stock opportunities remain visible with explicit explanation. Sale/offer actions must still
validate current agreement, warehouse, stock and price through their existing paths.

Persisted contact history is read on every request and follows the currently owned client even
if another manager made the contact. Future explicit next-contact appointments suppress actionable
segments until their exact deadline. Once an explicit deadline expires, inferred cooldown cannot
extend it. Without an appointment, reached/proposal/declined/promised-payment events suppress only
the current inferred product cycle, provided the contact covers every due product cycle. No-answer
alone does not suppress; no arbitrary day-based TTL is added. Clients remain visible in All, and
manual task/contact commitments are not mutated. New-client/reactivation without a product cycle
only use an explicit appointment as cooldown. These read-only segments do not rewrite AI tasks;
Desk 1416 owns task lifecycle reconciliation.

Validation: unit/API fixtures cover regular, seasonal, short, no-history, same-day purchase,
out-of-stock/unknown stock, search/pagination, contact deadline/cycle/no-answer and source failure.
`NBA_SQL_FIXTURE_ENV_FILE=/path/to/dev/reader.env pytest tests/test_repeat_sales_sql.py` runs the
actual SQL against session-local temporary tables with a read-only business identity, checking
returns, drafts, merged inputs, current-day invoices, stock filters and evidence caps. No business
rows are written. Full suite also covers generation/backward-compatible payloads.
