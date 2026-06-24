# NBA real-data calibration (2026-06-08, ConcordDb_V5)

End-to-end run of the whole NBA engine on live data + adversarially-verified calibration.

## Environment
- Live ConcordDb_V5 via read-only `gba_nba_ro`; Mongo `gba-nba-mongo`; reco on :8000 (copurchase).
- Data is CURRENT (orders & payments through Jun 2026). All money EUR.
- 4 truly-active managers (10146 Баранов / 10182 Мот / 10183 Гураль / 10184 Крицький) + head 10162 (Грель) who also sells; 10150/10156 are empty test accounts.
- Tooling kept in `scripts/`: `realdata_census.py` (read-only signal census), `realdata_generate.py` (full generation + inbox dump), `inbox_analysis.py` (urgency×type cross-tab), `calibration_workflow.js` (the 6-dimension + adversarial-verify workflow).

## Defects found & fixed (engine + reco)
1. **cross_sell called reco for EVERY client** (462–625/mgr incl. cold) → `active_clients_for_manager` (≥`cross_sell_min_orders`=3 orders in `cross_sell_recent_days`=120d) + cap to top `cross_sell_max_clients`=40 by turnover. 4–13× fewer reco calls; cold clients yield no discovery anyway.
2. **debt surfaced €0 noise** (25% of debt clients owe <€10, mostly €0.01 rounding) → `debt_min_amount`=10 floor (sits in a clean valley — only 4 clients in €10–50).
3. **reco copurchase uncached + 6–8s/call** → Redis cache per (client, as_of) (`make_copurchase_key`, 24h TTL): cold ~1.5s, warm ~16ms (300×).
4. **reco copurchase pegged CPU for minutes on large B2B clients** (the co-occurrence matrix × Python scoring loop scaled with client size; top-by-turnover hit the worst cases) → `_COOC_ROW_CAP`=1500 (TOP-N by co_clients) bounds the loop + keeps `_product_degrees` IN-clause under the 2100 param limit; +`query_timeout`=25s statement timeout. Heaviest clients (€334k turnover) went from minutes → 1.2–2.3s, still 25 discovery items.
5. **reco pool exhaustion under concurrent warm** → pool 25/25 (sequential generation never needs it; concurrency is only for cache warm-up).

## Calibration workflow (6 dimensions, each adversarially verified)
The adversarial layer **rejected 4 of 8 proposals** — including a debt proposal whose headline evidence was arithmetically false.

| Dimension | Proposal | Verdict |
|---|---|---|
| debt | debt value-saturation 6000→50000 | **REJECTED** — false evidence (the €728K already out-ranks the cited €1,926 debt); raising k *lowers* every debt's value term. No change. |
| reorder | urgency band from P75 of all due items (not the single max-overdue lead) | **APPLIED** (band only; priority still on the lead). |
| churn | window-normalized rate comparison | **APPLIED** (Option B). A steady buyer (90 recent / 275 baseline) was being flagged as churning. |
| churn | churn_urgency rescale | REJECTED (scope-gamed on a candidate set that wouldn't exist). |
| new_client | — | none — well-calibrated (the "only ≤13d old" was sync-batch timestamps, not a bug). |
| cross_sell | drop top_n / fix comment | REJECTED the top_n change (copurchase scores re-normalize under top_n); removed the stale comment only. |
| scoring | reorder_urgency slope 0.35→0.25 | **APPLIED** (0.25): reorder caps at HIGH (never CRITICAL), since the signal's own 3× ceiling already means "abandoned/near-churn". |
| scoring | reorder lead = median product | REJECTED — keep the most-overdue lead. |

Confirmed **unchanged** by the data: `value_saturation`=6000 (client annual monetary p75≈5,197 EUR), `target_trailing_months`=3, `max_pace_boost`=1.25, `reorder_min_cycle_days`=7 (binds 3.6% of kept pairs), `reorder_max_overdue_mult`=3.0 (5×+ band is abandoned), `ubiquity_exclude_pct`=0.20 (only "Ввід боргів" excluded; safe 0.16–0.74).

## Result (live inbox, urgency × type)
| type | critical | high | normal |
|---|---|---|---|
| debt_followup | 5 | 68 | 0 |
| reorder_due | **0** | 37 | 13 |
| churn_winback | 17 | 22 | 2 |
| cross_sell | 0 | 0 | 35 |
| new_client | 0 | 0 | 22 |

Before calibration reorder was 49 critical / 1 high (flooding the top with routine restocking). After: reorder caps at HIGH with a normal/high spread, and every full-book manager's top-10 now leads with debt + churn (cash-at-risk + lapsed clients), e.g. mgr 10182 top-3 = three debt criticals; mgr 10146's €728K debt rose from buried-under-reorder to #2. reorder priority can still reach 100 via pace-boost, but its band stays HIGH so debt/churn criticals always sort above it.

## Cross-service feedback loop (NBA → reco) — DONE + live-validated
The within-NBA feedback (rejected pairs sink in priority) now extends OUTWARD to reco so the
recommender itself learns from manager behaviour:
- **nba**: `lifecycle.cross_sell_negatives(window_days)` collects, per client, the product_ids from
  cross_sell tasks a manager DISMISSED or completed done-not-sold (across all managers, since reco is
  keyed by client). `worker.push_reco_feedback()` POSTs them to reco at the start of each daily run
  (before generation, so the same run's cross_sell already excludes them); best-effort via
  `reco_client.send_feedback` (never blocks the worker).
- **reco**: `POST /feedback {customer_id, product_ids, kind}` stores a TTL'd negative set
  (`reco:neg:{client}`, `feedback_ttl`=90d) and invalidates that client's copurchase cache;
  `copurchase.recommend` unions the negatives into its exclusion set (alongside the ubiquity filter),
  so rejected products never resurface as discovery.
- **Live-validated end-to-end**: dismissed a real cross_sell task (client 410415, 5 products) →
  `worker.push_reco_feedback` pushed them → that client's copurchase discovery changed completely and
  all 5 rejected products were gone. 55 nba + 8 reco tests + ruff green.

## Operational notes
- First daily generation per `as_of` warms the copurchase cache (sequential cross_sell, ~1.5s/cold client); same-day re-runs and on-demand /generate hit the cache (instant).
- Generation is sequential per manager → reco sees ≤1 concurrent request, so no pool pressure in normal operation.
- Calibration is **distribution-calibrated** (the signals fire on the right clients with sane bands). TRUE outcome-tuning (what converts) still needs managers working the cockpit so the feedback loop accrues real DONE/sold data.
