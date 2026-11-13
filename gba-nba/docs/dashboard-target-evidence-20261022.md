# Historical reference source correction

BUG1418: compatibility field `shipped` now aggregates gross confirmed invoice document lines by a verified invoice calendar day via canonical purchase_orders. It uses original Qty and saved EUR PricePerItem; later returns do not rewrite this invoice reference. This is neither recognized net revenue nor proof of physical dispatch. Ambiguous source dates fail the metric rather than invent a timezone.

Scope is the current client book and current document revision; historical reference is not an approved plan or retrospective manager credit. Payments retain the saved EUR/source-register calendar definition. Monday–Saturday pacing excludes no holidays and is explicitly labelled as that assumption. `metric_definitions` carries these caveats; watermark and source coverage remain unknown. Team cache metric version prevents reuse of old Order.Created-based snapshots.

Validation: 282 existing tests passed,40 opt-in SQL tests skipped; new canonical SQL fixture passed against session-local #tables under a read-only DEV login. Focused cache/head/target regression31 tests passed, including rejection of a fresh cache with an old metric definition. SQL fixture verifies invoice day rather than stale order creation, rejects ambiguous lineage, and preserves original gross quantity after full/partial return.
