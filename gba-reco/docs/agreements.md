# Recommendations by client agreement

Sales recommendations use `customer_id` and `client_agreement_net_id`
(`dbo.ClientAgreement.NetUID`). Both `/recommend` and `/recommend/copurchase`
return the agreement identity, including on cache hits and empty results.

- The agreement must be active, non-deleted, belong to the current client and
  have an active organization. Validation runs before reading cached results.
- Order counts, segmentation, frequency, recency, the target similarity basket,
  repurchase and owned-product exclusions use only the selected agreement's
  history from 2025-01-01. Historical agreement generations resolve by NetUID.
- Discovery still uses pooled evidence from other clients and global popularity.
  It must not label purchases from the selected agreement as discovery.
- Candidate stock matches the existing Ukrainian sale bucket policy: VAT uses
  the agreement organization's own storage; non-VAT additionally permits
  Ukrainian resale storage. Defective and Polish stock are excluded. Stock is
  summed before testing positivity.
- `gba-server` continues to calculate the actual selling price, discounts,
  VAT and currency values through its agreement pricing functions. Unpriced
  products are removed by that authoritative hydration path. The ranking model
  does not invent prices or optimize discounts.
- The API client checks the returned agreement identity before hydration. The
  console checks it again in product metadata, requires an agreement, shows an
  agreement selector in client and cockpit panels, and resets products and sale
  selections when the agreement changes. Sale/offer creation receives that same
  agreement. An invalid response cannot silently become client-wide output.

Cache keys include the v39 model version, client, date, top-N, discovery/region
flags and canonical agreement UUID. Copurchase additionally separates explicit
seed lists. Client feedback invalidates every agreement cache for that client;
the existing “do not recommend to this client” action stays client-wide.

Legacy internal NBA and batch requests without an agreement remain client-wide
and use separate keys. Legacy bare-product GET consumers are still supported;
sales metadata requests (`includeMeta=true`) require an agreement.

## Daily refresh

The default `DAILY_WARM_HOUR` is **5**, with `TIMEZONE=Europe/Kyiv`. This is the
start time, not a promise that all calculations finish by 05:00. The worker warms
both standard recommendations and unseeded copurchase for every active agreement
of every recently active, non-deleted client, plus the legacy client scope used
by NBA. Agreements of clients without recent activity calculate on demand.
Partial failures in either output schedule a retry. Startup catch-up remains.

## Deployment and verification

Deploy the Python API/worker first, then the corresponding `gba-server` client
and controller, then the console. Existing `.env` overrides win over defaults;
set `DAILY_WARM_HOUR=5` in the deployed environment and restart the scheduler.
Do not replace running server/console images from an older clean checkout when
their active branch contains other work: apply these focused commits to the
current deployment source and rebuild there.

Validate A → B → A for a client with two active agreements, ensure returned
`client_agreement_net_id` and metadata `ClientAgreementNetId` match every request,
and compare repurchase IDs with the selected agreement's SQL history. Check
prices against the existing agreement price calculation, including a pair with
different discounts. The next scheduled log must show `failed=0`,
`copurchase_failed=0`, and matching context/ok counts before declaring the
deployed morning refresh successful.

Unit regressions cover agreement history/cache isolation, wrong cached identity,
foreign/inactive agreement rejection, copurchase ownership, per-agreement warm
keys and retries on partial failures. The old client-level precision estimate
is not a newly measured agreement-level quality metric; offline evaluation at
the new grain is still needed to estimate predictive quality.
