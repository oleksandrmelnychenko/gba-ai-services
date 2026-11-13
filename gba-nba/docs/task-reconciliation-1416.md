# BUG1416: source-backed recommendation reconciliation

Generation now reconciles existing advice before applying quotas. It does not infer success
from an empty generator response. No new worker, scheduler, sync, or write to the source SQL
business database is introduced.

* A debt task is superseded only when every previously identified debt row still exists with
  an explicit nonpositive balance. Missing/deleted debts, missing clients, SQL failures and
  partial data remain unknown. Legacy tasks without a debt baseline need a positive baseline
  before a later settlement can be confirmed; they cannot be safely closed retroactively.
* A bundled purchase suggestion is superseded only after every suggested product has a newer
  retained purchase. Fully returned orders do not count. Positive return evidence distinguishes
  a renewed need from the earlier, superseded cycle. Current-day purchases suppress stale
  candidates calculated at the generator's exclusive date boundary.
* Source-cycle keys replace the monthly deduplication window. Existing task identities survive
  month rollover and manager reassignment. Current nondeleted Client.MainManagerID plus an active
  User row is required for reassignment; assignment history is retained. Old monthly duplicates
  are superseded with an explicit link to the retained advice.
* Notes, deadlines, source evidence and status history are preserved. Explicit snoozed or
  in-progress work becomes a separately identified manual follow-up when its AI suggestion is
  superseded. An agreed snooze that has already woken becomes an open manual follow-up; it does
  not lose its commitment merely because its current status is open. Interrupted follow-up copies are retried on the next reconciliation. Seller contact
  events and their next-contact dates remain untouched and follow the current client book.
* Manual tasks are excluded from global, per-client and per-type AI quotas. Dashboard payloads
  expose `ai_active` and `manual_backlog` for ALL active states, including deferred snoozed tasks.
  Manager `active_total` is their sum; each head row's `open_tasks` is the same sum. The manager's
  existing `completed_vs_open.open` chart remains the visible inbox count, so it may be smaller.
  Superseded advice is available in workday history.
* Reconciliation occurs on an already-authorized generation run, including runs with zero
  candidates; deploying this code does not run generation. Source errors are reported separately
  from unverified client IDs. No claim is made that source absence proves a completed full sync.

Validation (2026-10-22): hermetic full generator→reconciler→quota→Mongo fixtures use the actual
debt and reorder generators with source I/O replaced. They cover payment, purchase, full return,
second cycle, transfer, generator/source failure, month rollover and manual backlog above both
AI caps. Separate HTTP contract fixtures verify manager/head counts, including deferred tasks.
Actual SQL Server session-local #table fixtures validate source selection and timestamp provenance;
no fixture writes reach business tables, and rollback removes the temporary fixtures.

Commands:

```sh
python -m pytest -q
NBA_SQL_FIXTURE_ENV_FILE=/path/to/dev-reader.env python -m pytest tests/test_purchase_history_sql.py -q
```

The SQL fixture refuses a non-DEV database or a login with INSERT permission on that database.

### Independent review evidence (2026-10-22)

- Current DEV sales since 2025 contain 227,531 valid imported sale lines with all three
  Sale/Order/OrderItem deletion flags false, plus 49 imported lines in another order status.
  Four deleted valid lines belong to native sales. The stale calibration claim that imported
  lines are mostly deleted does not describe the current database. Deletion predicates remain.
- Collection advice carries the exact debt IDs returned by its existing overdue/window filter.
  Other client balances do not enter that advice's baseline. Legacy advice without document IDs
  remains unverified until an identified candidate supplies them. Read-only execution verified
  140 distinct advised debt IDs across 40 clients for one current manager.
- Refreshes capture facts for newly suggested products/debts; prior purchases cannot retroactively
  settle newly added suggestions. Old document identities remain until explicitly settled.
- Changing an existing order's date is not another purchase. Same-day resolution requires verified
  UTC instants from the native confirmed invoice or a unique imported source identity/date that
  matches Sale.Created. Unverified clocks retain conservative date-only handling; no UTC is guessed.
- Source audit on the largest manager found 21,384 unique source-covered sale document dates, all
  matching Sale.Created but only three matching Order.Created. Therefore the shared purchase spine
  uses the confirmed invoice/source event, never draft creation. `purchased_at` is the verified
  Kyiv wall clock (or raw unknown sale clock); `purchased_at_utc` exists only when verified.
  The reader excludes deleted/merged inputs, editable drafts, invalid lines and fully returned stock.
  The output merge document counts once, and ambiguous multiple sale bindings do not multiply rows.
- Reassignment uses an updated-at compare-and-swap; a concurrent seller edit defers reconciliation.
  Stale generation cannot move an already transferred task back to the previous owner.

Remaining release acceptance: integrate the additive nullable C# / console contract, deploy DEV/QA,
and verify visible counts and same-day source evidence. This backend commit alone does not close
BUG-1416. Unknown legacy source clocks and missing source identities intentionally remain unknown;
no sync repair, new worker or production rollout is included.
