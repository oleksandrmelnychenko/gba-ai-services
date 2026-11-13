# Sales forecast: statistical review, 2026-11-12

Scope: the monthly EUR forecast served by `gba-forecast` for a client, a product and a
client+product pair. Data: DEV `ConcordDb_V5`, valid order lines 2025-01-01..2026-10-31
(22 closed months, 235,858 lines, 1,028 clients, 15,688 products, 7.87M EUR). All numbers
below come from rolling-origin backtests; nothing was fitted on the months it is scored on.

## 1. Conclusions

1. **The first month on the chart is two months past the last closed month.** The open month
   is excluded from training and the first point is the month after it. The earlier tuning
   scored one month ahead, which is not what the chart shows. Everything below is scored on
   the steps the service emits (2 to 7 months past the last closed month).
2. **Client forecasts are usable for regular clients and weak for sporadic ones.** Regular
   clients (76% of client revenue): WAPE 41% at the first emitted month, 50% at the sixth.
   Sporadic clients: 93-136%. No smoothing constant is significantly better than the current
   one for clients, so client constants are unchanged.
3. **Product and pair forecasts were too reactive; fixed.** A lower smoothing constant is
   significantly better on every product class and on regular pairs (table in section 3).
4. **A single line overstates what is known. Every point now has a range.** For a large
   regular client the range is roughly 0.4x to 1.7x of the forecast; for a product it is 0 to
   2.2-3.1x. Out of sample 80-94% of months fell inside the range (section 4).
5. **The total company forecast is biased low because the business grows.** A flat line
   under-forecasts the total by 5% one month ahead and by 16% seven months ahead; MAPE is 10%
   at one month, 13% at two, 16-18% at four to seven. The service does not publish a total;
   if one is added it needs a growth term (section 5).
6. **Seasonality cannot be established yet.** With 22 months there is one earlier observation
   per calendar month. A seasonal adjustment from the company total helped in 8 of 9 origins,
   by 0.7-1.6 points of MAPE; that is too thin to ship. Revisit in February 2027 with 24
   closed months.
7. **Client+product forecasts over-forecast by 26-34% in total** because pairs stop buying.
   The size of that bias is not stable (29% in one half of the test months, 60% in the
   other), so no correction factor was applied. Treat pair forecasts as an upper guide.

## 2. Method

- Origins: every month from 2025-07 to 2026-10 (16 origins for one step ahead, 15 for two,
  down to 10 for seven). Training uses only months before the origin; the demand class is
  assigned from the training window; a series is scored when it has at least three non-zero
  training months, as in the service.
- Metrics: WAPE (sum of absolute errors / sum of actuals) and bias (sum of errors / sum of
  actuals), in EUR.
- Uncertainty: 95% intervals from a bootstrap over series (clients, products, pairs), 300
  resamples; for company totals, a bootstrap over origins.
- The interval table is produced by `scripts/build_interval_table.py` with the service's own
  classification and smoothing code. Its record counts match an independent vectorised
  implementation exactly (client 39,460, product 516,287, pair 468,636 scored points).

## 3. Smoothing constant by scope and class

Steps 2..7 pooled. "Before" is the single map 0.3 / 0.3 / 0.4 / 0.4.

| scope | class | share of EUR | alpha before -> after | WAPE before -> after | gain, 95% interval | bias before -> after |
|---|---|---|---|---|---|---|
| client | smooth | 76% | 0.30 kept | 45.8 | best candidate 0.25: -0.2..+0.2 | -6.5% |
| client | erratic | 14% | 0.30 kept | 74.2 | best 0.20: -0.7..+1.6 | -10.5% |
| client | intermittent | 6% | 0.40 kept | 100.9 | best 0.60: -0.6..+4.1 | -29.2% |
| client | lumpy | 4% | 0.40 kept | 128.6 | best 0.60: -4.2..+10.6 | -0.9% |
| product | smooth | 47% | 0.30 -> 0.15 | 69.9 -> 68.0 | 1.4..2.5 | -3.4% -> -8.5% |
| product | erratic | 15% | 0.30 -> 0.15 | 91.7 -> 86.9 | 3.1..6.2 | +7.6% -> +0.8% |
| product | intermittent | 29% | 0.40 -> 0.20 | 123.1 -> 121.4 | 1.0..2.4 | -4.6% -> -2.4% |
| product | lumpy | 9% | 0.40 -> 0.20 | 119.1 -> 113.9 | 3.8..6.6 | -7.0% -> -7.1% |
| pair | smooth | 19% | 0.30 -> 0.10 | 112.9 -> 98.5 | 9.4..20.4 | +39.9% -> +0.9% |
| pair | erratic | 3% | 0.30 -> 0.10 | 119.3 -> 102.2 | 4.6..34.0 | +40.5% -> +5.7% |
| pair | intermittent | 68% | 0.40 kept | 164.8 | best 0.50: -0.3..+0.5 | +36.9% |
| pair | lumpy | 10% | 0.40 kept | 169.8 | best 0.25: -0.9..+4.3 | +36.6% |

Split check (constant chosen on targets up to 2026-03, scored on later targets): product
smooth 65.5 -> 64.0, erratic 91.3 -> 86.3, intermittent 121.2 -> 119.3, lumpy 115.5 -> 109.9;
pair smooth 107.8 -> 91.4, erratic 118.0 -> 95.4. The choice holds out of sample.

All classes pooled, by step:

| scope | step | origins | WAPE before | WAPE after | difference, 95% interval |
|---|---|---|---|---|---|
| product | 1 (not emitted) | 16 | 86.3 | 86.7 | -0.8..0.0 |
| product | 2 | 15 | 91.5 | 89.8 | 1.3..2.2 |
| product | 4 | 13 | 94.3 | 91.2 | 2.6..3.5 |
| product | 7 | 10 | 91.5 | 89.4 | 1.5..2.7 |
| pair | 2 | 15 | 147.9 | 146.2 | 0.9..2.4 |
| pair | 4 | 13 | 154.5 | 151.0 | 2.6..4.5 |
| pair | 7 | 10 | 160.1 | 155.3 | 3.3..6.4 |
| client | 2 | 15 | 51.4 | unchanged | |
| client | 4 | 13 | 56.2 | unchanged | |
| client | 7 | 10 | 59.8 | unchanged | |

This also explains the regression seen earlier today (product erratic worse two months ahead
than under the old window mean): at step 2 the window mean scored 83.7 against 88.3 for alpha
0.3; alpha 0.15 scores 85.4-86.9 and is not sensitive to the length of history, which the
window mean is.

Rejected candidates on the emitted steps: trailing means of 1-12 months, the whole-window
mean, TSB (0.2/0.1 and 0.3/0.2), damped trend (0.4/0.2/0.85 and 0.3/0.1/0.9). None beats the
chosen constant on any scope and class by more than its interval. A scale factor chosen to
minimise WAPE was also rejected: it shrinks sporadic forecasts toward zero (the median),
which lowers WAPE and makes the EUR totals badly biased.

## 4. Prediction range

Each point carries `SaleAmountLow` and `SaleAmountHigh`: the forecast times the 10th and 90th
percentile of actual / forecast, taken from the backtest for the same scope, demand class,
forecast-size tercile and step. `meta.interval` reports the coverage and the table version.

Multipliers at step 2 (step 7 in brackets):

| scope | class | small forecasts | middle | large |
|---|---|---|---|---|
| client | smooth (edges 286 / 927 EUR) | 0-2.59 (0-2.74) | 0.19-2.08 (0.01-2.16) | 0.41-1.68 (0.20-1.87) |
| client | erratic (128 / 384) | 0-3.26 | 0-2.63 | 0.06-2.19 |
| client | intermittent (12 / 54) | 0-6.33 | 0-4.49 | 0-3.21 |
| client | lumpy (20 / 77) | 0-12.2 | 0-4.37 | 0-2.52 |
| product | smooth (27 / 79) | 0-3.16 | 0-2.51 | 0-2.23 |
| product | erratic (24 / 65) | 0-3.03 | 0-2.66 | 0-2.38 |
| product | intermittent (6 / 17) | 0-4.91 | 0-3.83 | 0-3.04 |
| product | lumpy (7 / 21) | 0-4.71 | 0-3.29 | 0-2.78 |
| pair | smooth (8 / 28) | 0-3.69 | 0-3.09 | 0-2.86 |
| pair | intermittent (2 / 8) | 0-1.00 | 0-2.67 | 0-2.27 |

The lower bound is zero wherever more than a tenth of months have no sale at all, which is
every product and pair class and the small clients. For those series the range is one-sided
and its nominal coverage is 90%, not 80%.

Out-of-sample check (percentiles from targets before 2026-05, hit rate on 2026-05..2026-10):

| scope | class | points | inside | above |
|---|---|---|---|---|
| client | smooth | 7,548 | 80.2% | 13.8% |
| client | erratic | 3,114 | 82.8% | 16.3% |
| client | intermittent | 5,590 | 89.7% | 10.3% |
| client | lumpy | 4,901 | 89.6% | 10.4% |
| product | smooth | 36,042 | 87.8% | 12.2% |
| product | erratic | 18,882 | 88.6% | 11.4% |
| product | intermittent | 190,553 | 87.7% | 12.3% |
| product | lumpy | 46,325 | 87.0% | 13.0% |
| pair | smooth | 9,789 | 89.6% | 10.4% |
| pair | erratic | 1,554 | 93.6% | 6.4% |
| pair | intermittent | 280,531 | 91.0% | 9.0% |
| pair | lumpy | 27,637 | 92.1% | 7.9% |

The upper bound is exceeded 10-16% of the time against a nominal 10%: recent months ran
above the earlier ones because sales grew. The range is per month; adding the bounds of
several months does not give the range of their sum (the sum is relatively narrower).

Limits: steps beyond 7 reuse step 7, because 22 months leave too few origins further out;
the table must be rebuilt when the smoothing constants change or every few months as history
grows (`python -m scripts.build_interval_table --psv ... --last-month ... --table-version ...`).

## 5. Aggregates and hierarchy

Company total per month, direct forecast of the total series, MAPE across origins:

| step | origins | EWMA 0.3 | EWMA 0.5 | mean of 3 | mean of 12 | damped trend |
|---|---|---|---|---|---|---|
| 1 | 16 | 10.2 | 8.3 | 9.3 | 12.9 | 9.7 |
| 2 | 15 | 12.9 | 11.7 | 12.5 | 14.4 | 13.1 |
| 4 | 13 | 16.0 | 15.7 | 15.9 | 16.5 | 16.9 |
| 7 | 10 | 18.3 | 18.4 | 18.7 | 19.1 | 16.5 |

Steps 2..7 pooled, EWMA 0.3: MAPE 15.7% (origin bootstrap 12.8-18.6), bias -10.8%. Every
flat method is biased by -8% to -14%; the company grew by about a fifth year on year.

Summing series forecasts gives the same accuracy as forecasting the total directly, provided
the series the service refuses (fewer than three non-zero months) are added back:

| step | direct | sum of clients, refused excluded | sum of clients + refused | sum of products + refused |
|---|---|---|---|---|
| 1 | 10.2 | 12.5 | 10.1 | 11.7 |
| 2 | 12.9 | 15.2 | 12.8 | 13.8 |
| 4 | 16.0 | 17.3 | 16.3 | 16.4 |
| 7 | 18.3 | 20.4 | 18.8 | 18.8 |

Refused series carry 6-10% of client revenue and 16-22% of product revenue, so any screen
that adds up per-entity forecasts understates the total by that much.

By manager (11 scored) the best constant is higher (0.5-0.7: WAPE 20-21% at step 2 against
25% for 0.3); by product group (76 scored) 0.3-0.5 is best (24.5%). Neither aggregate is
served today.

## 6. What was not established

- Demand-class cut-offs (ADI 1.32, CV2 0.49) were not re-derived. The classes do separate
  accuracy (client WAPE 46 / 74 / 101 / 129 for smooth / erratic / intermittent / lumpy), and
  since every class uses the same method the cut-offs only move a series between two nearby
  constants and two interval rows. Classification on units instead of EUR was not tested.
- Seasonality and trend at series level: not enough history.
- No forecast was checked through gba-server in this pass; the range fields are optional
  members of its contract on branch `claude/forecast-intervals`.

## 7. Parameters

| parameter | value | origin | sensitivity | recommendation |
|---|---|---|---|---|
| EWMA alpha, client smooth / erratic | 0.3 / 0.3 | holdout 2026-11-12, kept | WAPE flat within 0.5 point for 0.2-0.4 | keep |
| EWMA alpha, client intermittent / lumpy | 0.4 / 0.4 | holdout 2026-11-12, kept | 0.6 better by 1.7-2.1 points, interval includes 0 | keep; re-test with more history |
| EWMA alpha, product smooth / erratic | 0.15 / 0.15 | this review | 0.30 worse by 1.9 / 4.8 points | changed |
| EWMA alpha, product intermittent / lumpy | 0.2 / 0.2 | this review | 0.40 worse by 1.7 / 5.2 points | changed |
| EWMA alpha, pair smooth / erratic | 0.1 / 0.1 | this review | 0.30 worse by 14 / 17 points | changed |
| EWMA alpha, pair intermittent / lumpy | 0.4 / 0.4 | kept | flat | keep |
| `min_history_months` | 3 non-zero months | commit 2026-06 | refuses 6-10% of client and 16-22% of product revenue | no evidence either way |
| `history_months` | 24 | initial | bound by the 2025-01 source start today | keep |
| `forecast_horizon_months` / max | 6 / 24 | initial | range table covers 6; later months reuse the last row | keep; do not present beyond 6-7 months as equally reliable |
| ADI / CV2 cut-offs | 1.32 / 0.49 | literature | not tested | no evidence either way |
| `croston_alpha`, Holt 0.4 / 0.2 / 0.85 | unused in auto mode | 2026-06 backtest | forced modes only | keep as A-B switches |
| interval quantiles | 0.10 / 0.90 | this review | | keep |
| interval size bands | terciles per scope and class | this review | large-client range is half as wide as small-client | keep |
| gate lead | 2 months | this review | lead 1 scored a month the chart never shows | changed |
| `cache_ttl` | 3600 s | initial | does not change numbers within a day | keep |
| `source_max_age_hours` | 168 | 2026-09 | readiness only | keep |

## 8. Changes made

| commit | change | version |
|---|---|---|
| `439cf7f` | smoothing constant per scope and class; accuracy gate scores the served step | model `forecast-v4-ewma-by-scope-and-class`, service 0.2.0 |
| `ba2ad27` | prediction range on every point, interval table and its build script | model `forecast-v5-scope-alpha-intervals`, service 0.3.0 |

Deployment note: the live `.env` pins `MODEL_VERSION`; it was moved to
`forecast-v5-scope-alpha-intervals` when the service was restarted.

## 9. Data that would improve the forecast most

1. Two more months of history (24 closed months) to test seasonality and a growth term.
2. Lost and unfulfilled demand: sales are capped by stock, so the forecast learns sales, not
   demand.
3. A flag for one-off deals (tenders, project supplies) to keep them out of the level.

## 10. Addendum, 2026-11-13: history is confirmed sales net of returns

Sections 1 to 9 were measured on the earlier history (every valid order line, dated by
`Order.Created`, UTC months). The service now reads gba-server's confirmed sales (invoiced,
non-deleted, not an accounting carrier, dated by `Sale.ChangedToInvoice`, Europe/Kyiv months)
net of buyer returns. Numbers below are from the DEV snapshot of 2026-11-13 01:15, closed
months 2025-01..2026-10.

### 10.1 Where a return is subtracted

Buyer returns in the window: 2,253 lines, 83.3k EUR (1.1% of sales).

| rule | returns that reduce nothing | lead-2 WAPE, last 6 months |
|---|---|---|
| in the return's own month, month floored at zero | client 3.0k EUR, product 19.1k, pair 41.0k | client 51.26, product 89.98 |
| on the returned line, in the line's invoice month, line floored at zero (chosen) | 0 | client 50.96, product 89.75 |

The chosen rule keeps every returned euro and is the rule gba-procure uses for units. What it
gives up is month-by-month equality with the server's net sales report, which books a return
in the month of the return document:

- 42.6k of the 83.3k EUR of returns belong to a sale invoiced in an earlier month, so they
  lower that earlier month here and the return's month in the report.
- Company total per closed month differs from the report by 0.7k EUR on the median and 2.2k
  EUR at most (0.7% of a month); over the 22 months the two totals differ by 2.1k of 7.60M.
- A closed month can still go down when a later return arrives. Half of returns come within
  11 days of the invoice, 90% within 49 days, 99% within 202 days.
- The client is the one on the sale, not the one on the return document.

### 10.1a Returns anchored to import placeholders (2026-11-13)

A return of a sale older than the imported history has no returned line: the 1C import anchors
it to a zero placeholder line (zero quantity, prices and rate, caption `1С`, on an imported
return). The line rule above lost all of them. They are now negative facts of their own: minus
the recorded return amount (`SaleReturnItem.Amount`, the server's valuation) at
`SaleReturn.FromDate`, for the client of the return's agreement and the returned product; a
series month is floored at zero and the part a month cannot absorb is dropped and reported by
`GET /diagnostics/placeholder-returns`. Ordinary returns keep the line rule.

DEV, as of 2026-11-13: 92 lines, 46 clients, 90 products, 3,600.79 EUR.

| series | absorbed, EUR | dropped, EUR | months that dropped |
|---|---|---|---|
| client | 3,057.24 | 543.55 | 6 of 54 |
| product | 1,674.18 | 1,926.61 | 51 of 92 |
| client and product | 171.60 | 3,429.20 | 86 of 92 |

The unfloored facts moved by exactly -3,600.79 EUR and no other client/product/month cell
changed (215,308 cells identical). The largest client effect is 451.47 EUR of history; client
631823 lost 299.74 EUR of history. Served client forecasts moved by at most 0.82 EUR a month
(34 of 46 clients changed, 2.43 EUR a month in total), because most of these returns are dated
2025-01..2025-03. Smoothing constants and the interval table were not re-estimated.

### 10.2 Smoothing constants

Same procedure as section 3 (steps 2..7, grid 0.05..0.7, 95% bootstrap interval over series
for the gain against the constant in service), with the constant chosen only on targets up to
2026-04 so that the six months the refresh gate scores (2026-05..2026-10) stay untouched.

| scope | class | in service | best on targets up to 2026-04 | gain, 95% interval | decision |
|---|---|---|---|---|---|
| client | smooth | 0.30 | 0.30 | | keep |
| client | erratic | 0.30 | 0.20 | -0.6..+3.2 | keep |
| client | intermittent | 0.40 | 0.50 | -1.2..+2.2 | keep |
| client | lumpy | 0.40 | 0.70 | -7.8..+23.1 | keep |
| product | smooth | 0.15 | 0.15 | | keep |
| product | erratic | 0.15 | 0.15 | | keep |
| product | intermittent | 0.20 | 0.25 | -0.2..+0.7 | keep |
| product | lumpy | 0.20 | 0.20 | | keep |
| pair | smooth | 0.10 | 0.05 | -0.5..+3.5 | keep |
| pair | erratic | 0.10 | 0.10 | | keep |
| pair | intermittent | 0.40 | 0.50 | -0.5..+0.8 | keep |
| pair | lumpy | 0.40 | 0.25 | -3.6..+7.7 | keep |

No candidate is significantly better, on this window or on all origins pooled, so all twelve
constants stay. Scored points with the service's own code: client 39,038, product 518,248,
pair 480,875; an independent vectorised implementation gives the same counts.

### 10.3 Refresh gate, lead 2, targets 2026-05..2026-10

WAPE / bias in percent; "before" is the earlier history with the same constants.

| scope | class | points before -> after | WAPE before -> after | bias before -> after | mean of 3, after |
|---|---|---|---|---|---|
| client | all | 3,788 -> 3,762 | 49.91 -> 50.96 | -7.31 -> -6.70 | 52.10 |
| client | smooth | 1,259 -> 1,236 | 39.99 -> 40.36 | -5.27 -> -5.39 | 42.02 |
| client | erratic | 549 -> 561 | 66.08 -> 65.71 | -4.02 -> -4.34 | 67.97 |
| client | intermittent | 1,012 -> 1,017 | 74.92 -> 85.25 | -36.06 -> -27.77 | 82.11 |
| client | lumpy | 968 -> 948 | 121.37 -> 122.33 | -0.16 -> +1.26 | 118.24 |
| product | all | 53,571 -> 53,786 | 89.40 -> 89.75 | -9.05 -> -8.80 | 94.11 |
| product | smooth | 5,508 -> 5,499 | 58.72 -> 59.25 | -14.66 -> -14.43 | 61.47 |
| product | erratic | 3,203 -> 3,242 | 85.31 -> 85.13 | -2.02 -> -3.73 | 93.02 |
| product | intermittent | 35,652 -> 35,902 | 119.46 -> 119.90 | -7.11 -> -6.54 | 123.71 |
| product | lumpy | 9,208 -> 9,231 | 114.42 -> 115.89 | -5.16 -> -3.28 | 123.85 |

The gate passes on both scopes. Sporadic clients (intermittent and lumpy, 12% of scored
client revenue) stay behind the mean of three months, as they were before the change; the
jump in the intermittent row comes from moving to confirmed sales (it is 85.57 with returns
booked in their own month), not from the returns rule.

### 10.4 Prediction range

`interval_table.json` was rebuilt on the new history (table version 2026-11-13). Of 216
cells, 26 moved the upper multiplier by more than 5%; the median change is -0.3%. Out of
sample (percentiles from targets before 2026-05, hit rate on 2026-05..2026-10, steps 2..7),
inside / above, before -> after:

| scope | smooth | erratic | intermittent | lumpy |
|---|---|---|---|---|
| client | 80.2 / 13.8 -> 80.5 / 13.3 | 82.8 / 16.3 -> 83.2 / 15.7 | 89.7 / 10.3 -> 89.8 / 10.2 | 89.6 / 10.4 -> 90.3 / 9.7 |
| product | 87.8 / 12.2 -> 88.1 / 11.9 | 88.6 / 11.4 -> 88.8 / 11.2 | 87.7 / 12.3 -> 88.0 / 12.0 | 87.0 / 13.0 -> 88.3 / 11.7 |
| pair | 89.6 / 10.4 -> 89.5 / 10.5 | 93.6 / 6.4 -> 93.2 / 6.8 | 91.0 / 9.0 -> 91.1 / 8.9 | 92.1 / 7.9 -> 92.0 / 8.0 |

### 10.5 Limits

- The backtest sees the returns known today. A live forecast sees its last closed month
  before all of that month's returns have arrived, so that month is up to about 0.5% higher
  than it will be once they have.
- Model `forecast-v6-line-net-sales`, cache namespace `v5-line-net-sales`. The live `.env`
  pins `MODEL_VERSION` and has to be moved with the deployment.

## 11. Addendum, 2026-11-13: sporadic series, horizon shape, range by step, late returns

Data: DEV snapshot after the sync of 2026-11-13, confirmed sales net of returns, closed months
2025-01..2026-10 and the open month (237,427 lines, 2,254 return lines; 1,025 clients, 15,750 products, 15,244
pairs with three or more non-zero months). Discipline as in section 10: every choice is made on
targets up to 2026-04 ("choice window"), the months 2026-05..2026-10 are only read afterwards
("check window"). Month 1 is the first month on the chart, two steps past the last closed month.
WAPE and bias in percent. Intervals are 95% bootstrap intervals over series.

### 11.1 Sporadic clients and products

Candidates per scope and class: EWMA 0.05..0.7, trailing means of 1..12 months, mean since the
first sale, Croston and SBA 0.1..0.3, TSB on a 9 x 9 grid, a blend of the 3-month mean with the
12-month mean, the 6-month mean, the mean since the first sale and the service level, the median
of 3 months. The median of 3 months and SBA were excluded: they lower WAPE by forecasting less
(bias -20% to -48% on the choice window), the same effect section 3 rejected.

Choice window, months 1..6 pooled, and the check window:

| scope | class | method before -> after | choice WAPE before -> after | mean of 3, choice | check, months 1..6: before / after / mean of 3 |
|---|---|---|---|---|---|
| client | intermittent | EWMA 0.4 -> TSB 0.5 / 0.5 | 116.76 -> 116.25 | 117.55 | 96.43 / 94.42 / 95.45 |
| client | lumpy | EWMA 0.4 -> TSB 0.5 / 0.5 | 145.20 -> 141.16 | 143.97 | 119.58 / 115.18 / 118.52 |
| product | intermittent | EWMA 0.2 -> TSB 0.2 / 0.3 | 124.16 -> 123.00 | 126.10 | 119.54 / 119.08 / 123.67 |
| product | lumpy | EWMA 0.2 -> TSB 0.2 / 0.3 | 118.00 -> 114.44 | 125.58 | 111.50 / 109.53 / 120.88 |
| pair | intermittent | EWMA 0.4 kept | 169.95 (best TSB 169.00) | 169.79 | 161.69 / 161.69 / 161.50 |
| pair | lumpy | EWMA 0.4 kept | 158.05 (best TSB 156.92) | 159.62 | 180.63 / 180.63 / 183.11 |

Regular classes are unchanged: no candidate with a comparable bias is better on the choice
window.

Refresh gate (month 1, targets 2026-05..2026-10), WAPE (bias):

| scope | class | points | before | after | mean of 3 | after minus mean of 3, 95% interval |
|---|---|---|---|---|---|---|
| client | all | 3,762 | 50.96 (-6.70) | 50.62 (-6.89) | 52.09 (-2.84) | -1.47 |
| client | smooth | 1,236 | 40.36 (-5.39) | 40.36 (-5.39) | 42.02 (-1.51) | -1.66 (-3.78..+0.46) |
| client | erratic | 561 | 65.70 (-4.33) | 65.70 (-4.33) | 67.96 (+3.55) | -2.26 (-6.00..+0.23) |
| client | intermittent | 1,017 | 85.25 (-27.77) | 82.81 (-28.29) | 82.11 (-28.18) | +0.70 (-5.02..+3.43) |
| client | lumpy | 948 | 122.33 (+1.26) | 118.75 (-2.55) | 118.24 (-1.85) | +0.51 (-6.68..+7.53) |
| product | all | 53,786 | 89.76 (-8.79) | 89.01 (-9.34) | 94.12 (-0.42) | -5.11 |
| product | smooth | 5,499 | 59.25 (-14.43) | 59.25 (-14.43) | 61.47 (-4.48) | -2.22 (-3.94..-0.40) |
| product | erratic | 3,242 | 85.13 (-3.73) | 85.13 (-3.73) | 93.02 (+10.99) | -7.89 (-10.86..-4.13) |
| product | intermittent | 35,902 | 119.90 (-6.54) | 118.91 (-6.32) | 123.71 (-2.21) | -4.80 |
| product | lumpy | 9,143 | 115.93 (-3.26) | 112.39 (-8.28) | 123.89 (+2.05) | -11.50 |

Before the change the service was behind the 3-month mean on intermittent clients by 3.14
points, interval +0.92..+6.67: a real deficit. After it the two sporadic client classes are 0.5
to 0.7 points behind at month 1, inside intervals of about 5 points either way, and ahead over
months 1..6 (table below). They are not shown to beat the 3-month mean at month 1 and cannot be
with this history: on the choice window TSB leads the 3-month mean at that month by 2.8 and 8.5
points, on the check window it trails by 0.7 and 0.5, and no candidate chosen without reading
the check window does better (the finer TSB grid picks 0.85 / 0.4 for intermittent clients,
which scores 89.04 on the gate). A half-and-half blend of TSB and the 3-month mean was tried
once after this was seen; it is worse than TSB on the choice window (116.43 and 141.86) and was
not adopted. Serving the 3-month mean itself for the two classes would tie the gate by
construction; it forecasts exactly zero after three silent months (those clients then bought
361k EUR in the backtest) and is behind TSB on months 2..6.

The gain for sporadic products against the previous service is significant on the choice
window (intermittent -1.16, interval -2.00..-0.31; lumpy -3.56, -5.85..-1.28) and on the gate
(-0.99, -1.49..-0.45; -3.54, -5.49..-1.91).

WAPE by month on the check window, before / after / mean of 3:

| scope | class | month 1 | month 2 | month 3 | month 4 | month 5 | month 6 |
|---|---|---|---|---|---|---|---|
| client | all | 51.0 / 50.6 / 52.1 | 53.0 / 52.7 / 54.5 | 55.6 / 55.2 / 57.8 | 57.5 / 57.2 / 59.8 | 59.0 / 58.6 / 60.5 | 59.0 / 58.7 / 60.2 |
| client | smooth | 40.4 / 40.4 / 42.0 | 43.0 / 43.0 / 44.3 | 45.1 / 45.1 / 47.0 | 46.5 / 46.5 / 48.9 | 47.8 / 47.8 / 49.9 | 48.4 / 48.4 / 49.8 |
| client | erratic | 65.7 / 65.7 / 68.0 | 68.2 / 68.2 / 72.2 | 70.6 / 70.6 / 75.6 | 74.5 / 74.5 / 77.5 | 75.9 / 75.9 / 75.7 | 71.0 / 71.0 / 73.4 |
| client | intermittent | 85.2 / 82.8 / 82.1 | 87.4 / 85.4 / 87.3 | 96.9 / 94.7 / 97.3 | 98.0 / 96.4 / 97.3 | 105.0 / 103.0 / 104.1 | 111.4 / 109.8 / 110.2 |
| client | lumpy | 122.3 / 118.8 / 118.2 | 109.8 / 106.8 / 108.0 | 110.8 / 106.9 / 111.8 | 122.7 / 117.8 / 124.2 | 123.1 / 118.0 / 123.4 | 129.5 / 123.6 / 125.9 |
| product | all | 89.8 / 89.0 / 94.1 | 90.7 / 90.2 / 96.8 | 91.2 / 90.9 / 98.4 | 91.0 / 90.8 / 98.7 | 90.0 / 89.8 / 96.9 | 89.3 / 89.1 / 94.9 |
| product | smooth | 59.2 / 59.2 / 61.5 | 62.4 / 62.4 / 66.0 | 65.2 / 65.2 / 70.1 | 66.3 / 66.3 / 72.0 | 67.1 / 67.1 / 72.9 | 67.9 / 67.9 / 73.1 |
| product | erratic | 85.1 / 85.1 / 93.0 | 87.4 / 87.4 / 100.4 | 88.4 / 88.4 / 104.3 | 88.7 / 88.7 / 105.3 | 88.0 / 88.0 / 103.4 | 86.8 / 86.8 / 99.2 |
| product | intermittent | 119.9 / 118.9 / 123.7 | 120.5 / 119.8 / 125.4 | 120.3 / 120.0 / 125.5 | 119.8 / 119.6 / 125.0 | 118.4 / 118.1 / 121.5 | 118.1 / 117.8 / 120.5 |
| product | lumpy | 115.9 / 112.4 / 123.9 | 113.3 / 110.5 / 121.9 | 111.8 / 110.0 / 121.5 | 110.1 / 108.5 / 121.4 | 108.3 / 107.3 / 119.2 | 108.2 / 107.7 / 116.0 |
| pair | all | 149.8 / 149.8 / 152.1 | 151.4 / 151.4 / 155.3 | 152.5 / 152.5 / 158.0 | 153.7 / 153.7 / 160.3 | 156.7 / 156.7 / 162.4 | 156.7 / 156.7 / 162.8 |

Cells still behind the 3-month mean on the check window: client intermittent and lumpy at
month 1 (above), client erratic at month 5 (75.9 against 75.7), pair intermittent at months 1,
5 and 6 (by 0.4 to 1.5 points). A sporadic series forecast above WAPE 100 is less accurate in
EUR than a zero line; it is still served because a zero line is 100% biased.

The demand probability also settles how a series that stopped selling is treated. Forecast and
actual EUR summed over all origins and months 1..6, by the number of silent months at the end
of the training window:

| scope | silent months | points | actual | before | after | mean of 3 |
|---|---|---|---|---|---|---|
| client | 3..5 | 3,084 | 361k | 138k | 80k | 0 |
| client | 6 or more | 1,853 | 30k | 15k | 5k | 0 |
| product | 3..5 | 76,610 | 1,349k | 1,018k | 692k | 0 |
| product | 6 or more | 34,431 | 359k | 199k | 84k | 0 |

No series keeps its old level: after six silent months a client forecast is 1.6% of the
smoothed sale size (0.5^6) and a product forecast 12% (0.7^6); before the change it was 4.7%
and 26% of the level.
Silent series do come back, so the totals are under-forecast, not over-forecast, and no rule
that forces zero was added.

### 11.2 Horizon shape

Tested per month on top of the level above: a growth term from the log-linear trend of the
company total (0.25 to 1.0 of the slope, with and without two months of lag), a seasonal factor
from the company total one year earlier (half and full strength, with and without removing the
trend), damped trend per series (four settings), reversion to the mean since the first sale
(0.8 and 0.9 per month), and a separate smoothing constant per month. WAPE, choice window /
check window:

| scope | shape | month 1 | month 2 | month 3 | month 4 | month 5 | month 6 |
|---|---|---|---|---|---|---|---|
| client | flat | 52.5 / 50.6 | 55.0 / 52.7 | 56.3 / 55.2 | 56.5 / 57.2 | 58.3 / 58.6 | 59.1 / 58.7 |
| client | growth, half slope | 52.8 / 50.6 | 55.7 / 52.8 | 57.2 / 55.2 | 57.8 / 57.2 | 60.2 / 58.6 | 60.7 / 58.7 |
| client | seasonal, half, detrended | n/a / 50.5 | n/a / 52.6 | n/a / 55.2 | n/a / 57.4 | n/a / 58.7 | n/a / 58.8 |
| client | damped trend 0.3 / 0.1 / 0.8 | 65.5 / 51.3 | 69.6 / 53.9 | 73.1 / 56.6 | 76.1 / 58.9 | 80.6 / 60.8 | 85.0 / 61.2 |
| client | reversion 0.9 | 52.6 / 51.1 | 55.2 / 53.6 | 56.3 / 56.4 | 56.7 / 58.5 | 58.4 / 60.0 | 59.2 / 60.6 |
| product | flat | 86.9 / 89.0 | 88.3 / 90.2 | 88.6 / 90.9 | 88.9 / 90.8 | 88.5 / 89.8 | 88.2 / 89.1 |
| product | growth, half slope | 87.5 / 89.3 | 89.3 / 90.5 | 90.1 / 91.3 | 90.8 / 91.2 | 91.0 / 90.4 | 90.6 / 89.9 |
| product | seasonal, half, detrended | n/a / 89.3 | n/a / 90.7 | n/a / 91.3 | n/a / 91.4 | n/a / 90.2 | n/a / 89.4 |
| product | damped trend 0.3 / 0.1 / 0.8 | 106.4 / 92.4 | 112.1 / 94.6 | 117.1 / 96.4 | 121.4 / 96.7 | 126.3 / 96.0 | 131.2 / 95.8 |
| product | reversion 0.9 | 87.0 / 89.3 | 88.5 / 90.5 | 89.0 / 91.3 | 89.2 / 91.3 | 89.0 / 90.6 | 88.7 / 90.1 |
| pair | flat | 144.0 / 149.8 | 148.3 / 151.4 | 153.6 / 152.5 | 160.5 / 153.7 | 164.4 / 156.7 | 167.5 / 156.7 |
| pair | growth, half slope | 145.3 / 150.8 | 150.7 / 152.8 | 157.5 / 154.1 | 166.2 / 155.7 | 171.7 / 159.4 | 175.8 / 160.5 |

Nothing beats the flat line on the choice window at any month in any scope, so the horizon
stays flat. Details:

- Growth lowers the bias on the check window (clients at month 6: -17.1% flat, -12.5% with half
  the slope) and leaves WAPE within 0.1 point there, but costs 0.4 to 1.9 points on the choice
  window. The slope itself moved from 3.7% a month (estimated in mid-2025) to 0.4% (early 2026)
  to 1.9% (now).
- The seasonal factor cannot be chosen out of sample: a factor for a calendar month needs the
  same month a year earlier, which exists only for origins from 2026-03, that is only inside the
  check window (hence n/a). Read there, half strength with the trend removed gives 0.1 point at
  months 1 and 2 for clients and is worse elsewhere and for products at every month.
- A separate smoothing constant per month, chosen on the choice window, is worse than the single
  constant on the check window at five of six months for clients and at all six for products.

The response says so: `meta.horizon_shape` is `"flat_run_rate"` (`"damped_trend"` only when
that method is forced). Six equal months are the monthly run-rate, not a statement that each
month will be equal. The flat line under-forecasts a growing total: bias on the check window
goes from -7% at month 1 to -17% at month 6 for clients and from -9% to -16% for products.

### 11.3 Range by step

The table has its own percentiles per step. Fitted on targets before 2026-05 and tested on
2026-05..2026-10, share of months above the upper bound (nominal 10%), all classes:

| scope | month 1 | month 2 | month 3 | month 4 | month 5 | month 6 |
|---|---|---|---|---|---|---|
| client | 10.5 | 11.0 | 11.8 | 13.5 | 13.1 | 12.7 |
| product | 10.1 | 11.0 | 11.7 | 12.4 | 12.4 | 12.2 |
| pair | 8.1 | 8.5 | 9.1 | 9.1 | 9.3 | 9.2 |

By class the worst cells were client erratic (12.7% at month 1 to 18.7% at month 5) and client
smooth (11.1% to 15.6%; inside the range 84.0% at month 1, 78.3% at month 4). The lower bound
holds (4.9% to 6.1% below it for smooth clients against a nominal 10%). The upper bound is too
low further out because sales grew.

The build script now widens the upper bound per scope and step: the distance of the upper
multiplier from 1 is multiplied by the smallest factor (grid of 0.05) that brings the share
above the bound to 10% on the later targets, never less than at the step before.

| scope | month 1 | month 2 | month 3 | month 4 | month 5 | month 6 |
|---|---|---|---|---|---|---|
| client | 1.10 | 1.10 | 1.20 | 1.35 | 1.35 | 1.35 |
| product | 1.05 | 1.15 | 1.20 | 1.30 | 1.30 | 1.30 |
| pair | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

After widening, on the same later targets: clients 8.8% to 9.9% above and 87.7% to 88.5%
inside; products 9.5% to 9.9% above; by class 6.8% to 12.1% above (client erratic stays the
highest). Those figures are from the months the factors were fitted on. An independent check
(percentiles from targets before 2026-03, factors from 2026-03..2026-06, tested on
2026-07..2026-10): share above the bound, before -> after widening, clients month 1 10.3 ->
7.6, month 4 15.2 -> 10.9, month 6 15.2 -> 10.3; products month 1 11.2 -> 8.2, month 6 15.6 ->
10.7. Table version `2026-11-13.2`; for the ТІР МАРКЕТ client the upper bound moves from 13,830
to 14,400 EUR at month 1 and from 15,197 to 17,664 EUR at month 6.

### 11.4 Returns that arrive after the month closed

Replayed for every origin: the training months as they looked on the 1st and on the 13th of the
following month (returns dated later removed), with and without a completion factor estimated
from earlier origins only.

- The last closed month is overstated by 0.1% to 0.9% on the 1st (median 0.6%) and by 0.0% to
  0.5% on the 13th (median 0.3%). The factor comes out at 0.994 and 0.997; the month before
  needs 0.998, the one before that 0.999.
- For a single series it is larger: where the last closed month later fell, by 2.7% on the
  median and 22.7% at the 90th percentile for clients, 34% and 121% for products; that is 285
  client and 225 product series-months out of all origins on the 13th.
- Effect on the month-1 forecast, WAPE on the check window, 13th of the month: clients 50.627
  as seen, 50.628 with the factor, 50.620 with all later returns known; products 89.019, 89.010,
  89.007. On the choice window the factor gains 0.013 points.

Knowing every later return is worth 0.007 to 0.012 points of WAPE; the factor recovers part of
that or nothing. It was not implemented.

### 11.5 Thin, new and silent series

- No sale in the window (client ТНТ ПАРТС: one opening-balance sale dated 2024-12-31, before the
  source start): the service answers `status = insufficient_history`, `horizon_months = 6`,
  `month_count = 0`, no points. The payload is right. The console title «на 0 місяців» comes
  from the console counting returned points instead of reading `meta.horizon_months`.
- New client or product: no forecast with fewer than three non-zero months. With three, the
  level starts from the zero months before the first sale, so it is below the recent months
  (400, 500, 700 EUR gives 374). Starting at the first sale instead was tested and is worse on
  both windows in every scope: series first sold within six months, months 1..6, check window,
  WAPE and bias as served against started at the first sale: clients 82.0 (-17%) against 84.0
  (-3%), products 135.7 (+3%) against 167.3 (+66%), pairs 178.9 (+43%) against 221.7 (+118%).
  New buyers fall back more often than they keep their first level. Kept as is.
- Silent series: section 11.1. A client with 16 active months then 6 silent ones gets 16 EUR per
  1,000 EUR of level; with 12 silent months under 1 EUR.
- Negative months cannot occur: lines are floored at zero in SQL, the history summary rejects a
  negative month, and amounts and bounds are clamped at zero cents.
- Pair scope: unchanged method; over-forecast by 22% to 28% on the check window (section 1,
  point 7), concentrated in pairs that bought in the last training month (+54% over all
  origins).

### 11.6 Versions and gate

Model `forecast-v7-tsb-sporadic`, cache namespace `v6-tsb-sporadic`, interval table
`2026-11-13.2`, service 0.4.0. The live `.env` must carry
`MODEL_VERSION=forecast-v7-tsb-sporadic`.

The refresh gate keeps its rule (status `ok` when the service is not behind the 3-month mean
overall on both scopes) and now also reports, per scope, `class_gap_points` (service WAPE minus
baseline WAPE per class) and `classes_behind_baseline`. On the DEV snapshot of 2026-11-13 with
the new code: status `ok`; clients 50.62 against 52.09, behind in `intermittent` (+0.70) and
`lumpy` (+0.51); products 89.01 against 94.12, behind in no class.
