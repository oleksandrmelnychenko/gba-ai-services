# Sales-task mechanic — "to 100%" backlog

From an adversarial gap audit (8 dimensions → ~115 findings → ~50 deduped). The engine is
architecturally sound but not production-ready. Tiers: P0 = correctness/ops blockers, P1 = functions
managers/heads need, P2 = data-dependent tuning, P3 = polish.

## P0 — blockers to "production-ready"
1. **Deploy gba-nba** — Dockerfile + compose services for API **and** scheduler (healthcheck/restart/liveness); gba-nba is absent from the docker stack, so 09:00 generation has no home. (ops, M)
2. **Escalation is half-built** — `sweep_sla()` sets `sla_breached` but NEVER populates `escalated_to`; wire it to escalate high-urgency overdue → HeadSalesAnalytic + audit. (bug, M)
3. **INTERNAL_API_KEY mandatory at deploy** — defaults to '' → API runs OPEN on the internal net. (ops, S)
4. **Guarantee Mongo indexes/init in deploy** — `ensure_indexes()` only runs at API startup; a failed init → unindexed full scans. (ops, M)
5. **Per-day cap & idempotency** — `MAX_TASKS_PER_CLIENT_PER_DAY` is per-RUN not per-calendar-day (on-demand /generate can 2×); scheduler has no distributed lock (concurrent replicas double-insert). (bug, M)
6. **new_client_activation never fires** — TaskType defined, ranked, quota-referenced, in UI unions, but has NO generator + not in `_GENERATORS`. Implement (and rebalance `_TYPE_SHARE`) or remove. (function, M)
7. **run_rate=0 → target=0 → pace boost dead for new/<3-month managers** — new reps with urgent debt look "not behind". Add a fallback baseline. (bug, M)
8. **gba-server proxy error mapping** — `EnsureSuccessStatusCode()` collapses gba-nba 403/404 → 400 and leaks the raw exception body; translate to safe statuses. (bug, M)
9. **gba-nba CI + Docker e2e** — zero CI, only mongomock units; add ruff+pytest CI and a Mongo+nba+server+console e2e (generate→done→head dashboard). (ops, M→L)
10. **menu_seed.sql → real migration** — currently a manual docker cp + sqlcmd; fresh deploy silently has no menu. (ops, S)
11. **Isolated generator unit tests** (debt/reorder/churn/cross_sell with a DB fixture) — only tested via mocked orchestrator; a wrong SQL is undetectable. (ops, M)
12. **Task TTL/expiry sweep** — `expires_at`/`ix_expiry` exist but nothing purges OPEN tasks. (ops, S)
13. **Timeout/circuit-breaker around reco** in cross_sell — only is_healthy() guarded; a hanging reco blocks the whole daily run. (ops, S)
14. **Sanitize API error detail leakage** — inbox_failed/generation_failed return str(exc) (SQL/stack) downstream. (ops, S)

## P1 — high-value functions managers/heads need
1. **Surface the manager's OWN target/pace in the cockpit** — `/cockpit/target` exists but is NEVER called; managers triage blind to their monthly minimum + daily catch-up. (function, M)
2. A/B + by-type KPI analytics endpoint + head drill-down (ab_variant/task_type tagged but never compared). (function, M)
3. Persistent **target_snapshots** collection written by the daily worker (targets are compute-on-the-fly → no history/trends/mid-month review). (function, M)
4. Head-visible **escalated-tasks** endpoint + console view (`ix_escalated` never queried). (function, M)
5. **Manual target override** by head (persisted ManagerTarget + PUT + editable UI). (function, M)
6. Outcome feedback READ side — aggregate task_events + outcomes (events are write-only). (function, M)
7. Head **drill-down** row → that manager's task list (head-only get-tasks-for-manager). (function, M)
8. Task **reassignment** endpoint + UI (tasks permanently bound to original manager). (function, M)
9. Return **manager_name** from /head/team + /cockpit/target (join User full name; head shows '#id'). (data, S)
10. Manual **pin / priority-override** so a manager can elevate one task. (function, M)
11. **Notes history** viewer (notes[] stored, no UI to browse). (function, M)
12. **manager_prefs management** UI + endpoints (view/extend/clear mutes; no unmute path today). (function, M)
13. **Real-time push** for new tasks + wire the bell (SignalR exists, no cockpit stream; bell badge dead). (function/ops, M)
14. **Dismiss-reason** modal + done-not-sold reason (richest negative signal currently discarded). (ux, S→M)
15. **ROI attribution** — link DONE outcome back to the task; "tasks → revenue closed" by type/manager. (function, L)
16. Manager-level **KPI alerts** (conversion<X% or far behind pace → notify head). (function, L)

## P2 — tuning (needs real usage / outcome data)
1. **Externalize ALL hardcoded constants to config + model_version** — weights 0.5/0.3/0.2, VALUE_SATURATION=6000, urgency bands, _MAX_PACE_BOOST, _TYPE_SHARE, pace bands, trailing_months, dismiss_mute_days. Unblocks A/B without redeploy. (tuning, M)
2. **Feedback learning loop** — dismissed/done-not-sold lower future priority of (type,client), not just 30d mute; feed back to gba-reco/gba-procure as negatives. (tuning, L)
3. Calibrate churn (90/365/0.5/prior≥2) + reorder cycle on real holdout; young-SKU derating. (tuning, M)
4. Consistent **confidence definition** across types (currently ad-hoc); decay on repeat dismissals. (tuning, M)
5. **Currency consistency** — shipped/monetary use raw OrderItem vs paid EuroAmount; VALUE_SATURATION calibrated on mixed units. VERIFY + unify. (data, M)
6. **Seasonality + UA holidays** in target engine (flat 3-mo avg; Mon-Sat ignores bank holidays). (tuning, M)
7. Stratify conversion/close-rate by type AND variant; guard zero-divisor → N/A not 0%. (data, M→S)
8. Adaptive age-aware SLA (day-100 debt before day-10; escalate as task ages). (tuning, M)
9. Config-driven MODEL_VERSION + rollback. (ops, S)
10. Exclude synthetic lines in debt_followup; unify ubiquity exclusion across all generators. (tuning, S)
11. Real RFM segmentation (the "RFM-M top 15%" explanation string is never computed). (function, M)
12. Handle **NULL-MainManagerID clients (~72%)** — silently generate zero tasks; round-robin/surface to heads. (data, S)
13. Pagination/limits on signal queries (unbounded result sets buffered before cap). (ops, M)
14. Kyiv-local due_dates + compute_target tz (generators UTC vs working-day/Kyiv semantics). (bug, S)
15. Per-task-type dismiss_mute_days once reason data exists. (tuning, S)
16. Per-manager preference weights (commission vs salary → non-uniform revenue boost). (function, M)

## P3 — polish
Bulk actions; snooze presets (snap to working hours); search/sort/saved-filters + period leaderboard;
CSV/PDF export + email digest; LLM call-script drafts; undo toasts + destructive confirms; in_progress
UI action; task detail drawer + per-task scoring breakdown; relative timestamps + ab_variant badge;
Contact.preferred channel + touchpoint logging; accessibility + mobile; retry/skeletons; won-back
follow-up + never-converts suppression; v2 signals (promo/credit-limit/stalled-cart/birthday);
rate-limit /generate + correlation IDs + secrets rotation + task_events retention.

## Recommended next sprint (biggest jump to 100%)
1. **Deployment + safety**: gba-nba Dockerfile + compose (API + scheduler) + healthchecks; INTERNAL_API_KEY mandatory; Mongo indexes/init; menu_seed as migration.
2. **Close escalation end-to-end**: populate escalated_to in sweep_sla + head escalated-tasks endpoint + console view.
3. **Kill silent no-ops**: implement new_client_activation (+ rebalance _TYPE_SHARE); fix run_rate=0 pace boost; per-calendar-day cap + scheduler idempotency.
4. **Safe iteration**: fix gba-server 403/404 mapping; gba-nba CI (ruff+pytest) + isolated generator tests.
5. **Make it visible**: render the manager's own target/pace in the cockpit; return manager_name from the head API.
6. **Analytics foundation**: write daily target_snapshots; externalize all scoring/quota constants to config + model_version.
