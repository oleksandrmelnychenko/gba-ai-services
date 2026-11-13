# Solvency score: statistical review, 2026-11-12

Scope: the served score `creditscore-v3` (WOE logistic scorecard, score 0–100, rating A–D, PD),
its parameters, and a replacement built and validated on DEV history (`ConcordDb_V5`,
2025-01-01 … 2026-11-12). All intervals are 95%. Where a client appears in several months the
interval resamples whole clients.

Everything in sections 1–6 was verified by running it, unless marked *inferred*.

## 1. Conclusions

1. **The v3 score is a debt flag, not a forecast.** After binning, the trained scorecard keeps
   no debt-ageing feature at all. It scores on five facts: has any open debt line, grace days of
   the agreement, had turnover in 12 months, recency, tenure. Overdue amounts and overdue days
   carry zero weight.
2. **The v3 label is a rule on the features.** "≥ EUR 100 overdue by 180+ days on 2026-11-09"
   equals "≥ EUR 100 overdue by 90+ days on 2026-08-11" for all 3,307 rows (25 of 25 positives,
   0 of 3,282 negatives). The debt table keeps only debts that are still open, so a debt that
   was open in August and paid since is invisible. 24 of the 25 positives were already in
   default on the feature date. Nothing is being predicted.
3. **v3 cannot rank clients that carry debt.** Among the 177 buyers with open debt the
   out-of-fold AUC is 0.63 [0.51, 0.75]. The total open debt alone gives 0.998 on the same rows.
4. **The reported Brier 0.60 is an artefact, not a served number.** It is measured on
   class-balanced, uncalibrated fold models (mean prediction 0.79 against a base rate of 0.14).
   The served PD has Brier 0.109 against 0.121 for a constant.
5. **v3 misrates in both directions on DEV today.** 69 buyers with ≥ EUR 100 overdue by 1–90
   days are rated A. 39 buyers are rated D for a debt residue of median EUR 0.52 (maximum
   EUR 17.78).
6. **A real forward target exists and is modelable.** Receivables history can be rebuilt from
   sales, payments and returns. On a monthly panel the event "reaches ≥ EUR 100 overdue by 30+
   days within 91 days" has 148 events on 40 clients. The 90+ day version has only 12 clients
   and stays a policy rule.
7. **A transparent points index beats v3 out of time, unambiguously.**

   | Out of time, 3 purged blocks | All active buyers | Buyers with open balance |
   |---|---|---|
   | v3 scorecard | 0.775 [0.710, 0.830] | 0.603 [0.506, 0.697] |
   | v4 points index | 0.938 [0.915, 0.957] | 0.899 [0.868, 0.926] |
   | Difference (first design, 9 components) | +0.163 [+0.105, +0.218] | +0.295 [+0.204, +0.390] |

8. **The index is implemented as `creditscore-v4` behind the model-version switch and is not
   switched on.** It changes 1,460 of 1,851 visible ratings on DEV, mostly dormant buyers moving
   from 100/A to 75/B. That presentation is the owner's decision (section 7).

## 2. What v3 really measures

Training set: 3,307 role-1 buyers, 25 positives (0.76%), features as of 2026-08-11, label as of
2026-11-09. 177 rows have open debt; their median total debt is EUR 0.69 and their median
oldest overdue line is 1,236 days — mostly old residues.

### Features that survive binning

`fit_woe` requires every bin to hold at least 3% of rows (99 rows). Only 177 rows have debt, so
every bin above zero is merged away.

| Feature | Bins | Rows / positives per bin | Coefficient |
|---|---|---|---|
| `n_open_debt_lines` | 0 / > 0 | 2,992 / 0 · 315 / 25 | +1.69 |
| `grace_days` | 0 / 1–7 / 8–10 / > 10 | 1,974 / 3 · 891 / 7 · 167 / 3 · 275 / 12 | +0.79 |
| `turnover_eur_12mo` | 0 / > 0 | 2,481 / 23 · 826 / 2 | +0.90 |
| `order_count_12mo` | 0 / > 0 (identical to turnover) | 2,481 / 23 · 826 / 2 | +0.90 |
| `recency_days` | ≤ 158 / > 158 | 663 / 1 · 2,644 / 24 | +0.99 |
| `tenure_months` | ≤ 19 / > 19 | 2,365 / 21 · 942 / 4 | +0.99 |
| 14 other features | one bin | — | 0 |

Events per candidate feature: 25 / 20 = 1.25. Events per surviving feature: 25 / 6 = 4.2. The
usual minimum is 10–20.

### Coefficient stability (300 bootstrap resamples, binning refitted each time)

| Feature | Coefficient 2.5% … 97.5% | Share of resamples with negative sign |
|---|---|---|
| `n_open_debt_lines` | +0.86 … +1.78 | 0% |
| `grace_days` | +0.35 … +1.11 | 0% |
| `turnover_eur_12mo` | −0.07 … +2.40 | 3% |
| `recency_days` | −1.29 … +2.69 | 35% |
| `tenure_months` | −0.20 … +3.18 | 5% |
| `max_overdue_days` | enters in 26% of resamples | — |

### Discrimination

| Population | Prediction | AUC | KS | Brier (constant) |
|---|---|---|---|---|
| All 3,307 rows | served, in-sample | 0.989 [0.985, 0.993] | 0.966 | 0.0059 (0.0075) |
| All 3,307 rows | out-of-fold | 0.979 [0.969, 0.987] | 0.921 | 0.0412 (0.0075) |
| 177 with open debt | served, in-sample | 0.771 [0.697, 0.839] | 0.664 [0.557, 0.764] | 0.109 (0.121) |
| 177 with open debt | out-of-fold | 0.625 [0.509, 0.748] | 0.306 [0.174, 0.494] | 0.603 (0.121) |

Out-of-fold AUC on open debt across 20 fold seeds: mean 0.637, range 0.612–0.661.

Single features on the open-debt rows: `total_debt_eur` 0.998, `grace_days` 0.628,
`recency_days` 0.621, `max_overdue_days` 0.562, `n_open_debt_lines` 0.493. The served score is
0.227 below total debt alone [−0.303, −0.156]. Total debt separates so well because the
positives are the only rows with a material debt; everything else is residue.

### Calibration

Brier decomposition, open-debt rows:

| Prediction | Reliability | Resolution | Uncertainty | Mean predicted | Observed |
|---|---|---|---|---|---|
| Served | 0.0069 | 0.0166 | 0.1213 | 0.129 | 0.141 |
| Out-of-fold | 0.4909 | 0.0101 | 0.1213 | 0.787 | 0.141 |

Served bands on the training rows:

| Band | PD rule | Rows | Positives | Observed, Wilson interval | Mean PD shown |
|---|---|---|---|---|---|
| A | < 2% | 3,110 | 0 | 0% [0, 0.12%] | 0.03% |
| B | 2–5% | 54 | 0 | 0% [0, 6.6%] | 2.1% |
| C | 5–15% | 74 | 1 | 1.4% [0.2%, 7.3%] | 9.0% |
| D | ≥ 15% | 69 | 24 | 34.8% [24.6%, 46.6%] | 24.4% |

B and C cannot be told apart. The score takes 19 distinct values; 71.6% of scored buyers get
exactly 100.

## 3. A forward target from rebuilt receivables

`Debt` / `ClientInDebt` hold 2,605 live rows and no history of paid debts. Receivables were
rebuilt per client: sales (EUR, due date = sale date + agreement grace days, opening balances
from the synthetic debt-entry line dated 2024-12-31) settled first-in-first-out by payments
(`IncomePaymentOrder.EuroAmount`) and returns.

**Check against today's debt table** (role-1 buyers, 2026-11-12): rebuilt open EUR 248k against
EUR 226k in the table; per-client correlation 0.994; of 206 clients with ≥ EUR 100 in the table
201 are also ≥ EUR 100 in the rebuild.

**What history shows.** Buyers bought EUR 7.74M and paid EUR 7.51M in 22 months. Of 53,320
invoices of at least EUR 20, 1.9% were paid more than 30 days late, 0.54% more than 60, 0.27%
more than 90. At any month-end 13–16 clients hold ≥ EUR 100 at 30+ days and 5–9 at 90+ days.
Opening-balance debt of about EUR 58k has not moved since February 2025.

**Events available** (16 month-ends, 2025-04 … 2026-07, buyers with sales in 12 months or open
debt, not already in the state):

| Event within 91 days | Client-months at risk | Events | Clients with an event |
|---|---|---|---|
| ≥ EUR 50 at 30+ days | 11,809 | 216 | 57 |
| **≥ EUR 100 at 30+ days** | **11,862** | **148** | **40** |
| ≥ EUR 200 at 30+ days | 11,915 | 98 | 28 |
| ≥ EUR 100 at 60+ days | 11,904 | 45 | 15 |
| ≥ EUR 100 at 90+ days | 11,920 | 35 | 12 |

The 30-day, EUR 100 event is the only definition with enough independent clients. With payment
terms of 7–14 days it is also the business-relevant early warning.

**What happens after delinquency** (91 days later):

| State at month-end | Client-months | Clients | Still ≥ EUR 100 at 90+ days |
|---|---|---|---|
| ≥ EUR 100 at 30–89 days | 58 | 28 | 31% |
| ≥ EUR 100 at 90+ days | 79 | 13 | 87% [78%, 93%], not adjusted for repeats |

## 4. Candidates compared out of time

Design: expanding training window, 91-day purge, three test blocks (2025-10…12, 2026-01…03,
2026-04…07). 7,963 test client-months, 113 events on 35 clients. AUC is the event-weighted mean
over blocks.

| Candidate | All active | With open balance | Per block |
|---|---|---|---|
| (a) v3 scorecard as served | 0.775 [0.710, 0.830] | 0.603 [0.506, 0.697] | 0.795 / 0.784 / 0.754 |
| Overdue EUR alone | 0.866 [0.817, 0.909] | 0.836 [0.781, 0.887] | 0.854 / 0.915 / 0.835 |
| Worst delay in 12 months alone | 0.836 [0.806, 0.865] | 0.705 [0.653, 0.753] | 0.823 / 0.837 / 0.845 |
| (b) points index, 9 components | 0.938 [0.914, 0.957] | 0.898 [0.864, 0.927] | 0.950 / 0.934 / 0.932 |
| (c) logistic, 5 features | 0.930 [0.905, 0.951] | 0.883 [0.842, 0.918] | 0.924 / 0.950 / 0.918 |
| (d) monotonic gradient boosting, 20 features | 0.958 [0.937, 0.975] | 0.937 [0.908, 0.958] | 0.955 / 0.963 / 0.957 |
| **(b) as shipped, 7 components** | **0.938 [0.915, 0.957]** | **0.899 [0.868, 0.926]** | 0.949 / 0.942 / 0.920 |

Paired differences (first design): index − v3 +0.163 [+0.105, +0.218]; boosting − index +0.021
[+0.009, +0.033]; logistic − index −0.008 [−0.024, +0.011]; index − overdue EUR alone +0.072
[+0.039, +0.110].

Calibration out of time (observed rate 1.42%):

| Model | Mean predicted | Slope | Brier (constant 0.01399) | Top decile predicted / observed |
|---|---|---|---|---|
| v3 PD | 0.71% | 0.40 | 0.01407 | 3.3% / 3.4% |
| Index, 1-D logistic | 1.49% | 0.69 | 0.01298 | 13.1% / 10.0% |
| Logistic | 1.41% | 0.92 | 0.01204 | 11.2% / 10.5% |
| Boosting | 1.49% | 0.84 | 0.01137 | 13.7% / 12.3% |

**Choice: the points index.** Boosting is 0.02 better, on 35 test clients, and cannot be read by
a credit controller. The index matches the logistic and every point is a visible bin.

**Does payment history add anything?** Yes, and it is what separates good clients:

| Subset | Full index | Without history components | Difference |
|---|---|---|---|
| All active | 0.926 | 0.903 | +0.023 [+0.008, +0.041] |
| No overdue today (< EUR 20), 22 events on 16 clients | 0.851 [0.784, 0.905] | 0.722 [0.573, 0.848] | +0.128 [+0.051, +0.210] |

**Dropped components.** Tenure and order count: the data runs against the expected direction
(more orders, more events — more invoices to be late on), so the monotone fit sets them to zero.

## 5. The v4 index

Card `index-20261112T132537+0000-d8dfdb673ae1`, fitted on all 16 months. Points are log-odds
against the base rate of 1.25%; a positive number is more risk. Each bin's rate is shrunk toward
the base rate with 30 pseudo-rows and forced monotone.

| Component | Bins | Points | Client-months | Events |
|---|---|---|---|---|
| Overdue now, EUR | 0 · ≤ 20 · ≤ 100 · ≤ 500 · more | −1.40 · −1.40 · +0.51 · +2.23 · +3.27 | 7,390 · 3,100 · 719 · 497 · 156 | 27 · 5 · 15 · 55 · 46 |
| Oldest overdue line (≥ EUR 5), days | 0 · ≤ 7 · ≤ 14 · ≤ 29 · more | −1.40 · +0.58 · +1.80 · +1.80 · +1.80 | 9,726 · 800 · 391 · 474 · 471 | 30 · 18 · 38 · 51 · 11 |
| Worst delay in 12 months, days | ≤ 7 · ≤ 14 · ≤ 30 · ≤ 60 · more | −3.12 · −0.52 · +0.78 · +1.24 · +1.24 | 6,032 · 1,092 · 1,617 · 1,207 · 978 | 3 · 8 · 44 · 61 · 32 |
| Share of invoices > 7 days late, 12 months | 0 · ≤ 20% · ≤ 50% · more | −3.12 · +0.01 · +0.85 · +1.60 | 6,032 · 1,988 · 1,759 · 1,147 | 3 · 25 · 51 · 69 |
| Delay trend, last 3 months − previous 3, days | ≤ −3 · ≤ 3 · ≤ 10 · more | −0.21 · −0.21 · +1.00 · +1.00 | 1,284 · 8,081 · 931 · 630 | 37 · 58 · 36 · 17 |
| Debt ÷ monthly turnover (debt ≥ EUR 20) | 0 · ≤ 0.5 · ≤ 1 · ≤ 2 · more | −2.51 · +1.06 · +1.48 · +1.79 · +1.79 | 9,100 · 1,406 · 640 · 394 · 322 | 9 · 50 · 35 · 30 · 24 |
| Debt ÷ credit limit (debt ≥ EUR 20) | 0 · ≤ 50% · ≤ 100% · more | −0.37 · +1.46 · +2.47 · +3.07 | 11,196 · 619 · 19 · 28 | 97 · 33 · 6 · 12 |

Point stability (200 client resamples of the first design): bins with |points| ≥ 1 change sign
in at most 1% of resamples. Bins near zero change sign more often: overdue EUR 20–100 in 6%,
worst delay 8–14 days in 12%, utilisation up to 50% in 4%, debt ÷ turnover up to 0.5 in 35%.

**Score.** `score = round(100 × (z_max − z) / (z_max − z_min))`, z = sum of points,
z_min = −12.13, z_max = +13.76. A buyer with no matured invoice in 12 months gets 0 points for
the three history components, so "no evidence" is neutral, not perfect.

**Bands** — cut-offs 80 / 60 / 30, PD = observed out-of-time event share of the band:

| Band | Score | Out of time: client-months / clients / events | PD shown | Interval | In-sample rate |
|---|---|---|---|---|---|
| A | 80–100 | 4,351 / 747 / 1 | 0.02% | [0, 0.07%] | 0.03% |
| B | 60–79 | 2,052 / 485 / 8 | 0.39% | [0.10%, 0.75%] | 0.28% |
| C | 30–59 | 1,093 / 308 / 37 | 3.4% | [1.8%, 5.1%] | 2.1% |
| D | 0–29 | 467 / 174 / 67 | 14.3% | [9.0%, 19.2%] | 14.9% |

On the first design, cut-offs 80 / 60 / 40 were also monotone (D 10.5% [6.7%, 14.9%], C 1.5%);
80 / 60 / 30 separates C from D more sharply. The PD is a band rate, not a per-client probability.

**Policy rules** (applied after the band):

| Rule | Effect | Evidence |
|---|---|---|
| ≥ EUR 100 overdue by 90+ days | Rating D, score ≤ 29, PD 87% | 87% still there after 91 days (79 client-months, 13 clients) |
| ≥ EUR 100 overdue by 30+ days | Rating no better than C, score ≤ 59, PD 31% | 31% reach 90+ days (58 client-months, 28 clients) |
| Role «Безнадійна заборгованість» | 0 / D / PD 1.0 | unchanged from v3 |

The v3 floor (PD ≥ 16% at 180+ days, ≥ EUR 100) understates: the measured persistence at 90+
days is 87%. Waiting for 180 days adds nothing — a debt that reaches 90 days rarely clears.

**Gates in `scripts/build_payment_index.py`:** at least 30 event clients (40), out-of-time AUC
≥ 0.85 (0.938) with lower bound ≥ 0.80 (0.915), band rates monotone out of time, and the
training-side behaviour features must equal the serving code on 240 sampled client-months.

## 6. Side by side on DEV, 2026-11-12

1,851 scored buyers. v3 = this morning's served scores; v4 = the same clients through the v4
serving path against the live database (cache off, service untouched).

| v3 ↓ / v4 → | A | B | C | D | Total |
|---|---|---|---|---|---|
| A | 331 | 1,103 | 101 | 65 | 1,600 |
| B | 18 | 38 | 29 | 18 | 103 |
| C | 5 | 67 | 5 | 0 | 77 |
| D | 0 | 39 | 6 | 26 | 71 |
| Total | 354 | 1,247 | 141 | 109 | 1,851 |

Offline replay on this morning's snapshot: 1,460 ratings change (135 better, 1,325 worse).

| Measure | v3 | v4 |
|---|---|---|
| Buyers at exactly 100 | 1,326 (71.6%) | 317 (17.1%) |
| Distinct score values | 19 | 71 |
| Buyers with ≥ EUR 100 overdue by 1–90 days rated A | 69 | 0 |
| Buyers rated D with total debt under EUR 20 | 39 | 0 |
| Policy: 90+ days ≥ EUR 100 | 26 (D) | 26 (D) |

Who moves:

- **1,121 from A to B.** 905 of them have no matured invoice in 12 months: dormant buyers that
  v3 shows as 100/A now show 75/B. The rest are occasional late payers.
- **157 from A to C or D.** Median overdue EUR 71, median debt EUR 203.
- **39 from D to B.** Residue debts, median EUR 0.52.

Serving cost: 1,851 buyers uncached in 12 s; one client 0.5 s.

Stability on the panel: month-over-month PSI of the band distribution at most 0.018, of any
component at most 0.065. Over 15 months the band PSI is 0.116 — the D share rose from 4.3% to
9.6%. One month later 84% of clients are in the same band and 1.9% have moved two or more.

## 7. Decisions for the owner

1. **Switch DEV to v4.** Set `MODEL_VERSION=creditscore-v4` in `.env`, then
   `systemctl restart gba-solvency`. Cached v3 answers are rejected automatically. The
   gba-server contract test pins the string `creditscore-v3` in fixtures only.
2. **How to show buyers with no payment history in 12 months** (1,063 of 1,851):
   - *75/B, "no recent history"* — what v4 does now. Honest, but half the list turns from A to B.
   - *No score ("insufficient data")* — widen the existing rule. The list shrinks to about 800
     scored buyers.
   - *Keep A as a class without a number* — needs a console change.
3. **What the PD field means.** In v4 it is the share of the band that became 30+ days overdue
   within 91 days. If the console labels it "probability of default", the label should change.
4. **Retire the v3 weekly retrain** once v4 is on. Its gate (AUC ≥ 0.90 on all rows) passes on
   the has-debt split alone and says nothing about ranking.

## 8. Parameters

Sensitivity is stated where it was measured. "No evidence" means the data cannot support or
refute the value.

### Served v3

| Parameter | Value | Where | Finding | Recommendation |
|---|---|---|---|---|
| Label: days / EUR | 180 / 100 | `dataset.py`, `lineage.py` | Equals a 90-day rule on the features | Replace with the 30-day forward event |
| Feature → label gap | 90 days | `retrain.py` | Debt table has no history; gap is cosmetic | Not usable |
| `min_bin_frac` / minimum bin | 3% / 20 rows | `train_current_state.py` | Removes every debt feature | Moot under v4 |
| `max_bins` | 5 | same | No effect after merging | Moot |
| `WOE_CLIP` | 4.0 | same | Not reached except −3.86 | No evidence |
| Logistic `C`, class weight | 0.5, balanced | same | Balanced weights give the 0.60 Brier | Moot |
| Folds / seed | 5 / 42 | same | Open-debt AUC 0.61–0.66 across seeds | Report the range |
| Score mapping PDO / offset / range | 20 / 600 / 400–800 | same | 19 distinct scores, 72% at 100 | Replace |
| Band PD cut-offs | 2% / 5% / 15% | same | B and C indistinguishable | Replace with 80 / 60 / 30 on score |
| PD floor at 180+ days | 16% | `lineage.py` | Measured persistence 87% | 87% at 90+ days |
| Gate: rows / positives / AUC | 1,000 / 20 / 0.90 | `lineage.py` | AUC gate passes on the debt flag | Gate on open-debt AUC only |
| Gate: open-debt AUC floor / drop | 0.70 / 0.02 | `retrain.py` (today) | Out-of-fold is 0.63 [0.51, 0.75]; 0.02 is inside noise | Not meaningful at 25 events |
| Forward gate: clients / AUC | 30 / 0.75 | `lineage.py` | 3 clients available at 180 days | Use the 30-day event (40 clients) |
| Insufficient-data rule | no sales in 730 days, no debt, no terms | `service.py` | 1,911 buyers | Owner decision 2 |
| `_NO_SALES_RECENCY_SENTINEL` | 3,000 days | `dataset.py` | Lands in the top recency bin | Unused by v4 |
| `risk_90d` materiality | EUR 100 | `service.py` | Same materiality as the event | Keep |
| `window_months` | 12 | config | Fixed by the contract | Keep |
| `cache_ttl` | 3,600 s | config | Scores change daily at most | Keep |
| `max_source_lag_days` | 31 | config | Readiness only | Keep |
| `synthetic_drift_turnover_ratio` | 10 | config | Readiness only | No evidence |
| PSI warn / alert | 0.10 / 0.25 | `monitor.py` | Monthly maximum observed 0.065 | Keep |
| Five-factor weights, caps, bands 80/65/45 | `score.py` | — | Not on the serving path | Delete or mark as legacy |

### v4 index

| Parameter | Value | Sensitivity measured | Recommendation |
|---|---|---|---|
| Event days / EUR | 30 / 100 | 50 → 57 event clients, 200 → 28; 60 days → 15 | Keep |
| Horizon | 91 days | Not varied | Keep; matches `risk_90d` |
| Shrinkage pseudo-rows | 30 | 5 / 30 / 100: AUC 0.926 / 0.926 / 0.926 | Keep |
| Band cut-offs | 80 / 60 / 30 | Six alternatives on the first design; all monotone | Keep |
| Invoice floor for history | EUR 20 | Not varied | No evidence |
| "Late" threshold | 7 days | Not varied | No evidence |
| Material overdue line | EUR 5 | Not varied | No evidence |
| Material debt for ratios | EUR 20 | Without it 39 residue clients fall to C; AUC 0.926 → 0.938 | Keep |
| Settlement tolerance | EUR 0.5 or 1% | Not varied | No evidence |
| Bin edges | section 5 | Chosen before fitting, not tuned | Keep |
| Gates | 30 clients, AUC 0.85, lower 0.80 | — | Keep |
| First panel month | 2025-04 | Three months of history needed | Keep |

## 9. Monitoring

- **Monthly, from the builder:** out-of-time AUC with interval; band event rates; alert when a
  band rate leaves its interval two months running or the bands stop being monotone.
- **PSI:** warn at 0.10, alert at 0.25 for the band distribution and each component, month over
  month. Track the 12-month PSI as well (0.116 now, driven by a growing D share).
- **Score history.** The service has a read-only login and does not persist scores. Proposed
  design: a nightly job in gba-server writes `(client_id, as_of_date, score, rating, pd,
  model_version, run_id)` for every scored buyer into its own table, one row per client per
  day, 400-day retention. This also gives the score sparkline real data.
- **Rebuild check:** rebuilt open receivables against the debt table (EUR 248k vs 226k today);
  alert when the gap exceeds 15%.

## 10. Data that would improve the score most

1. **Daily snapshots of open debt** (or keeping paid `Debt` rows). This removes the need to
   rebuild receivables and makes the 90-day default observable.
2. **Payment-to-invoice allocation** as booked in 1C. First-in-first-out is an approximation;
   `IncomePaymentOrderSale` has 20,987 rows for 36,858 payments.
3. **History of credit terms.** `ClientAgreement.Created` is re-stamped by every sync, so limits
   and grace days are current values applied to past months (*inferred* effect: small, terms
   change rarely).
4. **More months.** 40 event clients support a 7-component index and nothing larger.

## 11. Limits of this analysis

- The panel ages debt from the rebuilt ledger; serving ages it from the debt table. They agree
  at 0.994 per client today but were not compared for past dates (no history to compare with).
- Opening balances are dated 2024-12-31 and excluded from the event; their true age is unknown.
- Credit limits in the panel are today's values, converted at one UAH rate.
- All 16 months come from one period of rising D share; behaviour under a real downturn is
  untested.
- DEV data only. PROD and QA were not read.

## 12. Reproduce

```
python scripts/build_payment_index.py --as-of 2026-11-12 --evaluate-only
python scripts/build_payment_index.py --as-of 2026-11-12 --cache-dir <dir>
pytest -q tests/test_payment_index.py
```

Exploratory scripts and extracts: `/root/.cache/claude-ai-stats/sn/solv/` and
`/root/.cache/claude-ai-stats/sn/data/`.
