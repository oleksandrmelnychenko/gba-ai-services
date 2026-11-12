# gba-products — statistical review, 2026-11-12

Scope: ABC/XYZ, lifecycle, stock bands, health score, action labels. Data: DEV `ConcordDb_V5`,
canonical confirmed invoices (the service's own sales definition) from 2025-01-01 to 2026-11-11:
238,329 lines, 15,825 SKUs with sales, 7.85M EUR; 2,253 return lines; current stock 11,316 sellable
SKUs / 1.94M EUR at cost. The analysis replica reproduces the live service counts exactly
(lifecycle, ABC, XYZ, bands), so the tables below describe the live numbers.

Method: rolling origins (as-of dates in the past, outcome = the following 90 or 180 days), intervals
from a cluster bootstrap over SKUs (95%). Stock history is not stored, so every rolling-origin
result uses sales only; the stock bands are checked on today's snapshot plus lot age.

## Conclusions

1. **XYZ was wrong for half of its best class — fixed (v10).** The old method (CV of gaps between
   selling months) gave CV = 0 to any SKU that sold in exactly two months, so 2,370 of 4,707 "X —
   stable" SKUs were two-sales-a-year items, and X sold *less* often afterwards than Y. X/Y/Z now
   follow how many months the SKU sells in.
2. **"Growing" / "declining" describe the past and do not predict the future.** Over 15 origins,
   next-90-day sales relative to the trailing-12-month pace: growing 1.16 [1.09, 1.23], mature 1.10
   [1.06, 1.14], declining 1.05 [1.00, 1.11]; the gap growing − mature is 0.06 [−0.01, 0.13].
   Relative to their own last six months, growing SKUs fall back to 0.77 and declining ones rebound
   to 1.98 (regression to the mean). Only about 170 growing and 75 declining labels per origin
   survive a 10% false-discovery test on invoice counts, out of about 2,500 and 1,500. Two defects in
   the rule were fixed (v8, v9); the rule itself is left as a descriptive label — see decisions.
3. **"Dead" is not final.** A SKU with no sale for 365+ days sells again within 180 days in 32%
   [30, 35] of cases (17% within 90 days). 875 of the 3,416 dead-band SKUs (122k of 384k EUR) hold
   stock received less than a year ago, i.e. they have not had a year to sell.
4. **The health score is a triage index, not a predictor.** Without its stock component it ranks
   next-90-day revenue worse (rho 0.41) than last year's revenue alone (0.50) or the invoice count
   alone (0.51). With the corrected XYZ it improves to 0.46. Weights are not sensitive (rank
   correlation 0.955 under perturbed weights). No weight change is made.
5. ABC 80/95 is a reasonable description of the Pareto curve: 21.5% [20.2, 22.7] of SKUs give 80%
   of revenue, 51.3% give 95%. Class boundaries are low in money terms (A starts at 312 EUR/year).

## B1. ABC and XYZ

Pareto, trailing 365 days, 13,364 SKUs with revenue:

| basis | SKUs for 50% | 80% | 90% | 95% | Gini |
|---|---|---|---|---|---|
| revenue | 4.7% | 21.5% [20.2, 22.7] | 36.9% | 51.3% [50.1, 52.4] | 0.754 |
| units | 1.9% | 11.8% [10.4, 13.4] | 24.7% | 39.9% | 0.838 |
| margin (cost known, 95% of revenue) | 4.1% | 19.4% [18.1, 20.8] | 33.9% | 47.8% | 0.774 |

- Revenue-ABC and margin-ABC agree for 83.6% of SKUs; revenue-ABC and units-ABC for 56.7%. A units
  ABC would be a different ranking; a margin ABC would not change much. 662 SKUs have a negative
  margin at current cost.
- A starts at 312 EUR/year, B at 88 EUR/year, median C is 31 EUR/year. 234 of 2,874 A SKUs have
  three invoices or fewer in the year; 261 sold in two months or fewer.
- Month-to-month stability (1st of each month, 2026): ABC changes for 6.1% of SKUs (5.0–6.7%);
  A stays A 94.6%, B stays B 90.1%, C stays C 96.0%.

XYZ. Selling months in the 12-month grid: 1 month 3,516 SKUs, 2 months 2,370, 3 months 1,703,
9–12 months 1,440. Demand is intermittent for almost the whole catalogue (Syntetos–Boylan on
monthly data: intermittent 10,863, lumpy 1,507, smooth 641, erratic 353), so a CV-type measure
separates little: classic CV 0.5/1.0 puts 88% in Z.

Predictive check (8 origins, SKUs with sales in the trailing year, outcome = next 90 days; WAPE of
a trailing-rate forecast):

| method | class | SKUs/origin | P(sale in 90 d) | WAPE | median selling months |
|---|---|---|---|---|---|
| interval CV 0.3/0.6 (old) | X | 4,380 | 56.4% | 0.57 | 2 |
| | Y | 3,111 | 71.0% | 0.70 | 5 |
| | Z | 5,310 | 40.2% | 0.91 | 1 |
| interval CV, X/Y need 4+ selling months | X / Y / Z | 1,546 / 2,530 / 8,725 | 82.8% / 75.4% / 41.6% | 0.51 / 0.68 / 1.01 | 10 / 6 / 2 |
| frequency: 9+ / 4–8 / 1–3 months (new) | X | 1,274 | 93.6% | 0.47 | 10 |
| | Y | 3,967 | 72.4% | 0.79 | 5 |
| | Z | 7,560 | 36.4% | 1.22 | 2 |

AUC of the class order for "sells in the next 90 days": 0.584 old, 0.663 with a 4-month minimum,
0.713 frequency. P(sale in 90 d) rises monotonically with the number of selling months (1: 28%,
3: 52%, 6: 76%, 9: 91%, 11: 97%). Month-to-month class changes: 17.1% old, 6.4% frequency.
The 75% cut is the monthly equivalent of the standard ADI 1.32 boundary (12/9 = 1.33).

## B2. Lifecycle

Current rule: 12 monthly buckets, recent six vs prior six; growing if recent ≥ 1.2 × prior,
declining if recent ≤ 0.5 × prior, both only with 6+ units across the two halves; no sale in the
recent half → declining once the last sale is 9+ months back; first sale within 90 days → new; no
sale in 365 days → dead.

Defects found and fixed:

- **Partial month.** The last bucket is the as-of month to date. On the 1st the recent half holds
  five months against six. Trend-eligible labels over 15 origins: on the 1st growing 2,399 /
  declining 1,574; on the 16th 2,626 / 1,473. With daily rates: 2,891 / 1,138 and 2,810 / 1,163.
- **"New" looked only inside the 12-month window.** 348 of 969 "new" SKUs on 2026-11-12 had sales
  in January–November 2025 (15–34% across origins, rising with the length of visible history).

Alternatives tested on the trend-eligible SKUs (about 8,700 per origin), next 90 days vs trailing
12-month pace, ratio of sums:

| rule | growing/origin | declining/origin | growing | mature | declining | growing − mature | mature − declining |
|---|---|---|---|---|---|---|---|
| current (v7) | 2,505 | 1,527 | 1.16 [1.09, 1.23] | 1.10 [1.06, 1.14] | 1.05 [1.00, 1.11] | 0.06 [−0.01, 0.13] | 0.05 [0.00, 0.11] |
| daily rates (v8) | 2,853 | 1,149 | 1.15 [1.08, 1.21] | 1.10 [1.06, 1.14] | 1.04 [0.98, 1.12] | 0.05 [−0.01, 0.11] | 0.06 [0.00, 0.12] |
| Poisson rate ratio on units, FDR 10% | 1,163 | 626 | 1.14 [1.07, 1.24] | 1.13 [1.11, 1.16] | 1.04 [0.98, 1.11] | 0.01 [−0.07, 0.10] | 0.10 [0.03, 0.15] |
| Poisson rate ratio on invoices, FDR 10% | 166 | 75 | 1.20 [0.96, 1.38] | 1.11 [1.08, 1.15] | 0.90 [0.74, 1.14] | 0.09 [−0.16, 0.30] | 0.21 [−0.01, 0.36] |
| invoices p < 0.05 and the 1.2 / 0.5 factors | 568 | 190 | 1.16 [1.02, 1.28] | 1.11 [1.08, 1.15] | 0.95 [0.84, 1.08] | 0.05 [−0.09, 0.16] | 0.16 [0.03, 0.27] |
| Mann–Kendall on monthly rates, FDR 10% | 0 | 0 | — | 1.11 | — | — | — |

- Units are not independent events (one invoice can carry 50 units), so the units test is too
  permissive; the invoice-count test is the defensible one. Twelve monthly points are too few for
  Mann–Kendall to survive a false-discovery control over ~9,000 SKUs.
- No rule yields a "growing" label that predicts growth. The share of SKUs whose next-90-day pace
  beats the trailing year is 41–43% in every class. The strict rules separate "declining" slightly
  better, with wide intervals. This is not enough to replace the rule.
- The dormancy branch is informative: P(sale in 90 d) is 56.5% for trend-eligible mature, 33.6% for
  a 6–8-month lull (mature) and 23.3% for a 9+-month fade (declining).

## B3. Dead stock, slow, cover bands

P(sale) by days since the last sale (14 monthly origins, SKUs with at least one sale since 2025):

| last sale | SKUs/origin | P(sale in 90 d) | P(sale in 180 d) |
|---|---|---|---|
| 0–30 d | 4,451 | 76.6% [76.1, 77.0] | 85.9% [85.5, 86.2] |
| 31–90 d | 3,137 | 51.1% [50.5, 51.7] | 69.2% [68.5, 69.9] |
| 91–180 d | 2,466 | 38.0% [37.2, 38.8] | 57.3% [56.3, 58.3] |
| 181–270 d | 1,297 | 30.2% [29.3, 31.2] | 46.1% [44.7, 47.4] |
| 271–365 d | 723 | 22.2% [21.2, 23.6] | 37.1% [35.3, 38.9] |
| 366–450 d | 333 | 18.2% [16.5, 19.6] | 32.8% [29.8, 35.7] |
| 451–540 d | 140 | 14.4% [12.7, 17.0] | 27.8% [22.6, 33.5] |

There is no cliff at 180 or 365 days; the probability decays smoothly. SKUs that never sold since
2025 are outside this table (2,653 of the 3,416 dead-band SKUs, 289k EUR) and are deader than it
suggests.

"Slow" (annualised 180-day units ≤ 5): P(sale in 180 d) is 43% at zero units, 53% at ≤ 3, 58% at
3–5, 70% at 5–10, 81% at 10–25. The cut at 5 sits on a smooth curve.

Today's bands (replica = live):

| band | SKUs | EUR at cost | median lot age, days | median cover, days |
|---|---|---|---|---|
| dead | 3,416 | 384k | 839 | — |
| slow | 3,262 | 434k | 449 | 360 |
| overstock (> 180 d) | 1,962 | 658k | 210 | 360 |
| healthy (7–180 d) | 2,617 | 458k | 177 | 88 |
| understock (< 7 d) | 59 | 0.5k | 218 | 4 |

- Cover days for SKUs with demand: p10 45, p25 90, median 180, p75 469, p90 1,080. Weighted by
  stock value: p25 138, median 313, p75 810. Half of the stock value covers more than ten months.
- Threshold sensitivity: overstock > 90 d → 3,115 SKUs / 940k EUR; > 365 d → 957 / 387k.
  Understock < 15 d → 158 SKUs; < 30 d → 372. Slow ≤ 3 → 2,305 / 296k; ≤ 10 → 4,275 / 584k.
  Dead window 180 d → 4,529 / 535k; 270 d → 3,791 / 431k.
- Lot age by band (k EUR; ≤ 90 d / 91–180 / 181–365 / 1–2 y / 2–5 y / 5 y+): dead 49 / 24 / 47 /
  86 / 107 / 71; slow 53 / 42 / 78 / 92 / 130 / 38; overstock 176 / 129 / 111 / 75 / 141 / 27;
  healthy 236 / 110 / 59 / 28 / 23 / 2. The bands agree with lot age in direction; 120k EUR of
  "dead" stock is under a year old.
- `cover_target_min_days` (15) is not used by any rule; the healthy band is 7–180 days.

## B4. Health score

Components (stock band, trend, margin, stability, returns, ABC) are weakly correlated with each
other (Spearman ≤ 0.24), so the composite is not six copies of one signal.

Predictive check, 8 origins, 102,407 SKU-origins, stock component excluded:

| signal | rho with next-90-day revenue | AUC "sells in 90 d" |
|---|---|---|
| health (v7 weights, old XYZ) | 0.412 [0.401, 0.421] | 0.666 [0.661, 0.673] |
| health (same weights, frequency XYZ) | 0.459 [0.448, 0.468] | 0.691 [0.687, 0.697] |
| demand score, old XYZ → frequency XYZ | 0.430 → 0.490 | 0.665 → 0.696 |
| trailing-365-day revenue alone | 0.499 [0.490, 0.510] | 0.677 |
| invoice count in 365 days alone | 0.511 [0.503, 0.520] | 0.761 |
| ABC / stability (old) / trend / margin / returns alone | 0.467 / 0.181 / 0.138 / 0.114 / −0.108 | 0.650 / 0.584 / 0.574 / 0.558 / 0.479 |

- Controlling for trailing revenue, the composite adds a partial rho of 0.096 [0.085, 0.110];
  stability 0.051, trend 0.053.
- Standardised logistic coefficients for "sells in 90 d": ABC 0.55, margin 0.16, stability 0.15,
  trend 0.13, returns 0.00. Current weights (ABC 0.22, margin 0.20, trend 0.18, stability 0.12,
  returns 0.03, stock 0.25) are in the same order apart from trend.
- Weight sensitivity: with weights perturbed around the current ones the ranking keeps a rank
  correlation of 0.955 (5th percentile 0.857); the worst fifth overlaps 84%, the top tenth 81%.
  Equal weights: 0.958.
- Margin: trailing margin % vs next-90-day margin %, rho 0.54 (current cost on both sides).
- Returns: SKUs flagged at ≥ 5% trailing return rate return 2.9% of units in the next 90 days
  against 0.42% [0.32, 0.50] for the rest. 67% of the flags rest on one returned unit, and those
  are as predictive (2.70% [2.18, 3.41]) as flags on two or more units (3.01% [1.35, 5.08]). No
  minimum-evidence rule is supported.
- The score deciles are ordered (P(sale in 90 d) 29% in the lowest tenth, 79% in the highest), with
  local reversals in deciles 2–3 and 7–8.

Reading: the score sorts the catalogue for review. It is not a forecast and should not be read as
one; a manager who wants "what will sell" is better served by the invoice count. The stock component
could not be back-tested (no stock history).

## Implemented

| commit | change | model version |
|---|---|---|
| 8a38e72 | lifecycle halves compared as daily rates | v8 |
| 6ec525e | first sale dated over the whole factual history | v9 |
| d14b155 | XYZ by demand frequency (X ≥ 75% of months, Y ≥ 33%) | v10 |

Before / after on DEV, as-of 2026-11-12, 16,781 SKUs (ABC and bands unchanged):

| | before (v7) | after (v10) |
|---|---|---|
| lifecycle: mature / growing / declining / new / dead | 7,038 / 2,957 / 2,400 / 969 / 3,417 | 7,380 / 3,213 / 2,150 / 621 / 3,417 |
| XYZ: X / Y / Z | 4,707 / 3,477 / 8,597 | 1,440 / 4,192 / 11,149 |
| action: to_order_candidate / monitor / keep_push / monitor_decline | 1,461 / 4,063 / 1,174 / 843 | 1,030 / 4,457 / 1,180 / 874 |
| health, mean | 52.4 | 51.2 (rank correlation with before 0.975) |
| demand score, mean | 52.1 | 49.3 (rank correlation 0.940) |

818 SKUs changed lifecycle (250 declining → mature, 220 mature → growing, 348 new → mature or
growing), 5,808 changed XYZ (2,990 X → Z, 707 X → Y, 1,043 Z → Y, 397 Y → X), 914 changed action
label (508 to_order_candidate → monitor). The response shape and all enum values are unchanged.

## Decisions for the owner

1. **Trend labels.** Keep growing/declining as a plain description of the last six months vs the
   six before (current), or show them only when the change is statistically distinguishable
   (invoice-count test, p < 0.05, plus the 1.2 / 0.5 factors): about 570 growing and 190 declining
   instead of 3,213 and about 1,150 trend-based declining. The strict version marks fewer SKUs and
   its "declining" is slightly more reliable; neither version predicts growth.
2. **Dead stock younger than a year.** 875 SKUs / 122k EUR are in the dead band with stock received
   under 365 days ago. A separate "new stock, no sales yet" band needs a new band value in the
   service, the server validator and the console.
3. **Overstock cut.** 180 days of cover marks 658k EUR; 365 days marks 387k EUR. With the 30/18/7-day
   lead times used by procurement, 180 days is already generous; the choice is how long a list the
   buyers can work through.
4. **Dead window.** At 365 days a third of the flagged SKUs sell again within six months. A 540-day
   "write-off candidate" tier would be cleaner (28% [23, 34]) but the history is too short to
   measure beyond that.

## Parameters

Origin: all but the last two came with the initial import of the service (18c2fa1, 2026-06-27) with
no recorded calibration other than the code comments; `lifecycle_min_trend_units` is from a4918b4
(2026-11-12).

| parameter | value | sensitivity | recommendation |
|---|---|---|---|
| `abc_a_share` / `abc_b_share` | 0.80 / 0.95 | 21.5% / 51.3% of SKUs; 6% change class monthly | keep |
| ABC basis | 365-day gross revenue | margin basis agrees 84%, units basis 57% | keep |
| `classify_months` | 12 | — | keep; no evidence either way |
| `xyz_method` | interval → **frequency** | AUC 0.584 → 0.713 | changed |
| `xyz_x_min_share` / `xyz_y_min_share` | 0.75 / 0.33 (new) | P(sale) 94% / 72% / 36% | new |
| `xyz_x_cut` / `xyz_y_cut` | 0.3 / 0.6 | used only by the old methods | keep for fallback |
| `lifecycle_trend_months` | 6 | — | keep; no evidence either way |
| `lifecycle_growing_factor` / `lifecycle_declining_factor` | 1.2 / 0.5 | labels not predictive at any cut tested | keep; decision 1 |
| `lifecycle_min_trend_units` | 6 | — | keep |
| `lifecycle_dormant_decline_months` | 9 | P(sale 90 d) 34% at 6–8 months, 23% at 9+ | keep |
| `lifecycle_new_days` | 90 | — | keep |
| `lifecycle_first_sale_months` | 60 (new) | 36% of "new" were returning SKUs | new |
| `velocity_window_days` | 180 | — | keep; no evidence either way |
| `dead_window_days` | 365 | 180 d: +1,113 SKUs / +151k EUR | keep; decision 4 |
| `cover_overstock_days` | 180 | 90 d: 940k EUR; 365 d: 387k EUR | decision 3 |
| `cover_understock_days` | 7 | 15 d: 158 SKUs; 30 d: 372 | align with procurement lead times once they are known |
| `slow_max_annual_units` | 5 | 3: 296k EUR; 10: 584k EUR | keep |
| `return_window_days` / `returns_high_min_rate` | 365 / 0.05 | flagged SKUs return 7× more | keep |
| `margin_target` / `return_rate_cap` | 0.30 / 0.20 | scaling only | no evidence either way |
| `w_stock`, `w_trend`, `w_margin`, `w_stability`, `w_returns`, `w_abc` | 0.25, 0.18, 0.20, 0.12, 0.03, 0.22 | rank correlation 0.955 under perturbation | keep |
| demand-score weights (in code) | abc 0.40, stability 0.25, trend 0.20, stock 0.15 | — | keep |
| margin-score weights (in code) | margin 0.70, returns 0.20, abc 0.10 | — | keep |
| band / lifecycle / XYZ / ABC sub-scores (in code) | e.g. healthy 1.0, overstock 0.4, dead 0 | not back-testable without stock history | keep |
| action thresholds (in code) | demand ≥ 70, margin ≥ 60, demand < 50 | 508 SKUs left to_order_candidate after the XYZ fix | keep |
| `cover_target_min_days`, `trend_recent_months`, `seasonal_lookback_days` | 15, 3, 730 | not read by any rule | remove in a cleanup |
| `cache_ttl`, `product_facts_refresh_seconds` | 90,000 s, 21,600 s | freshness only | keep |

## Limits

- 22 months of history: lifecycle and XYZ need 12, so rolling origins start in January 2026.
- No stock history: band rules and the stock component of the score are not back-tested. A daily
  stock snapshot table would allow it.
- Costs are today's lot costs; margin checks use them for past dates too.
- Sales are observed sales, not demand: stock-outs are invisible.
- `docs/product-health-backtest-baseline.json` predates v10 and builds its snapshot on current
  stock; it should be regenerated before it is used as a gate again.

Scripts and outputs: `/root/.cache/claude-ai-stats/four/products/` (`core.py`, `b1.py`, `b2.py`,
`b2_eval.py`, `b3.py`, `b4.py`, `b4b.py`, `b4c.py`, `b4d.py`, `cmp.py`, `*.txt`).
