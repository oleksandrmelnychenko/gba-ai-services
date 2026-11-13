# NBA cockpit: reconciliation, task quality and degraded mode — 2026-11-13

Data: DEV `ConcordDb_V5` synced to 2026-11-13, six managers (10146, 10154, 10162, 10182, 10183,
10184), 3,037 clients, 265 active tasks. Reference sale rule: gba-server confirmed sale
(`SalesReportProjectionRepository`: `Sale.Deleted = 0`, `ChangedToInvoice IS NOT NULL`, not an owned
accounting carrier, `OrderItem.Deleted = 0`, dated by `ChangedToInvoice` in Kyiv days). The server
predicate was reproduced in SQL; the report endpoint itself was not called. Scripts and extracts:
`/root/.cache/claude-nba-finish/`.

Intervals are 95% and resample whole clients.

## 1. Reconciliation against SQL

| Figure | Result | Notes |
|---|---|---|
| Invoices month to date, 6 managers | match to the cent | 31,025.48 / 41,326.22 / 16,978.72 / 33,906.47 / 28,757.14 / 26,679.90 |
| Invoice reference (mean of Aug–Oct) | match (≤ 0.01 rounding) | 82,703.59 / 68,377.95 / 12,910.84 / 77,320.90 / 90,544.15 / 77,275.85 |
| Payments month to date | 5 match, 10182 was 134.60 low | payment months used the raw UTC value of `FromDate`; fixed to Kyiv days |
| Payments reference (mean of Aug–Oct) | 4 of 6 differed | 10162 +39.74, 10182 +473.94, 10183 +439.18, 10184 +38.26 after the fix; 89 payments, 27,212.52 EUR sat in the wrong month |
| «Оборот (12м)», «Замовлень (12м)», last order | 3,037 of 3,037 clients match | the figure is gross: return documents are not subtracted |
| Overdue debt: card = aging = column = SQL | match for all 6, 0 client differences | 10,977.62 / 8,485.83 / 891.15 / 10,814.58 / 8,485.09 / 5,564.84; department 45,219.11 |
| Unknown-date debt | 144 documents, 55,943.12 EUR, matches | all are opening balances of 2024-12-31 without source lineage |
| Task counts by type and urgency, head table, counters | match Mongo | 265 active; 43 churn, 42 cross-sell, 106 debt, 74 reorder; 10 critical, 152 high, 103 normal |
| SLA | match | 15 breached, 13 escalated (2 normal-urgency reorder tasks are not escalated by rule) |
| Expected value sums | match | per manager 27,557.36 / 24,596.38 / 33,615.72 / 20,153.29 / 40,584.65 / 24,367.76 |
| Tasks generated in November | match | 273 |

Invoices are gross and stay gross. Return documents of the same months (by return date, as in
the server net report): November 1,173.52 / 133.90 / 0 / 298.89 / 472.81 / 233.67 EUR; twelve
months 10,104.89 / 13,001.09 / 739.90 / 10,489.71 / 9,971.86 / 8,273.25 EUR (1.0–1.7% of turnover,
0.3% for 10162). `OrderItem.ReturnedQty` is filled for 45.96 EUR only, so "net of returned
quantity" in the turnover column is gross in practice.

Console labels: «Накладні, брутто з ПДВ» is correct as gross; VAT is included only on VAT sales
(the note under the card says so). «Оборот (12м), з ПДВ» should read «Оборот (12м), брутто» —
it does not subtract returns.

The console line «weighted» is Σ expected value × probability; the ranking key stored on tasks
is larger (16,351.55 vs 15,488.58 for 10146) because it carries the pace boost. Both are correct
for what they are; they are different numbers.

Owned accounting sales and payments (opening-balance carriers, all dated 2024-12-31) are now
excluded by rule as in gba-server. Measured effect on every figure: 0.

## 2. Task quality: time-split backtest

Generators replayed on as-of dates 2026-01-01 … 2026-10-01 (monthly) from the confirmed
purchases of the current client book; the replay reproduces the service SQL (1,053 of 1,054
reorder pairs, 38 of 38 churn candidates, identical features). Outcomes are confirmed purchases
in the following 30 / 60 days.

### Reorder

| Measure | 30 days | 60 days |
|---|---:|---:|
| Due pair bought again | 14.9% | 25.6% |
| Baseline: any repeat pair (3+ purchases) | 16.2% | 27.0% |
| Repeat pair not yet due | 20.5% | 33.2% |
| Task lead product bought, most overdue first (was) | 11.3% | 18.3% |
| Task lead product bought, most purchased first (now) | 19.4% | 29.7% |
| One of the five listed bought, most overdue first (was) | 28.8% [26.2, 31.5] | 42.4% |
| One of the five listed bought, most purchased first (now) | 34.9% | 47.9% |
| Client of a reorder task places any order | 82.6% | 87.8% |
| Baseline: active client without a reorder task places any order | 34.0% | 44.8% |

2,553 tasks, 361 clients. The client selection is strong; the product order was wrong: the
most overdue product is the least likely to be bought (10.9% at 2.5–3 cycles), purchase count is
the signal (8.2% at 3 purchases, 30.4% at 10+). Changed: products inside a task are ordered by
purchase count, then by the shorter time since the last purchase.

### Churn

Hit = the client stayed silent for the next 30 / 60 days.

| Population | Per as-of date | Silent 30 d | Silent 60 d |
|---|---:|---:|---:|
| Baseline: every client active in 365 days | 813 | 50.8% | 41.8% |
| Rule as it was (order rate fell by half) | 188.5 | 73.4% [70.3, 76.4] | 63.2% |
| … silence under 14 days | 14.8 | 20.3% | 12.1% |
| … silence 14–30 days | 10.1 | 24.8% | 17.4% |
| … silence below the client's own median interval | 11.1 | 27.9% | 17.5% |
| Now: also silence ≥ max(30 days, 2 × own mean interval) | 135.0 | 81.1% | 71.5% |
| Admitted (top 8 per manager by expected EUR), as it was | 48 | 49.4% | 41.9% |
| Admitted, flat 30-day minimum | 48 | 69.8% | 58.5% |
| Admitted, now | 48 | 70.7% | 60.5% |

The admitted tasks were worse than the baseline: ranking by expected EUR picked large clients
that had ordered a few days earlier (median silence 45 days, turnover 3,947 EUR). Changed:
`churn_min_silence_days = 30`, `churn_silence_cycle_mult = 2.0`; the own interval is the mean gap
between purchase days in the 365-day window. A flat 30 days gives almost the same result; the
own-interval factor adds 2 points at 60 days and removes clients inside a long natural cycle
(37% of candidates have a mean interval of 30 days or more). Short-cycle clients silent for
2 × their interval but under 30 days stay silent only 21.6% of the time (37 cases), so a shorter
floor for them is not supported. The «Мої клієнти» reactivation segment uses the same rule
(live: 185 → 128 clients).

### Cross-sell

Two as-of dates (2026-09-01, 2026-10-01), 411 reco answers recomputed as of the date, 409 with
new-to-client products; none of the recommended products had been bought before.

| Measure | 30 days | 60 days |
|---|---:|---:|
| Client bought one of the five recommended | 8.3% [5.6, 11.2] | 15.1% [10.7, 20.5] |
| Baseline: five most popular products the client has not bought | 5.1% [3.2, 7.5] | 9.3% [5.8, 13.2] |
| Client bought any new product | 93.6% | 96.6% |

Better than the baseline by 3–6 points with overlapping intervals at 30 days. Not changed.
The persisted reco cache of the retrain is keyed by client ids from before the last DEV rebuild
(0 of 11,069 files match a current client), so cross-sell rows of the next retrain are rebuilt
from live reco calls.

### Debt

History cannot be replayed over months: a paid document loses its `ClientInDebt` attachment and
attachments were soft-deleted only from 2026-11-07. Measured on the open documents of 2026-11-08
with five days of follow-up (one window, settlement arrives in sync batches):

| Population | Clients | ≥ 50% of the amount settled | All settled |
|---|---:|---:|---:|
| Debt-task population (overdue ≥ 10 EUR) | 201 | 60.7% | 51.2% |
| Baseline: open documents, none overdue | 64 | 35.9% | 21.9% |
| … 1–7 days past terms | 109 | 68.8% | |
| … 8–30 days | 70 | 54.3% | |
| … over 90 days | 9 | 11.1% | |

By document: 66.1% of documents 1–3 days past terms were settled within five days; 5.6% of those
over 90 days (72 documents, 598.26 EUR). Not changed. Two observations for a decision: most
fresh overdue debt is paid without a call, and urgency rises with age while the chance of
payment falls.

Fixed: a paid document was never recognised as settled, because settlement is a soft-deleted
attachment and `Debt.Total` stays positive. 11 of 106 open debt tasks pointed at clients with no
overdue debt left; they stayed in the queue until the due date passed and the stale rule removed
them. A debt whose only attachment is soft-deleted now counts as settled (9 of the 11 close on
the next run; the other two have documents that are no longer overdue or are under 10 EUR).

## 3. Probability calibration

Scores are those of the artefact that was live at each as-of date (seven dates, 2026-08-01 …
2026-09-13), so every row is out of time; outcome inside 60 days.

Reorder, lead product = most purchased (the rows served after this change), 1,976 rows, 313 clients,
AUC 0.777, mean predicted 0.310, realised 0.285:

| Decile | Predicted | Realised | 95% |
|---:|---:|---:|---|
| 1 | 0.050 | 0.066 | 0.039–0.109 |
| 2 | 0.110 | 0.081 | 0.050–0.127 |
| 3 | 0.152 | 0.132 | 0.092–0.186 |
| 4 | 0.195 | 0.146 | 0.104–0.202 |
| 5 | 0.239 | 0.147 | 0.104–0.203 |
| 6 | 0.288 | 0.288 | 0.229–0.355 |
| 7 | 0.343 | 0.355 | 0.292–0.424 |
| 8 | 0.408 | 0.409 | 0.343–0.479 |
| 9 | 0.528 | 0.447 | 0.379–0.516 |
| 10 | 0.782 | 0.783 | 0.720–0.835 |

Churn under the new rule, 908 rows, 184 clients, AUC 0.795, mean predicted 0.290, realised 0.279:

| Decile | Predicted | Realised | 95% |
|---:|---:|---:|---|
| 1 | 0.076 | 0.044 | 0.017–0.108 |
| 2 | 0.118 | 0.088 | 0.045–0.164 |
| 3 | 0.148 | 0.132 | 0.077–0.216 |
| 4 | 0.195 | 0.156 | 0.095–0.244 |
| 5 | 0.258 | 0.143 | 0.085–0.229 |
| 6 | 0.305 | 0.209 | 0.138–0.303 |
| 7 | 0.336 | 0.278 | 0.196–0.378 |
| 8 | 0.370 | 0.330 | 0.242–0.431 |
| 9 | 0.429 | 0.637 | 0.535–0.729 |
| 10 | 0.669 | 0.769 | 0.673–0.844 |

With the old lead product (most overdue) reorder over-predicted: 0.186 against 0.147, gap +4.0
points [+0.7, +7.3]. With the new lead the gap is +2.2 points [−1.7, +6.1]. Churn is unbiased on
average (+0.3) and too flat in the upper half: 0.43 shown, 0.64 realised.

Post-hoc calibration fitted on the August dates and tested on the September dates:

| Rows | Calibrator | Mean predicted | Realised | Calibration error | Brier | AUC |
|---|---|---:|---:|---:|---:|---:|
| Reorder, new lead | none | 0.310 | 0.288 | 0.037 | 0.1651 | 0.767 |
| | Platt | 0.285 | 0.288 | 0.029 | 0.1648 | 0.767 |
| | isotonic | 0.285 | 0.288 | 0.034 | 0.1693 | 0.757 |
| Churn, new rule | none | 0.295 | 0.291 | 0.080 | 0.1621 | 0.787 |
| | Platt | 0.277 | 0.291 | 0.070 | 0.1605 | 0.787 |
| | isotonic | 0.267 | 0.291 | 0.063 | 0.1687 | 0.751 |

Platt never worsens Brier or AUC; isotonic does in both rows. The gain is small. The layer is in
the service (`app/ml/calibration.py`, `scripts/fit_calibration.py`): a logit-linear map per task
type in `artifacts/calibration.json`, bound to the SHA-256 of the model it was fitted for and
ignored for any other artefact. No file is shipped, so probabilities are unchanged until one is
fitted. The current artefact on its two latest dates shows 0.286 against 0.293 for reorder (label
windows overlap its training), so there is no measured need to switch the layer on today.

What the numbers support on screen:

- A percentage for reorder and churn: mean error within 3 points, deciles inside their intervals
  except churn 9–10, where the shown value is lower than reality.
- Three bands instead of a percentage. Reorder: under 20% → 9.5% realised, 20–45% → 28.7%,
  over 45% → 61.9%. Churn: 10.3% / 28.8% / 73.5%.
- No percentage for debt and cross-sell: the number is a rule index, not a probability.
  The payload now says so on every task.

The percentage on a reorder task is still "the lead product is bought", not "the client orders"
(19% against 83% in 30 days).

## 4. Degraded mode

| Where | Field |
|---|---|
| Task `payload` | `score_source` (`model` / `heuristic`), `degraded`, `degraded_reason` |
| `/cockpit/inbox` | `degraded`, `degraded_reasons`, `score_sources` |
| `/generate`, `/cockpit/generate`, generation run record | `degraded`, `degraded_reasons` |
| `/health` | `scoring_degraded`, `scoring_degraded_reasons`, `score_sources`, `calibration_*` |
| `/cockpit/target`, `/head/team` | `degraded`, `degraded_reasons`, `unverified_orders` (already present) |

Reasons: `model_head_untrained`, `model_head_withheld`, `model_artifact_missing`,
`model_incompatible`, `model_stale` (artefact older than `model_max_age_days` = 21; the model
score is kept and marked), `reco_unavailable` (reco down or every call failed; an empty answer
from a healthy reco is not a failure), `generator_failed:<type>`.

Live state: debt (106 tasks) and cross-sell (42) are scored by the rule because their heads
have 21 and 0 labelled rows; reorder and churn by the model.

## 5. Hygiene

Daily worker run against a copy of the task store (`gba_nba_finish_trial`), 2,681 tasks:

| Check | Count |
|---|---:|
| Snoozed tasks to wake | 0 (none snoozed) |
| SLA to flag / to escalate | 0 / 0 (15 flagged, 13 escalated already) |
| Expired to purge | 0 |
| Stale retirement | 0 |
| Closed as settled debt (new evidence rule) | 9, replaced by 9 new debt tasks |
| Tasks whose client moved to another manager | 0 |
| Tasks of deleted or missing clients (active) | 0 |
| Duplicate client + type (active) | 0 |
| Orphan closing | 0 by design; 2,145 historical tasks carry `orphaned` from earlier DEV rebuilds |

23 of 43 open churn tasks do not pass the new silence rule. They are not refreshed and leave
through the existing stale rule after their due date (up to six days).

## 6. Not changed, for a decision

- 20% holdout of admitted tasks: still the only way to measure the effect of a call. Nothing
  recorded yet: 0 outcomes on 2,681 tasks.

## 7. Decisions applied

### Opening balances

The 144 unknown-date documents are the debts whose sale is an owned accounting carrier (the
1C balances entered on 2024-12-31). 143 are counted as overdue on the stored date; the one
whose stored date plus terms (9,000 days) lies years ahead stays unknown.

| Figure | Before | After |
|---|---:|---:|
| Department overdue | 45,219.11 | 100,920.71 |
| 10146 / 10154 / 10162 | 10,977.62 / 8,485.83 / 891.15 | 10,981.92 / 8,495.96 / 56,063.20 |
| 10182 / 10183 / 10184 | 10,814.58 / 8,485.09 / 5,564.84 | 10,845.40 / 8,500.76 / 6,033.47 |
| of which opening balances | — | 143 documents, 55,701.60 |
| Unknown date | 144 documents, 55,943.12 | 1 document, 241.52 |

Card = aging rows = «Мої клієнти» column for all six managers. Payload: `debt_opening_balance_count`,
`debt_opening_balance_eur` on `/cockpit/dashboard`, per team row and as
`total_debt_opening_balance_*` on `/cockpit/head/dashboard`, per client on `/cockpit/clients`.
An opening balance never raises a follow-up task and is not part of a task's amount or
document list.

### Debt follow-up: grace period and urgency by collectability

No task while the oldest overdue document is at most 3 days past terms. Urgency is set by the
most collectable document past the grace period: 4–30 days high, 31–90 normal, over 90 low
(«стара заборгованість», still listed).

Replay on the open documents of 2026-11-08 with five days of follow-up (201 clients with
overdue ≥ 10 EUR, 56,691.86 EUR):

| Urgency | Tasks before | Tasks after | ≥ 50% settled in 5 days (after) |
|---|---:|---:|---:|
| critical | 9 | 0 | |
| high | 192 | 146 | 60.3% |
| normal | 0 | 4 | 50.0% |
| low | 0 | 7 | 0.0% |
| no task (grace) | — | 44 | 72.7% without a task |

By document: 1–3 days 66.1% settled (257), 4–30 days 55.7% (497), 31–90 days 36.5% (52),
over 90 days 5.6% (72). Candidates of 2026-11-13: 159 → 117 (8 critical + 151 high →
110 high, 3 normal, 4 low; 42 inside the grace period).

### Training data on the serving rule

The retrain dataset is enumerated and labelled by the generators' own readers over the whole
managed book; reco answers are cached under the client NetUID; the regression gate re-scores the
previous artefact on the current fold whenever the fold or the dataset rule differs.

Dry run of 2026-11-13 (vintages 2026-05-01 … 2026-09-01, test fold August–September, 9,557
rows; 21,556 rows in all, reco asked for 1,024 clients, 0 failed calls, 1,017 cross-sell rows
against 0 in the weekly run of 2026-11-09). Same out-of-time rows:

| Model | AUC | Brier | Calibration error | Mean predicted (realised 0.203) |
|---|---:|---:|---:|---:|
| Live artefact (fitted on May–September, saw the fold) | 0.6872 | 0.1503 | 0.0331 | 0.235 |
| Old rule, fitted on May–July, logit / HGB | 0.6841 / 0.6754 | 0.1511 / 0.1511 | 0.0430 / 0.0445 | 0.246 / 0.247 |
| Serving rule, fitted on May–July, logit / HGB | 0.6902 / 0.6831 | 0.1485 / 0.1483 | 0.0354 / 0.0330 | 0.236 / 0.234 |

Cross-sell rows (408): AUC 0.544 → 0.619, Brier 0.240 → 0.151, mean predicted 0.482 → 0.227
against 0.191 realised. Reorder rows (8,885): 0.695 → 0.681; served reorder rows (563):
0.762 → 0.735; churn (256): 0.810 → 0.782 — the live artefact had seen these months, and the
candidate is HGB where the live one is logit (like for like: 0.7631 → 0.7620 served reorder,
0.8040 → 0.8061 churn). Gate: pass (0.6831 ≥ 0.62 and ≥ 0.6872 − 0.01). Heads: reorder and
churn published, cross-sell withheld (0.6187 < 0.62), debt withheld (19 rows).
