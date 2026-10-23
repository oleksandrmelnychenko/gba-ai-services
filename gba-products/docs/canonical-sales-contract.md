# Canonical product sales metrics

Product intelligence uses the same confirmed invoice population and active-return amount/currency proof as native SalesReportProjection / NativeSalesNet. Sale.EffectiveDate and return FromDate are independently filtered in a half-open interval of Europe/Kyiv midnights. `as_of` is the exclusive end date; metadata also exposes the inclusive end shown to users. Import completeness is not inferred from configured history coverage.

`revenue_eur` is confirmed gross sales with VAT, before returns. `net_revenue_eur` subtracts actual active returned Amount with native currency evidence. Unknown return amounts make the full net null; `known_net_revenue_eur` remains an explicitly partial amount, with `unresolved_amount_count`. No original sale price or current FX substitution is used. Signed gross adjustments and return-only negative net remain signed. Current client region is normalized (trim/uppercase, 200→KR); null region is one retained bucket. Query region_id=0 selects that bucket, while its response identity remains null.

All additive monetary calculations use raw Decimal values and round once at the public boundary. `financial_basis` provides exact decimal strings for product/month aggregation. Monthly quantities, invoice counts and gross are separate from returns. Portfolio public units and gross share the rolling sales window; classification retains its own month grid and return rates their own configured window. Estimated margin uses current available stock cost and is not recognized historical profit. ABC ranks positive gross contribution, preserves signed accounting totals and marks nonpositive contribution unknown.

## Required release gate

From gba-products run:

```sh
python scripts/sync_canonical_sales_sql.py --server-repo /path/to/reviewed/gba-server --check
python -m pytest -q
```

The generator extracts canonical SQL string fragments verbatim, including the complete imported return currency proof, and records source plus artifact SHA-256 digests. `--check` must run against the reviewed server revision at each coordinated release. If canonical server code changes, regenerate, review the semantic diff, rerun parity, then commit the generated artifacts and manifest together. Runtime rejects modified artifacts. Do not edit generated SQL by hand.

Cache history fingerprint v2 and default model version v6 prevent reuse of Order.Created/unconfirmed legacy aggregates. DEV and QA source fences/cache isolation remain unchanged.

Read-only validation on 2026-10-23 matched native map/report October 1–23 totals: DEV 213727.91743710870429 EUR/352 clients; QA 212623.92803710870429 EUR/351 clients. All 365-day portfolio gross/return/net totals matched all-region totals, including the unknown bucket. Per-product monthly totals also matched region totals, including a return-only -13.10 EUR example. These checks prove parity for the queried loaded population, not completeness of the original 1C import.
