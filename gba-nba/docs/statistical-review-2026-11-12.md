# NBA statistical review — 2026-11-12

Scope: the propensity model behind `p_outcome`, the EUR expected value, the ranking and quotas,
and every tunable constant of `gba-nba`. Data: `data/nba_dataset.parquet` (the served model's
training set) plus a local replica of the candidate logic rebuilt from DEV `ConcordDb_V5` order
lines for nine monthly vintages, 2026-01-01 … 2026-09-01 (the replica reproduces the parquet:
3,262 / 3,570 / 3,996 / 4,286 / 4,413 reorder rows vs 3,259 / 3,565 / 3,996 / 4,280 / 4,406, same
base rates). Scripts and extracts: `/root/.cache/claude-ai-stats/sn/nba/`.

All intervals are 95%. Intervals on AUC and rates resample whole clients, because one client
contributes many correlated rows.

## 1. Conclusions

1. **The model does not beat a one-line rule.** Out of time, the reorder head scores AUC 0.689
   [0.658, 0.711]; "orders of that product ÷ days since the last one" scores 0.683 [0.651, 0.705].
   The churn head scores 0.830 [0.780, 0.877]; "fewer days of silence" alone scores 0.836
   [0.786, 0.879]. On six rolling test months the picture is the same (reorder 0.668 vs 0.666,
   churn 0.772 vs 0.776). The 21-feature model adds nothing measurable over recency and frequency.
2. **For reorder tasks `p_outcome` answers a different question from the one the task asks.** The
   task says "call this client about restocking" and lists up to five products. The probability
   shown is the chance that the single most overdue product is bought again within 60 days:
   17% on average. Within the same 60 days 41% of those clients buy at least one of the five
   listed products, 50% buy at least one due product, and 87% place some order. The most overdue
   product is the least likely one to be bought again (22% at 2.5–3 cycles overdue vs 28% when
   just due), so the task leads with its weakest line.
3. **Probabilities are too high for reorder, about right for churn.** Reorder rows: predicted
   0.289 vs realised 0.233 over six test months; rows shown as ≥ 0.60 realised 0.55 (interval
   0.45–0.68). The cause is not the calibrator: isotonic, Platt and uncalibrated logistic give the
   same gap (0.72 → 0.55, 0.72 → 0.58, 0.74 → 0.56). The causes are a falling base rate (35% in
   February–March vintages, 18% in September) and concentration: 74 clients make up the top bin,
   five of them hold 58% of its rows, ten clients hold 40% of all training rows. Churn: predicted
   0.324 vs realised 0.335, top bin 0.68 predicted vs 0.75 realised.
4. **Two of the four task types have no probability at all.** Debt follow-up has 21 historical
   rows because `Debt` holds only debts that are still open today, so history cannot be replayed;
   cross-sell has 0 rows. Since today both show a rule-based index (live values 0.51–0.69). That
   number is not a probability and should not be read as a percentage.
5. **The EUR estimate for churn is now unbiased; for reorder it is inconsistent.** Churn, after
   today's 60-day normalisation: predicted ÷ realised 60-day revenue = 0.84 [0.63, 1.10]; before
   today it was 5.1 [3.8, 6.7]. Reorder: probability of one product × the client's whole average
   order overstates that product's revenue 2.75× [1.9, 4.2], and understates the revenue of all due
   products 6× (0.17 [0.13, 0.24]). Per-task accuracy is poor either way (median error 75%, rank
   correlation 0.29).
6. **Ranking by p × EUR picks clients who buy anyway.** The eight churn tasks each manager holds
   today have median silence 21 days and median `p_outcome` 0.72; among reorder-task clients 32%
   order within one day of the task date, 63% within the six-day due date. Nothing in the data can
   say whether a call changes anything: there are 0 recorded task outcomes and no untreated
   comparison group. Every "value" number here is revenue that follows a task, not revenue caused
   by it.
7. **The quotas (40/27/16/17) cannot be tested on history** — debt and cross-sell candidates do
   not exist historically. Within the reorder and churn slots, every ranking rule tried beats
   random 3–4×; a per-product EUR estimate is modestly better than the current one (+8% task EUR
   or +10 points of tasks followed by a purchase, section 6).
8. **More data will not help the current design.** Churn quality is flat from about 400 training
   rows (AUC 0.826 at 376 rows, 0.833 at 1,177). The limit is the information in the features, not
   the row count. What would help is recorded outcomes and a holdout.

## 2. Recommended design

| Item | Now | Recommended | Evidence |
|---|---|---|---|
| Reorder probability | P(most overdue product rebought), 17% base, AUC 0.68 [0.61, 0.74] on served rows | P(client buys ≥ 1 due product in 60 days) from three inputs: number of due products, days since last order, orders in 365 days | AUC 0.862 [0.840, 0.885] out of time, predicted 0.533 vs realised 0.502; flat learning curve (0.849 at 266 rows) |
| Reorder EUR | client average order value | Σ over due products of P(rebuy) × that product's average line value | rank correlation with realised due-product revenue 0.72 vs 0.38; total 0.65 of realised vs 0.17 |
| Churn probability | 7-feature slice of the pooled model | keep, or replace by `logit = 4.994 − 1.207·ln(1 + silence_days)`; identical quality | AUC 0.774 vs 0.772 over six months |
| Debt, cross-sell number | rule index shown as `p_outcome` | show as a score, not a percentage, until outcomes exist | no labelled rows |
| Calibration | isotonic, 5 folds | no calibrator swap; the task-level head above over-predicts by 5–9 points between 0.2 and 0.8 and is exact at the ends | section 4 |
| Training window | 5 vintages | no change | learning curves flat |
| Retrain gate | pooled AUC ≥ 0.62 and ≥ old − 0.01 | add "head AUC ≥ baseline AUC − 0.01" per type, from the new `oot_honest` block | section 3 |

Closed-form reorder head fitted on nine closed vintages (2,281 client-vintages):
`logit = 0.489 + 1.361·ln(n_due) − 0.499·ln(1 + recency_days) − 0.303·ln(1 + order_count_365)`.
For the five listed products: `0.064 + 0.550·ln(n_due) − 0.366·ln(1 + recency_days) −
0.112·ln(1 + order_count_365)` (AUC 0.740, predicted 0.453 vs realised 0.413, over-predicts above
0.8: 0.85 vs 0.62 [0.53, 0.70]).

## 3. Propensity per task type (verified by running)

Production split (train ≤ 2026-07-01, test 2026-08-01 and 2026-09-01), served logistic + isotonic:

| Type, vintage | Rows | Clients | Base rate | Mean predicted | AUC | 95% interval |
|---|---:|---:|---:|---:|---:|---|
| reorder 2026-08 | 4,280 | 282 | 0.231 | 0.244 | 0.670 | 0.641–0.702 |
| reorder 2026-09 | 4,406 | 287 | 0.178 | 0.235 | 0.707 | 0.674–0.734 |
| reorder both | 8,686 | 305 | 0.204 | 0.239 | 0.689 | 0.658–0.711 |
| reorder, served rows (one per client) | 569 | 305 | 0.144 | 0.179 | 0.683 | 0.610–0.739 |
| churn 2026-08 | 190 | 190 | 0.347 | 0.352 | 0.839 | 0.781–0.897 |
| churn 2026-09 | 189 | 189 | 0.381 | 0.343 | 0.820 | 0.751–0.878 |
| churn both | 379 | 236 | 0.364 | 0.348 | 0.830 | 0.780–0.877 |
| debt | 8 | 4 | — | — | not estimable | |
| cross-sell | 0 | 0 | — | — | not estimable | |
| pooled | 9,073 | | 0.211 | | 0.700 | 0.677–0.722 |

Brier: reorder 0.1506 against 0.1626 for a constant; churn 0.1614 against 0.2315.

Rolling evaluation, six test months (2026-04 … 2026-09), each trained only on vintages whose
60-day label window had closed:

| Model | Reorder rows AUC | Churn AUC |
|---|---|---|
| Served design (logistic + isotonic) | 0.668 [0.642, 0.697] | 0.772 [0.731, 0.810] |
| Best one-line rule | 0.666 [0.645, 0.685] (orders ÷ elapsed days) | 0.776 [0.741, 0.814] (−silence days) |
| Two-feature logistic | 0.667 [0.647, 0.686] | 0.769 [0.729, 0.806] |
| Gradient boosting | 0.651 [0.626, 0.679] | not run |
| Other single signals | n_orders 0.645, −cycle 0.642, −elapsed 0.649, −overdue ratio 0.534 | prior orders 0.686, recent orders 0.706 |

Per-vintage reorder AUC ranges 0.625–0.690; June 2026 is the weakest (0.625 [0.563, 0.691], Brier
worse than a constant).

Leakage checks:

- Cross-validation is grouped by client (`StratifiedGroupKFold`) — correct.
- The out-of-time split trains on a vintage whose label window (T, T+60] overlaps the test
  vintage by a month. Measured effect: AUC 0.669 with the overlap vs 0.668 without. Negligible.
- The calibrator's internal 5 folds are not grouped by client. Not measured separately; all three
  calibration variants give the same result, so it does not drive the over-prediction.
- Clients repeat: 483 churn clients over 1,757 rows (3.6 vintages each); 93% of reorder-task
  clients are candidates again a month later. Row counts overstate the information.

Coefficient stability of the served pooled logistic (standardised, 120 client resamples):
stable sign — recency −0.49 [−1.16, −0.09], cycle days −0.28 [−0.43, −0.13], overdue ratio −0.17
[−0.30, −0.06], product order count +0.39 [0.26, 0.50], recent orders +0.11. Unstable sign (share
of resamples with the opposite sign): silence days 37%, debt indicator 36%, order count 27%,
monetary 19%, elapsed days 16%, the three debt signals 6–16%. Four of 21 features are constant
zero (cross-sell columns), four more carry 21 rows.

Learning curves (test 2026-08 and 2026-09, training on the k latest closed vintages):

| k | Churn rows | Churn AUC | Reorder-task rows | Reorder task AUC (any due product) |
|---:|---:|---:|---:|---:|
| 1 | 191 | 0.815 | 266 | 0.849 |
| 2 | 376 | 0.826 | 528 | 0.857 |
| 3 | 559 | 0.836 | 781 | 0.856 |
| 6 | 1,177 | 0.833 | 1,470 | 0.859 |

Training weighted by client, or only on served rows, does not improve served-row quality
(AUC 0.680 / 0.679 vs 0.687; mean predicted 0.193 / 0.192 vs 0.199, realised 0.169).

## 4. Calibration (verified by running)

Reorder rows, six rolling test months, isotonic:

| Predicted bin | Rows | Clients | Mean predicted | Realised | Wilson interval |
|---|---:|---:|---:|---:|---|
| 0.0–0.1 | 514 | 93 | 0.047 | 0.041 | 0.027–0.062 |
| 0.1–0.2 | 6,564 | 276 | 0.164 | 0.138 | 0.130–0.147 |
| 0.2–0.3 | 5,830 | 251 | 0.252 | 0.188 | 0.179–0.199 |
| 0.3–0.4 | 4,918 | 204 | 0.343 | 0.268 | 0.255–0.280 |
| 0.4–0.5 | 2,162 | 129 | 0.448 | 0.323 | 0.304–0.343 |
| 0.5–0.6 | 1,024 | 81 | 0.541 | 0.405 | 0.376–0.436 |
| 0.6–0.7 | 626 | 65 | 0.639 | 0.454 | 0.415–0.493 |
| 0.7–0.8 | 524 | 40 | 0.733 | 0.567 | 0.524–0.609 |
| 0.8–1.0 | 265 | 28 | 0.894 | 0.747 | 0.692–0.796 |

The Wilson intervals treat rows as independent and are too narrow; resampling clients, the
realised rate for predicted ≥ 0.6 is 0.452–0.675.

| Calibrator | AUC | Brier | Mean predicted | Rows ≥ 0.6: predicted → realised |
|---|---:|---:|---:|---|
| isotonic (served) | 0.669 | 0.1719 | 0.303 | 0.722 → 0.551 |
| Platt | 0.670 | 0.1710 | 0.304 | 0.717 → 0.578 |
| none | 0.668 | 0.1705 | 0.289 | 0.737 → 0.560 |
| isotonic, 3 latest vintages only | 0.667 | 0.1714 | 0.292 | 0.729 → 0.536 |

Served reorder rows (one per client): predicted 0.217 vs realised 0.169; bins 0.1–0.2: 0.160 →
0.120 [0.098, 0.146]; 0.2–0.3: 0.250 → 0.186 [0.151, 0.226]; 0.4–0.6: 0.483 → 0.338
[0.240, 0.451]; ≥ 0.6: 0.754 → 0.619 [0.409, 0.792] (21 rows).

Churn, isotonic: 0.0–0.1: 0.056 → 0.101 [0.066, 0.151]; 0.2–0.3: 0.251 → 0.251; 0.3–0.4:
0.345 → 0.310; 0.6–0.7: 0.655 → 0.720 [0.645, 0.784]; 0.7–0.8: 0.730 → 0.851 [0.723, 0.926].
Slight under-prediction at the top, within intervals elsewhere.

## 5. Labels and what cannot be measured

Base rates within 60 days (nine vintages):

| Population | Rate |
|---|---:|
| Reorder: most overdue product rebought (the served label) | 18.1% |
| Reorder: ≥ 1 of the five listed products | 42.3% |
| Reorder: ≥ 1 due product | 50.7% |
| Reorder-task client places any order | 87.3% |
| Churn candidate places any order | 35.8% |
| Any client active in the last 365 days places an order | 58.1% |
| Active client that is neither a reorder nor a churn candidate | 50.2% |

Uplift — the extra purchases a call causes — cannot be measured from this data: nobody acted on
tasks, 0 outcomes are recorded, and there is no group that was deliberately not called. The label
is "bought or paid within 60 days", most of which happens without any task.

Minimal experiment once managers use the cockpit: per manager, randomly withhold 20% of admitted
tasks (hidden, logged), compare 60-day outcomes. Sample sizes for 80% power, 5% two-sided:

| Outcome | Base | +5 points: held-out / total tasks | +10 points: held-out / total |
|---|---:|---|---|
| Reorder, ≥ 1 due product | 0.50 | 978 / 4,890 | 242 / 1,210 |
| Reorder, any order | 0.87 | 364 / 1,820 | 65 / 324 |
| Churn, any order (all candidates) | 0.34 | 909 / 4,543 | 233 / 1,163 |
| Churn, any order (the high-value ones admitted today) | 0.67 | 831 / 4,154 | 197 / 982 |

Supply is about 253 reorder-task clients and 195 churn clients a month across all managers, so a
+10-point effect needs roughly five months of reorder tasks or six of churn tasks at a 20% holdout;
a 50/50 split needs three to four (388 and 372 per arm). Revenue as the outcome is impractical:
60-day revenue per client has a coefficient of variation of 2.0 (reorder) and 3.8 (churn), so
detecting +20% needs 1,624 and 5,667 clients per arm (955 and 5,057 after adjusting for prior
turnover).

Debt follow-up label ("≥ 50% of the overdue amount paid in 60 days") cannot be validated: `Debt`
and `ClientInDebt` keep only currently open debts (2,605 live rows, 2 deleted), so a historical
snapshot contains only debts that were never paid.

## 6. Expected value and ranking (verified by running)

Six rolling test months, 1,607 reorder tasks and 1,128 churn candidates.

| Estimate vs realised 60-day EUR | Total ratio | Interval | Median error | Rank correlation |
|---|---:|---|---:|---:|
| Reorder p × AOV vs revenue of the most overdue product | 2.75 | 1.89–4.17 | 73% | 0.19 |
| Reorder p × AOV vs revenue of the five listed products | 0.80 | 0.59–1.10 | 75% | 0.29 |
| Reorder p × AOV vs revenue of all due products | 0.17 | 0.13–0.24 | 85% | 0.38 |
| Reorder Σ p·line value (five listed) vs their revenue | 0.71 | — | 65% | 0.47 |
| Reorder Σ p·line value (all due) vs their revenue | 0.65 | — | 59% | 0.72 |
| Churn p × turnover·60/365 vs client revenue | 0.84 | 0.63–1.10 | 79% | 0.36 |
| Churn before today (p × 365-day turnover) | 5.14 | 3.82–6.68 | 126% | 0.36 |

When the most overdue product is rebought, the client's average order is 86 EUR (median) and that
product's line is 33 EUR. Top decile of reorder tasks by current EUR score: predicted 195 EUR,
realised 27 EUR for the lead product, 111 EUR for the five listed, 1,121 EUR for all due products.
Churn by decile is ordered and close (top decile 930 predicted, 837 realised).

Policy simulation, reorder and churn slots only (27% and 16% of the cap), six managers, six
vintages; "task EUR" is realised 60-day revenue of the listed products (reorder) or of the client
(churn):

| Cap | Policy | Task EUR per task | Interval | Share of tasks followed by a purchase | Share of all buyers covered |
|---:|---|---:|---|---:|---:|
| 30 | current | 315 | 220–436 | 0.660 | 0.244 |
| 50 | current | 242 | 186–314 | 0.644 | 0.387 |
| 50 | no type quota, pure EUR score | 251 | 197–323 | 0.614 | 0.368 |
| 50 | per-product EUR, listed products | 261 | 203–329 | 0.667 | 0.401 |
| 50 | per-product EUR, all due | 247 | 191–326 | 0.747 | 0.449 |
| 50 | trailing turnover only | 220 | 160–293 | 0.674 | 0.405 |
| 50 | random | 73 | 55–93 | 0.444 | 0.267 |
| 70 | current | 200 | 160–250 | 0.623 | 0.500 |

Paired comparison with the current ranking at cap 50 (same 36 manager-months resampled; the
manager-months are not independent, so these intervals are optimistic):

| Alternative | Task EUR per task | Share followed by a purchase | Client 60-day revenue per task |
|---|---|---|---|
| per-product EUR, all due products | +6 [−2, +14] | +10.3 points [+7.7, +12.6] | +632 [+438, +798] |
| per-product EUR, listed products | +20 [+13, +27] | +2.2 points [−0.6, +5.0] | −127 [−323, +63] |
| trailing turnover only | −22 [−35, −10] | +2.9 points [−0.2, +5.8] | +794 [+644, +940] |

The per-product estimators are modestly better on the measure each one targets (+8% task EUR, or
+10 points of tasks followed by a purchase). All of these measure revenue that follows a task,
which rewards picking clients who buy anyway.

A larger cap admits lower-value tasks (315 → 242 → 200 EUR per task at 30 / 50 / 70) and covers
more buyers (24% → 39% → 50%). The right cap depends on how many tasks a manager can handle a day,
which is not in the data.

Signal lifetime, from task date to the client's next order:

| Within | Reorder-task clients | Churn clients |
|---|---:|---:|
| 1 day | 32.2% | 3.0% |
| 3 days | 48.4% | 5.9% |
| 6 days (reorder and churn due date) | 63.3% | 8.4% |
| 14 days (staleness limit) | 75.0% | 14.4% |
| 30 days | 82.7% | 23.7% |
| 60 days | 87.5% | 33.6% |

A month later 93% of reorder-task clients and 77% of churn clients are still candidates. For
reorder, a 14-day limit removes little that is still valid. For churn, 86% of tasks reach day 14
with the client still silent; the "signal no longer current" rule is the right one for that type,
and the age rule only recycles the task into the next month's key.

## 7. Parameters inventory

Origin: `5127904` = first commit 2026-06-17; `2837669` = propensity model 2026-06-26; `7fc265a` =
2026-06-26; `ce14a6f` / `46aa724` = 2026-11-12. "Live" = value in the running `.env`.

| Parameter | Value | Origin | What the data says | Recommendation |
|---|---|---|---|---|
| `H_DAYS` outcome window | 60 | 2837669 | of clients who order within 60 days, 95% (reorder) and 71% (churn) do so within 30 | keep; needs outcome data to revisit |
| `ev_horizon_days` | 60 | 46aa724 | churn ratio 0.84 [0.63, 1.10] at 60; 5.1 at 365 | keep |
| `TRAINING_WINDOW_DAYS` | 365 | 2837669 | not varied | no evidence either way |
| `N_SNAPSHOTS` | 5 | 7fc265a | flat from 2–3 vintages | keep |
| `OOT_TEST_VINTAGES` | 2 | today | interval width ±0.025 reorder, ±0.05 churn | keep |
| `DEFAULT_AUC_FLOOR` | 0.62 | 7fc265a, lowered today | monthly reorder AUC 0.625–0.690 | keep; add per-type baseline comparison |
| `DEFAULT_AUC_EPSILON` | 0.01 | 7fc265a | month-to-month noise ±0.03 | 0.01 is inside noise; 0.03 would block less often for no reason |
| `min_model_training_rows` | 500 | ce14a6f | curves flat from ~400 | keep |
| `LIVE_SAMPLE_WEIGHT` | 5.0 | 7fc265a | 0 live rows | no evidence either way |
| `DEBT_PAYDOWN_FRACTION` | 0.5 | 2837669 | label not replayable | no evidence either way |
| Logistic `C` | 0.5 | 2837669 | 2 features match 21 | irrelevant at this signal level |
| Calibration | isotonic, cv 5 | 2837669 | same as Platt and none | keep |
| reorder: min purchases | 3 | 5127904 | rebuy 17% at 3 orders, 22% at 4, 28% at 5–6, 53% at 10–19 | 3 keeps 40% of rows at the lowest rate; 4 would lift the base rate, fewer candidates — owner choice |
| `reorder_min_cycle_days` | 7 | 5127904 | 410 rows (1.3%) at 7–14 days, rebuy 41% | keep |
| `reorder_max_overdue_mult` | 3.0 | 5127904 | rebuy 28% at 1–1.25×, 22% at 2.5–3× | keep; do not lead with the most overdue product |
| `_MAX_PRODUCTS_PER_TASK` | 5 | 5127904 | 42% buy one of 5, 51% one of all due | keep |
| churn: recent / baseline days | 90 / 365 | 5127904 | not varied | no evidence either way |
| churn: rate drop threshold | 0.5 | 5127904 | admits clients silent < 30 days who order 84–85% of the time | add a minimum silence (see options) |
| churn: min prior orders | 2 | 5127904 | 16% reorder at 2 prior orders, 50% at 10–19 | keep |
| `debt_max_age_days` | 365 | 5127904 | no history | no evidence either way |
| `debt_min_amount` | 10 EUR | 5127904 | no history | no evidence either way |
| `cross_sell_recent_days` / `min_orders` / `max_clients` | 120 / 3 / 40 | 5127904 | 0 labelled rows | no evidence either way |
| cross-sell `_MIN_SCORE` / `_MAX_PRODUCTS` / `_RECO_REQUEST_N` | 0.05 / 5 / 25 | 5127904 | 0 labelled rows | no evidence either way |
| `ubiquity_exclude_pct` | 0.20 | 5127904 | most common real product reaches 13% of clients; none above 20% | keep (inactive today; the synthetic product is excluded by id) |
| `_TYPE_SHARE` debt / reorder / churn / cross | 0.40 / 0.27 / 0.16 / 0.17 | 2837669 | not testable for debt and cross-sell | no evidence either way |
| `max_active_tasks_per_manager` | 50 | 5127904 | task EUR 315 / 242 / 200 at 30 / 50 / 70; coverage 24 / 39 / 50% | owner choice by manager capacity |
| `crit_debt_reserve` | 5 | 2837669 | no history | no evidence either way |
| `max_tasks_per_client_per_day` | 2 | 5127904 | 374 client-vintages are both reorder and churn candidates | no evidence either way |
| `max_pace_boost` | 1.25 | 5127904 | acts across types only through the second pass | no evidence either way |
| `target_trailing_months` | 3 | 5127904 | not analysed | no evidence either way |
| `feedback_window_days` / penalty / floor | 90 / 0.15 / 0.5 | 5127904 | 0 rejections logged | no evidence either way |
| `task_ttl_days` | 14 | 5127904 | reorder: 75% ordered by day 14; churn: 14% | keep |
| `stale_task_max_age_days` | 14 | ce14a6f | same | keep |
| `dismiss_mute_days` | 30 | 5127904 | 93% / 77% still candidates a month later | no evidence either way |
| `service_level_due_days` | 3 (debt ×1, reorder and churn ×2, cross-sell ×3) | 5127904 | reorder: 63% order on their own within 6 days; churn: 8% | reorder due date could be 3 days; churn tolerates longer |
| `w_urgency` / `w_value` / `w_confidence` | 0.5 / 0.3 / 0.2 | 5127904 | the blend's AUC against outcomes is 0.53 pooled, 0.54 reorder, 0.45 churn | used only for the debt and cross-sell index; no evidence for any weights |
| `value_saturation` | 6,000 EUR | 5127904 | active clients: median 689, p75 3,353, p90 13,039; reorder-task clients p50 6,754 | comment says "p75"; it is p75 only for reorder-task clients; no evidence either way |
| `urgency_band_*` | 0.85 / 0.6 / 0.3 | 5127904 | display tiers | no evidence either way |
| debt urgency 0.6 + 0.4·d/(d+60) | — | docs/REALDATA_CALIBRATION.md | no history | no evidence either way |
| reorder urgency 0.3 + 0.25·(ratio − 1) | — | same | more overdue = less likely to rebuy | urgency runs against the outcome; cosmetic unless used for ranking |
| churn urgency 0.6·drop + 0.4·s/(s+120) | — | 5127904 | longer silence = less likely to return (85% → 10%) | same remark |
| `model_version` | config `nba-v4-history-20250101`, live `nba-v3-propensity` | .env overrides | — | align the two |
| Cache TTLs 120 s / 600 s / 90 s; synthetic id 3,600 s | — | various | do not affect numbers | — |

## 8. Implemented today

| Commit | Change | Effect on served numbers |
|---|---|---|
| `a84e76b` | `train.py` writes `oot_honest` into `metrics.json` at each retrain: per-type AUC with client-level interval, the baseline rule's AUC, and quality on the rows actually served. Six tests in `tests/test_train_honest_metrics.py`. | none; no task changes, no restart, no artefact rewritten |

Nothing else was changed in serving. No retrain was run, the artefacts and the model version are
untouched, the service was not restarted.

## 9. Ready, awaiting a decision

1. **Reorder head at task level** (formula in section 2). Consequence: the percentage on reorder
   tasks moves from about 0.2 to about 0.5 and means "buys at least one due product"; reorder EUR
   scores change; order of reorder tasks changes. Out of time it is the better measurement (AUC
   0.86 vs 0.67 for the same question), and tasks ranked by it are followed by a purchase 10 points more
   often; EUR per admitted task is not distinguishable from the current ranking. Needs a model-version bump and a side-by-side on the 74 live reorder tasks.
2. **Per-product reorder EUR** (Σ P × line value). Needs the product's average line value in the
   generator query. Rank correlation with realised revenue 0.72 vs 0.38.
3. **Replace the model by two transparent formulas** (reorder above; churn on silence days). Same
   quality, no weekly retrain to fail, every number explainable. Loses nothing measurable.
4. **Stop showing the debt and cross-sell index as a probability**; needs a console change.
5. **Minimum silence for churn tasks** (for example 30 days): 234 of 1,757 historical candidates
   were silent under 30 days and 85% of them ordered anyway. Removes the "call a client who ordered
   last week" tasks; fewer churn tasks for large active clients.
6. **Holdout experiment** (section 5): 20% of admitted tasks withheld at random. Without it no
   parameter of the ranking can be justified by effect.

## 10. Monitoring

- Weekly, from `metrics.json → oot_honest`: head AUC minus baseline AUC per type; alert when the
  interval's upper end falls below the baseline.
- Monthly, when a vintage's 60 days close: mean predicted vs realised per type on served rows;
  alert on a gap above 5 points two months running (reorder is at 3.5–5 now).
- Base-rate drift per type by vintage (reorder went 35% → 18% in seven months).
- Share of admitted tasks whose client ordered within 3 days of generation (32% within one day
  today for reorder) — the "would have happened anyway" indicator.
- Outcome logging rate: tasks closed with a recorded outcome ÷ tasks admitted. It is 0 of 2,674.

## 11. Data that would improve this most

1. Recorded task outcomes and contact events (called / reached / result).
2. A random holdout of tasks.
3. Debt history: a daily snapshot of open debt per client, so debt tasks get a label.
4. Reco responses stored per date, so cross-sell gets a label.

## 12. Verified and not verified

Verified by running: everything in sections 3–6 and the data columns of section 7.

Inferred, not verified:

- That the live churn candidates (median silence 21 days, median 40 prior orders) differ from the
  training candidates (median silence 135 days) only because admission picks the highest EUR
  scores. The live generator reads "retained purchases", the training set reads raw orders; a
  definitional difference between the two was not checked.
- The per-product EUR estimator and the task-level head were evaluated on the local replica, not
  inside the service.
- Debt and cross-sell: nothing could be evaluated.
- At 15:00 the service health reported `db_connected: false` (the host was under heavy load from
  other work); live generation was not re-run for this review.

## 13. Follow-up 2026-11-13

Item 5 of section 9 (minimum churn silence) and the lead-product remark of section 1.2 are
implemented with measured effects, and the calibration tables are redone on the artefacts that
were live at each as-of date: `docs/task-quality-2026-11-13.md`.
