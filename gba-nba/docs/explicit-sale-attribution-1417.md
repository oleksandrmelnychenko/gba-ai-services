# Explicit sale attribution — policy v1

Approved by the user on 22 October 2026: explicit association, one primary task per sale,
and responsible manager fixed when the association is recorded. Historical sales are never
matched automatically by client, date, contact proximity or offer status.

The manager selects an actual sale belonging to the task's current client book. An optional
contact must be an unambiguous event for that same client. The source document's existing
Order → ClientShoppingCart foreign key is shown as proposal evidence; a proposal's recalculated
display total is not an immutable quotation and is not used as financial credit. The explicit
association covers the selected whole sale, including any extra basket lines; the UI says so.

`explicit_sale_attributions._id` is the canonical sale UUID. Mongo's native unique key is the
exclusive primary-task constraint, including concurrent requests. Exact retries return the
original association and timestamp; other tasks/managers receive a conflict. The record and
original source snapshot have no TTL. Read evidence is freshly resolved, never copied from
the original snapshot when source access fails. An inaccessible source is unknown, not zero.

The link owner is the authenticated manager at recording time. Later client/task transfers
do not move that historical attribution. The original owner and the head can read it; the new
owner cannot silently claim it again. Task status, payment records and contact events are not
modified by linking. This feature does not send anything to a client.

## Documentary and financial rules

- Only confirmed invoices (`Sale.ChangedToInvoice IS NOT NULL`) with retained lines can be
  linked. Deleted sale/order/agreement, drafts and fully returned sales cannot be newly linked.
- Merged sale/order inputs or outputs and multiple live sale headers for one order are
  ambiguous. They cannot be newly attributed. A later merge keeps the original association
  for review but removes its confirmed monetary contribution; no automatic transfer/split.
- Commercial evidence is document/order/proposal identity, document date and shipment date.
  Native invoice dates use confirmed invoice UTC converted to Kyiv. Imported dates are converted
  only when unique source-line lineage proves the stored UTC clock. Otherwise the original stored
  sale date is explicitly labelled unverified; payment/return source calendar dates are unchanged.
  No `Qty × current price` estimate is presented as net sales or shipment revenue.
- Payment evidence is each persisted `IncomePaymentOrderSale` allocation ID and parent
  payment ID/date/currency. Its `Amount` is stored EUR by the financial writer; no current
  exchange rate is applied. Active nondeleted, noncancelled allocations are summed. Partial
  payments remain partial; overpayments not allocated to this sale are excluded. A cancelled,
  deleted or reallocated payment disappears from the active allocation sum on refresh.
- Returns are attached through actual OrderItem IDs. Cancelled/deleted returns do not count.
  A return credit amount is known only when the financial writer marked RecordedGrossAmountVersion=1
  and IsMoneyReturned. Unversioned processed return entries make credit coverage unknown. This gross credit reduces debt or credits the client balance; it is not evidence of a cash refund.
  Payments and return credits are displayed separately, never advertised as net revenue.
- A later cancellation leaves immutable evidence but no confirmed monetary credit. Returns
  after the original month appear in current evidence. This screen is explicitly a current
  document reconciliation, not a historical monthly revenue attribution report.

Manual task outcomes remain a separate manager statement. The head's document panel is
paginated, with full link count and per-document financial evidence; it does not manufacture
targets, sales uplift, causality, or a full-company revenue total from a page of links.

## Verification

Fixtures cover exclusive concurrent ownership, replay precision, contact/client mismatch,
multiple proposals, mixed sale association, draft/full-return rejection, partial foreign-currency
payment, payment cancellation, unknown refund, later return, merge/cancellation, manager
transfer and source failure. The same API tests can use disposable real Mongo through
TEST_ATTRIBUTION_MONGO_URI; each random fixture database is dropped afterward.
