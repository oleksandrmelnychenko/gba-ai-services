export const meta = {
  name: 'nba-realdata-calibration',
  description: 'Calibrate NBA signal/scoring thresholds against live ConcordDb_V5 + generated inbox',
  phases: [
    { title: 'Analyze', detail: 'one agent per signal/scoring dimension, evidence from live DB' },
    { title: 'Verify', detail: 'adversarially verify each proposed threshold change' },
  ],
}

const CTX = `
You are calibrating the GBA NBA (next-best-action) task engine against REAL data.
Repo: /root/projects/gba-nba  (Python 3, venv at .venv, .env already configured).
DB: ConcordDb_V5 (read-only login gba_nba_ro, db_datareader). Mongo holds generated tasks.
as_of date for ALL analysis: 2026-06-08. ALL money is EUR.
Active managers: 10146 (Баранов), 10182 (Мот), 10183 (Гураль), 10184 (Крицький); head 10162 (Грель) also sells.
Managers 10150/10156 are empty test accounts — ignore.
Data is CURRENT: orders & payments run through Jun 2026.

HOW TO GET EVIDENCE (read-only; never write code, never modify the DB):
  Run SQL:    cd /root/projects/gba-nba && .venv/bin/python -c "from app.data.db import query; print(query('SELECT ...', {'k':v}))"
  Run signal: cd /root/projects/gba-nba && .venv/bin/python -c "from app.data import signals_repository as R; print(R.<fn>(...))"
  Read inbox: cd /root/projects/gba-nba && .venv/bin/python -c "from app.data import mongo; import collections; ..."
  Strip noise by piping through: 2>&1 | grep -v '\"event\": \"mongo'

KEY FILES (read them):
  app/core/config.py            — all tunable knobs (current values + comments)
  app/services/scoring.py       — urgency/value/priority functions
  app/data/signals_repository.py— the SQL signal queries
  app/services/generators/*.py  — how each task type is built

CURRENT CALIBRATION (knobs): debt_min_amount=10, debt_max_age_days=365,
  reorder_min_cycle_days=7, reorder_max_overdue_mult=3.0, ubiquity_exclude_pct=0.20,
  cross_sell_recent_days=120, cross_sell_min_orders=3,
  w_urgency=0.5, w_value=0.3, w_confidence=0.2, value_saturation=6000,
  urgency_band_critical=0.85/high=0.6/normal=0.3, max_pace_boost=1.25, target_trailing_months=3.

YOUR JOB: judge whether YOUR dimension's thresholds produce ACTIONABLE, well-prioritized tasks on
real data. A good signal: fires on the right clients, not too noisy, not too sparse, urgency bands
are meaningful (not all-critical or all-low), value reflects real money. Propose concrete knob
changes ONLY when the data justifies it; otherwise CONFIRM current values. Be specific and quantitative.
`

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['dimension', 'health', 'observations', 'proposals'],
  properties: {
    dimension: { type: 'string' },
    health: { type: 'string', enum: ['good', 'needs_tuning', 'broken'] },
    observations: { type: 'array', items: { type: 'string' }, description: 'Quantitative findings from the live data' },
    proposals: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['knob', 'current', 'proposed', 'rationale', 'evidence', 'confidence'],
        properties: {
          knob: { type: 'string', description: 'config.py field name, or "code:<file>" for a logic change' },
          current: { type: 'string' },
          proposed: { type: 'string' },
          rationale: { type: 'string' },
          evidence: { type: 'string', description: 'concrete numbers/queries that justify it' },
          confidence: { type: 'number' },
        },
      },
    },
  },
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['knob', 'verdict', 'final_value', 'reasoning'],
  properties: {
    knob: { type: 'string' },
    verdict: { type: 'string', enum: ['confirm', 'reject', 'adjust'] },
    final_value: { type: 'string', description: 'the value to actually use (may differ from proposed if adjust)' },
    reasoning: { type: 'string', description: 'adversarial check: does it drop real signal? is the evidence sound?' },
  },
}

const DIMENSIONS = [
  { key: 'debt', brief: `DEBT_FOLLOWUP. Knobs: debt_min_amount(10), debt_max_age_days(365). Signal: signals_repository.overdue_debts_for_manager. Scoring: scoring.debt_urgency (floor 0.6, ->critical ~100d past terms). Check: amount distribution, days-past-terms distribution, is the €10 floor right (how many sub-€X are settled noise vs real?), are old debts (near 365d) actionable, is urgency banding sane (not everything critical/high). Verify EUR (overdue_amount is summed Debt.Total).` },
  { key: 'reorder', brief: `REORDER_DUE. Knobs: reorder_min_cycle_days(7), reorder_max_overdue_mult(3.0), ubiquity_exclude_pct(0.20). Signal: reorder_candidates_for_manager (one task PER CLIENT, top-5 products). Scoring: scoring.reorder_urgency (linear 1x->0.30, 3x->0.85). Check: cycle_days & overdue_ratio distributions, is min_cycle floor suppressing real fast-movers, is max_overdue_mult cutting real reorders vs abandoned, urgency band spread, ubiquity exclusion (only "Ввід боргів"? any other synthetic SKU leaking?).` },
  { key: 'churn', brief: `CHURN_WINBACK. Knobs: hard-coded in query (prior_orders>=2, recent/prior<0.5, recent_days=90, baseline_days=365). Scoring: scoring.churn_urgency. Check: candidate volume per manager (~70-90 — too many?), drop_ratio & silence_days distributions, is <0.5 the right cutoff, do candidates overlap heavily with reorder/debt (double-counting same client), should there be a minimum monetary so winback targets real value. Note recent_orders can be huge (B2B clients order daily).` },
  { key: 'new_client', brief: `NEW_CLIENT_ACTIVATION. Knobs: new_clients_for_manager(recent_days=90, max_orders=0). Scoring: scoring.new_client_urgency. Check: candidate volume (real data shows only clients <=13 days old appear — why no 14-90d? is recent_days=90 effective or is the data only recently linking MainManagerID?), is max_orders=0 right (vs <=1), urgency curve.` },
  { key: 'cross_sell', brief: `CROSS_SELL via gba-reco copurchase (running on :8000). Knobs: cross_sell_recent_days(120), cross_sell_min_orders(3), _MIN_SCORE=0.05, _RECO_REQUEST_N=25. Check: does copurchase actually RETURN discovery items for active clients (test reco_client.recommend on 5-10 real active clients, path=/recommend/copurchase, look for source=="discovery")? what's the discovery score distribution? is _MIN_SCORE=0.05 right? how many cross_sell tasks actually get generated per manager? is min_orders=3 the right activity gate? Confirm the 4-5x reco-call reduction is real.` },
  { key: 'scoring_targets', brief: `SCORING WEIGHTS + TARGETS. Knobs: w_urgency(0.5)/w_value(0.3)/w_confidence(0.2), value_saturation(6000 EUR), urgency_band_critical(0.85)/high(0.6)/normal(0.3), max_pace_boost(1.25), target_trailing_months(3). Read the GENERATED Mongo inbox across all managers: priority distribution per task_type, urgency-band histogram, does debt actually sort above reorder, is value_saturation=6000 right vs real client annual monetary distribution, are targets (run-rate) sane vs actual monthly shipped/paid. Is any task_type systematically dominating or never surfacing?` },
]

phase('Analyze')
const results = await pipeline(
  DIMENSIONS,
  d => agent(`${CTX}\n\n=== YOUR DIMENSION: ${d.key} ===\n${d.brief}\n\nGather evidence from the live DB and the generated Mongo inbox, then return findings. CONFIRM knobs that are already well-calibrated (empty proposals is a valid, good answer). Only propose changes the data clearly supports.`,
    { label: `analyze:${d.key}`, phase: 'Analyze', schema: FINDINGS_SCHEMA }),
  (findings, d) => {
    const props = (findings?.proposals || [])
    if (!props.length) return { dimension: d.key, findings, verdicts: [] }
    return parallel(props.map(p => () =>
      agent(`${CTX}\n\n=== ADVERSARIAL VERIFICATION (dimension: ${d.key}) ===\nA calibration agent proposed this change:\n  knob: ${p.knob}\n  current: ${p.current}\n  proposed: ${p.proposed}\n  rationale: ${p.rationale}\n  evidence claimed: ${p.evidence}\n\nYour job: TRY TO REFUTE it. Independently query the live data. Would this change drop REAL actionable signal (false negatives)? Is the evidence numerically sound or cherry-picked? Is there a better value? Default to 'reject' or 'adjust' if the evidence is weak. Only 'confirm' if you independently reproduce the justification.`,
        { label: `verify:${d.key}:${p.knob}`, phase: 'Verify', schema: VERDICT_SCHEMA })
        .then(v => ({ ...p, verdict: v }))))
      .then(verdicts => ({ dimension: d.key, findings, verdicts }))
  }
)

return results.filter(Boolean)
