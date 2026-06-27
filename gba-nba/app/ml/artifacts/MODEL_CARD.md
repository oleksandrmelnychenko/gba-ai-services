# NBA Inbox Propensity Model

Pooled, calibrated `P(outcome | task)` model for the NBA live inbox.

`priority` is the compatibility score `100 * p_outcome`. Live ranking uses
`ev_score = p_outcome * expected_value` (expected EUR), with `priority` only as the fallback for
legacy tasks that do not have `ev_score`.

## What It Predicts

Natural conversion propensity: P(the task's defined outcome happens in `(T, T+H]`, `H=60` days,
given task signals as of `T`).

This is propensity, not manager causal lift. It ranks likely value capture; it does not estimate the
incremental effect of a manager touch.

Outcome labels, leak-safe and strictly after the as-of date:

- `reorder_due`: client re-buys that product.
- `debt_followup`: income payment EUR is at least 50% of overdue amount at `T`.
- `churn_winback`: client places any valid order.
- `cross_sell`: client buys a reco-discovered product.

`new_client_activation` is excluded because `Client.Created` is a 1C sync stamp, not a reliable
activation signal.

## Data

`data/nba_dataset.parquet` historical backfill, signal SQL replayed at each historical `T`, with
manager filter dropped.

Current artifact metrics:

- Rows: 63,378.
- Clients: 862.
- Base rate: 29.2%.
- Features: 21 shared/type-signal/one-hot columns.
- Production model: calibrated HGB.
- Temporal OOT split: train vintages `<= 2026-01-01`, test `2026-02-01..2026-04-01`.

## Model

HistGradientBoosting and LogisticRegression are both isotonic-calibrated. The production model is
selected by OOT calibration and AUC, then refit on all rows for serving.

## Validation

Stratified Group CV, grouped by client:

| metric | HGB | Logit |
|---|---:|---:|
| AUC | 0.698 | 0.696 |
| KS | 0.283 | 0.272 |
| Brier | 0.186 | 0.183 |

Temporal out-of-time split:

| metric | HGB | Logit |
|---|---:|---:|
| AUC | 0.704 | 0.702 |
| KS | 0.280 | 0.279 |
| Brier | 0.177 | 0.178 |

OOT per type, production HGB:

| task_type | n | pos | AUC | KS | Brier |
|---|---:|---:|---:|---:|---:|
| reorder_due | 22,508 | 6,499 | 0.670 | 0.235 | 0.188 |
| debt_followup | 632 | 384 | 0.902 | 0.653 | 0.131 |
| churn_winback | 858 | 255 | 0.815 | 0.496 | 0.149 |
| cross_sell | 1,750 | 145 | 0.725 | 0.375 | 0.074 |

Reliability bins are in `metrics.json` under `oot_per_type.hgb[*].reliability`.

## Benchmark vs Old Priority

AUC on the same outcome label:

| scope | old | model | delta |
|---|---:|---:|---:|
| overall CV | 0.556 | 0.698 | 0.141 |
| OOT future | 0.549 | 0.704 | 0.155 |
| reorder_due CV | 0.528 | 0.661 | 0.133 |
| debt_followup CV | 0.323 | 0.905 | 0.583 |
| churn_winback CV | 0.589 | 0.793 | 0.204 |
| cross_sell CV | 0.705 | 0.726 | 0.020 |

The model beats old priority overall, out-of-time, and on the modeled task types. This card is
generated from `metrics.json` by `app/ml/train.py`; if the metrics change, the card changes with
them.

## E[value] Head

The value head is simple, deterministic, and documented in `train.py` / `score_task.py`:

- `debt_followup`: overdue amount at `T`.
- `reorder_due` / `cross_sell`: approximate average order value, `monetary / order_count`.
- `churn_winback`: trailing-365 turnover, the relationship value at risk.

`score_task()` returns `p_outcome`, `expected_value`, `ev_score`, and `priority`.

## Ship Notes

Ship-worthy as a ranking model. The model improves historical and OOT ranking versus the old expert
priority. Remaining caveat: this is a propensity model, not causal lift; causal attribution still
needs holdout or experiment design.

Operational contract:

- Inbox/caps rank by `ev_score` after the urgency band; task type is only a tie-breaker.
- Fallback to `priority` only when `ev_score` is absent.
- Keep `priority = 100 * p_outcome` as the compatibility field for old clients and legacy docs.

## Artifacts

- `propensity_model.joblib`: final isotonic-calibrated model, refit on all data.
- `model_meta.json`: features, task types, OOT split, formula.
- `metrics.json`: full CV, OOT, per-type, reliability, and benchmark numbers.
- `MODEL_CARD.md`: this generated card.
- `../score_task.py`: serving head for `{p_outcome, expected_value, ev_score, priority}`.
- `../train.py`: training script that reproduces the artifact metrics.
