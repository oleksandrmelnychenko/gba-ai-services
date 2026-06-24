# gba-reco offline eval baseline

Honest, harness-measured baseline for the recommendation models. Regenerate with the
service's own eval harness against the dev DB (read-only `gba_reco_ro`); future changes
must regress against these numbers, not against guesses or the previously-fabricated
`precision_estimate`.

Protocol: leave-last-basket-out, point-in-time (`Created < as_of`, held-out order excluded),
k=10, synthetic/ubiquitous accounting lines excluded uniformly from truth and recs
(`app/services/eval/harness.py`). Truth = valid product lines (`IsValidForCurrentSale = 1`)
of each eligible client's last order. One case per client with >= 2 valid orders.

## How to reproduce

```
# full v3.2 (all eligible cases — cheap, ~280ms/case)
.venv/bin/python -m app.services.eval.harness --k 10

# 4-way head-to-head (v3.2 vs copurchase vs naive baselines) — sampled (copurchase ~1.7s/case)
.venv/bin/python -m app.services.eval.harness --compare --k 10 --limit 150

# byRegion A/B (v3.2 vs v3.2 with region-scoped discovery)
.venv/bin/python -m app.services.eval.harness --compare-region --k 10 --limit 250
```

## Measured baseline (dev DB, 2026-06)

### v3.2 — full population, all eligible cases (n = 409, k = 10)

| metric        | value |
|---------------|-------|
| hit_rate@10   | 0.242 |
| precision@10  | 0.033 |
| recall@10     | 0.193 |
| MRR@10        | 0.129 |

Per-segment hit_rate@10: HEAVY n=62 0.145 · LIGHT n=289 0.266 ·
REGULAR_CONSISTENT n=49 0.245 · REGULAR_EXPLORATORY n=9 0.111.

### Head-to-head vs baselines (sampled, n = 52, k = 10)

| model               | hit_rate | recall | precision | MRR   |
|---------------------|----------|--------|-----------|-------|
| v3.2                | 0.212    | 0.125  | 0.031     | 0.090 |
| copurchase          | 0.115    | 0.089  | 0.023     | 0.041 |
| naive_most_frequent | 0.077    | 0.059  | 0.015     | 0.061 |
| naive_global_popular| 0.038    | 0.020  | 0.004     | 0.029 |

V3.2 is the clear winner — ~2x the strongest naive floor (most_frequent) on hit_rate and
above copurchase on every metric. It justifies itself over the trivial baselines.

## repurchase-quota sweep — VERDICT: HOLD at repurchase_count = 20

Sweep of `Settings.repurchase_count` over {20, 16, 12, 10, 8, 6} on the leave-last-basket
harness (k=10, synthetic excluded, same case-build as the committed baseline; n=430 at the
current DB state). `discovery_count` set to the top_n complement per setting. Reproduce:
`.venv/bin/python -m scripts.repurchase_quota_sweep --k 10`.

| repurchase_count | discovery_count | hit@10 | MRR   | recall | prec  | disc_share |
|------------------|-----------------|--------|-------|--------|-------|------------|
| 20 (default)     | 0               | 0.228  | 0.122 | 0.185  | 0.030 | 0.297      |
| 16               | 0               | 0.228  | 0.122 | 0.185  | 0.030 | 0.297      |
| 12               | 0               | 0.228  | 0.122 | 0.185  | 0.030 | 0.297      |
| 10               | 0               | 0.228  | 0.122 | 0.185  | 0.030 | 0.297      |
| 8                | 2               | 0.221  | 0.121 | 0.178  | 0.029 | 0.419      |
| 6                | 4               | 0.198  | 0.118 | 0.162  | 0.026 | 0.549      |

Two facts explain the table:
- At k=10, `repurchase_n = min(repurchase_count, top_n)`, so every setting >= 10 collapses to
  the SAME top-10 (repurchase_n=10, discovery_n=0). Settings 20/16/12/10 are bit-identical.
  The non-zero disc_share at these settings is the `_backfill` path (co-purchase/global-popular
  fill, tagged DISCOVERY), not the Jaccard discovery stage, which never fires.
- Only repurchase_count < 10 actually injects Jaccard discovery into the evaluated window, and
  it monotonically REGRESSES hit@10 / MRR / recall / precision (8 → 0.221, 6 → 0.198). The
  held-out-basket truth is repurchase-dominated, so trading repurchase slots for discovery
  displaces correct predictions while only raising disc_share.

No swept value beats — or even matches — the committed 0.242 floor; the best is the default
itself. Lowering the quota strictly hurts. **HOLD at repurchase_count = 20** (config default,
`.env`, and the `--baseline` assertion all unchanged). The 0.228 vs 0.242 gap is DB drift
(n=430 vs 409 eligible clients now), within the 0.02 baseline tolerance and orthogonal to the
quota lever, which is flat-or-negative across the whole grid.

## real-data tuning (2026-06) — SHIPPED: recency-scale fix + per-segment weights + half-life 21

Re-measured on the leave-last-basket harness at the current DB state (n=493, k=10, synthetic
excluded). Reproduce the sweep: `.venv/bin/python -m scripts.realdata_tuning_sweep --k 10
--phase all` (weights grid) then `--phase tune` (combined + half-life + group-cap A/B).

Pre-tuning baseline at this DB state (also the recency-scale-fix arm — see below): hit@10
0.217, prec 0.028, recall 0.170, MRR 0.117. Per-segment: HEAVY 0.092 · LIGHT 0.266 ·
RC 0.187 · RE 0.148.

### 1. recency-scale correctness fix (`recommender.py`)
`freq` is max-normalized to [0,1] but `recency = exp(-days/halflife)` was not, so the nominal
`w_rec` was silently suppressed. Now both pass through `_normalize` (max-norm) so the weights
act on equal scales. On its own this is a **ranking no-op** at the old committed weights
(monotone per-client rescale didn't reorder any top-10 — measured identical to the pre-fix
baseline), but it makes weight tuning behave predictably, so it is applied first.

### 2. per-segment freq/recency weights — re-tuned (every arm = one segment changed in isolation)

| segment             | old (freq,rec) | new (freq,rec) | seg hit old→new | n   |
|---------------------|----------------|----------------|-----------------|-----|
| LIGHT               | 0.70 / 0.30    | 0.30 / 0.70    | 0.266 → 0.293   | 304 |
| REGULAR_CONSISTENT  | 0.50 / 0.35    | 0.40 / 0.60    | 0.187 → 0.213   | 75  |
| REGULAR_EXPLORATORY | 0.25 / 0.50    | 0.30 / 0.70    | 0.148 → 0.185   | 27  |
| HEAVY               | 0.60 / 0.25    | 0.40 / 0.60    | 0.092 → 0.115   | 87  |

LIGHT seg-hit saturates at 0.293 for any recency-heavy split (0.50/0.50 .. 0.25/0.75); MRR
keeps rising toward 0.75, 0.30/0.70 chosen as the balanced point. RC peaks at 0.40/0.60 (0.30/
0.70 dips to 0.200). HEAVY is NOT degenerate at this DB state (prior recon flagged it as such):
0.40/0.60 lifts it +2.3pp. Combined (all four, caps 3/3, halflife 90): hit@10 0.243, recall
0.193, MRR 0.137 — additive across the disjoint segments, no segment regresses.

### 3. recency half-life — re-tuned 90 → 21 (on combined weights)

| halflife | hit@10 | recall | MRR    |
|----------|--------|--------|--------|
| 21       | 0.2475 | 0.2025 | 0.1529 |
| 30       | 0.2434 | 0.1972 | 0.1476 |
| 45       | 0.2394 | 0.1935 | 0.1445 |
| 60       | 0.2414 | 0.1913 | 0.1405 |
| 90 (old) | 0.2434 | 0.1934 | 0.1365 |

Monotone in favour of the shortest tested half-life; 21 wins on all three metrics (matches the
~23d held-out repurchase-gap median). **Shipped halflife = 21.**

### SHIPPED combined result (recency-fix + weights + halflife 21), full harness, n=493

| metric       | before | after  | Δ        |
|--------------|--------|--------|----------|
| hit_rate@10  | 0.217  | 0.247  | +0.030   |
| precision@10 | 0.028  | 0.035  | +0.007   |
| recall@10    | 0.170  | 0.203  | +0.033   |
| MRR@10       | 0.117  | 0.153  | +0.036   |

Per-segment hit@10: HEAVY 0.092→0.103 · LIGHT 0.266→0.303 · RC 0.187→0.213 · RE 0.148→0.185.
Every metric and every segment improves; nothing regresses. Comfortably above the committed
`--baseline` floor (0.242), which is left unchanged as a conservative regression guard.

### 4. group-diversity cap (repurchase vs discovery) — NOT SHIPPED (regresses recall@10)

Decoupling the shared `max_per_group=3` into repurchase=5 / discovery=3 (measured on the
combined weights):

| k  | shared 3/3 (hit / recall) | repurchase5/disc3 (hit / recall) |
|----|---------------------------|----------------------------------|
| 10 | 0.2434 / 0.1934           | 0.2394 / 0.1896  (−0.40 / −0.38) |
| 20 | 0.2779 / 0.2240           | 0.2799 / 0.2279  (+0.20 / +0.39) |

Mixed: it helps at k=20 (the production serving regime, default_top_n=25 / worker top_n=50) but
**regresses recall@10**, the harness headline metric and the stated gate. Held at the shared
`max_per_group=3`. Worth revisiting if/when a k=20+ offline metric becomes the primary target.

### out of scope this pass
The co-purchase blend/cap (`copurchase.py`) was left untouched: validating it needs harness
`--compare` wiring plus a cross-sell metric the harness does not yet have.

## precision_estimate honesty fix

The contract field `RecommendationResult.precision_estimate` was a hardcoded **0.754**,
contradicted by the harness by ~23x. It is now the **harness-derived** precision@10 for the
v3.2 model (**0.033**, full n=409 run above), documented as a model-level offline metric, not
a per-call confidence. (The .NET DTO field `PrecisionEstimate` is a non-nullable `double`, so
the value is kept honest rather than omitted/nulled, which would break deserialization.)

## byRegion toggle — VERDICT: HOLD (recommend removing the dead toggle)

The console `byRegion` toggle reaches gba-server (`GetForClientByNetIdAsync(clientNetId,
byRegion)`) but is dropped there (never forwarded to gba-reco), and gba-reco had no region
awareness at all.

Region scoping was implemented end-to-end (opt-in `region_scope`): the discovery neighbour
pool is restricted to the client's oblast via the natural key `dbo.Client.RegionID`
(repurchase is the client's own history, so it is region-invariant and unscoped; fail-open
when a client has no region). Data: among 1623 ordering clients, 1602 have a region, spread
over **26** oblasts (`RegionID`) — `RegionCodeID` is per-client address granularity (1602
distinct ~= one per client) and does **not** group, so scoping must use `RegionID`.

A/B (n = 90, k = 10): v3.2 and v3.2_byRegion are **identical** (hit 0.222 / recall 0.143 /
prec 0.029 / MRR 0.107). Reason: at the harness/serving default the repurchase quota
(`repurchase_count = 20`) saturates the top-10, so discovery (the only region-scoped stage)
never enters the evaluated window; and the held-out-basket truth is repurchase-dominated, so
narrowing discovery to one oblast cannot help on this metric. Region scoping only changes
output for larger `top_n` (discovery firing) and even then narrows, not improves, candidates.

Recommendation: HOLD. Do not adopt region scoping as a default. Either remove the dead
console toggle, or — if the toggle must stay — wire `region_scope` through the .NET DTO +
controller and gate it behind explicit product sign-off, since it is at best neutral on the
current eval and removes cross-region discovery signal. The implementation is left in place
behind an opt-in flag (default off) so it is a no-op unless explicitly requested.
