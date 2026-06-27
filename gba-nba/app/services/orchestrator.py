"""Generation orchestrator: run generators → apply anti-spam/throttle → persist.

Window tag = year-month (dedup horizon: one task of a type per client per month, via task_key).
Anti-spam rules (from manager_prefs / config):
  - skip (client, task_type) muted after a recent DISMISS;
  - cap tasks per client per generation run (max_tasks_per_client_per_day);
  - cap total active tasks per manager (max_active_tasks_per_manager).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.logging import get_logger
from app.domain.models import TaskType, Urgency
from app.services import lifecycle
from app.services.generators import (
    churn_winback,
    cross_sell,
    debt_followup,
    reorder_due,
)

log = get_logger("orchestrator")

# Generator collection order is not the final ranking; candidates are sorted by expected EUR below.
# new_client_activation is DROPPED: the propensity model has no head for it (it was excluded from the
# training set — Client.Created is a 1C-sync stamp, not a real signal), and its live generator fires
# on those sync-stamp dates, producing junk tasks. Unwired here so it is never generated/persisted.
_GENERATORS = [debt_followup, reorder_due, churn_winback, cross_sell]

# Per-type share of the manager cap. On real data each of debt/reorder/churn alone produces
# more candidates than the cap, so without a quota the highest-confidence type (debt) fills the
# whole inbox. The quota keeps it a balanced, actionable mix; leftover capacity (e.g. when
# cross_sell is empty because gba-reco is offline) is redistributed in a second pass.
_TYPE_SHARE = {
    TaskType.DEBT_FOLLOWUP: 0.40,
    TaskType.REORDER_DUE: 0.27,
    TaskType.CHURN_WINBACK: 0.16,
    TaskType.CROSS_SELL: 0.17,
}

# Pace coupling: a manager behind their monthly target gets a proportional priority lift —
# revenue tasks (reorder/cross_sell) when SHIPPED is behind, debt-collection when PAID is behind —
# up to max_pace_boost (config) at 100% behind pace. This is how the target gap "drives" the queue.
_SHIPPED_BOOST_TYPES = {TaskType.REORDER_DUE, TaskType.CROSS_SELL}
_PAID_BOOST_TYPES = {TaskType.DEBT_FOLLOWUP}


def _float_score(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ranking_score(task) -> float:
    """Expected-EUR ranking score, with fallback for legacy candidates that lack ev_score."""
    ev_score = getattr(task, "ev_score", None)
    return _float_score(getattr(task, "priority", 0.0)) if ev_score is None else _float_score(ev_score)


def _candidate_sort_key(task) -> tuple:
    return (_ranking_score(task), _float_score(getattr(task, "priority", 0.0)))


def _scale_ranking_fields(task, factor: float) -> None:
    task.priority = min(100.0, round(task.priority * factor, 2))
    ev_score = getattr(task, "ev_score", None)
    if ev_score is not None:
        task.ev_score = round(_float_score(ev_score) * factor, 4)


def _allocate_type_caps(shares: dict, cap: int, present_types: set) -> dict:
    """Turn per-type SHARES into integer per-type caps that sum to EXACTLY the global cap.

    Two policy guarantees:
      1. Largest-remainder allocation, so the caps always sum to `cap` — the raw round(share*cap)
         can overshoot (debt+reorder+churn+new+cross = 18+12+8+5+8 = 51 > 50), letting pass-2 hand
         the +1 slack to the highest-priority deferred type (debt).
      2. A type that is structurally EMPTY for this manager (no candidates this run — e.g. cross_sell
         when reco is offline/401, or a manager with no churn risk) gets NO cap, and its share is
         redistributed PROPORTIONALLY across the remaining present types. Otherwise its slack would
         drain into debt via pass-2, inflating the real debt share (~0.50) well past the intended 0.35.

    When every type is present this reduces to a faithful largest-remainder split of the full shares.
    """
    eligible = {tt: w for tt, w in shares.items() if tt in present_types and w > 0}
    if not eligible:
        return {}
    total_w = sum(eligible.values())
    # ideal (fractional) allocation, renormalised across the present types only
    ideal = {tt: w / total_w * cap for tt, w in eligible.items()}
    floors = {tt: int(x) for tt, x in ideal.items()}
    allocated = sum(floors.values())
    remainder = cap - allocated  # whole seats still to hand out
    # largest fractional remainder wins the leftover seats (ties: heavier share first, then type order)
    order = sorted(eligible, key=lambda tt: (-(ideal[tt] - floors[tt]), -eligible[tt], tt.value))
    caps = dict(floors)
    for tt in order[:max(0, remainder)]:
        caps[tt] += 1
    # never starve a present type to 0 (it has real candidates and a positive share); keep the sum at
    # `cap` by borrowing the seat from the currently-largest cap. Only reachable if present types > cap.
    for tt in sorted(caps, key=lambda t: eligible[t]):
        if caps[tt] == 0:
            donor = max(caps, key=lambda t: caps[t])
            if caps[donor] > 1:
                caps[donor] -= 1
                caps[tt] = 1
    return caps


def _pace_deficit(metric: dict) -> float:
    """Fraction behind pace (0 if on/ahead): max(0, gap) / expected_to_date."""
    expected = metric.get("expected_to_date", 0.0)
    return max(0.0, metric.get("gap", 0.0)) / expected if expected > 0 else 0.0


def _apply_pace_boost(candidates: list, pace: dict) -> None:
    span = get_settings().max_pace_boost - 1.0
    shipped = 1.0 + min(_pace_deficit(pace.get("shipped", {})), 1.0) * span
    paid = 1.0 + min(_pace_deficit(pace.get("paid", {})), 1.0) * span
    for t in candidates:
        if t.task_type in _SHIPPED_BOOST_TYPES:
            factor = shipped
        elif t.task_type in _PAID_BOOST_TYPES:
            factor = paid
        else:
            factor = 1.0
        if factor > 1.0:
            _scale_ranking_fields(t, factor)


def _apply_feedback_penalty(candidates: list, rejections: dict) -> None:
    """Lower the priority of (client, task_type) pairs the manager keeps rejecting (dismissed /
    done-not-sold) — the queue learns from behaviour. Penalty caps at the configured floor."""
    if not rejections:
        return
    s = get_settings()
    for t in candidates:
        n = rejections.get((t.client_id, t.task_type.value), 0)
        if n:
            factor = max(s.feedback_penalty_floor, 1.0 - s.feedback_penalty_per_rejection * n)
            _scale_ranking_fields(t, factor)


def _window_tag(as_of: str) -> str:
    return as_of[:7]  # YYYY-MM


def generate_for_manager(manager_id: int, as_of: str | None = None) -> dict:
    s = get_settings()
    as_of = as_of or datetime.now(UTC).strftime("%Y-%m-%d")
    window = _window_tag(as_of)

    # collect candidates from all generators
    candidates = []
    for gen in _GENERATORS:
        try:
            candidates.extend(gen.generate(manager_id, as_of, window))
        except Exception as exc:  # noqa: BLE001
            log.warning("generator_failed", generator=gen.__name__, manager_id=manager_id, error=str(exc))

    # pace coupling: behind monthly target -> lift revenue (shipped) / debt (paid) task priority.
    # Graceful: if targets/DB are unavailable, generation proceeds without the boost.
    try:
        from app.services import targets
        _apply_pace_boost(candidates, targets.compute_target(manager_id, as_of))
    except Exception as exc:  # noqa: BLE001
        log.warning("pace_boost_skipped", manager_id=manager_id, error=str(exc))

    # feedback learning: sink (client,type) pairs the manager keeps rejecting
    try:
        _apply_feedback_penalty(candidates, lifecycle.feedback_rejections(manager_id, s.feedback_window_days))
    except Exception as exc:  # noqa: BLE001
        log.warning("feedback_penalty_skipped", manager_id=manager_id, error=str(exc))

    # highest expected EUR first so caps keep the BEST tasks; priority remains the legacy fallback
    # and tie-breaker for candidates without an EV score.
    candidates.sort(key=_candidate_sort_key, reverse=True)

    cap = s.max_active_tasks_per_manager
    # caps sum to EXACTLY the global cap (largest-remainder); a structurally-empty type's share is
    # redistributed across the present types instead of leaking into debt via the pass-2 refill.
    present_types = {t.task_type for t in candidates}
    type_cap = _allocate_type_caps(_TYPE_SHARE, cap, present_types)

    # seed caps from tasks ALREADY active this window so re-runs (on-demand /generate) can't exceed
    # the per-client/per-type daily caps; existing tasks are refreshed, not re-counted.
    per_client: dict[int, int] = defaultdict(int, lifecycle.active_counts_by_client(manager_id))
    per_type: dict[TaskType, int] = defaultdict(int)
    _valid_types = {tt.value: tt for tt in TaskType}
    for raw_type, n in lifecycle.active_counts_by_type(manager_id).items():
        if raw_type in _valid_types:
            per_type[_valid_types[raw_type]] = n
    active = lifecycle.active_count(manager_id)
    counters = {"persisted": 0, "skipped_muted": 0, "skipped_capped": 0, "refreshed": 0,
                "crit_debt_reserved": 0}

    def attempt(task) -> None:
        nonlocal active
        if lifecycle.get_task(task.task_key) is not None:
            lifecycle.upsert_generated(task)   # refresh computed fields; no-op if terminal; no re-count
            counters["refreshed"] += 1
            return
        if active >= cap:
            counters["skipped_capped"] += 1
            return
        if lifecycle.is_muted(manager_id, task.client_id, task.task_type.value):
            counters["skipped_muted"] += 1
            return
        if per_client[task.client_id] >= s.max_tasks_per_client_per_day:
            counters["skipped_capped"] += 1
            return
        lifecycle.upsert_generated(task)
        per_client[task.client_id] += 1
        per_type[task.task_type] += 1
        active += 1
        counters["persisted"] += 1

    # pass 1: respect per-type diversity quota; defer overflow
    deferred = []
    for task in candidates:
        if per_type[task.task_type] >= type_cap.get(task.task_type, cap):
            deferred.append(task)
            continue
        attempt(task)
    # pass 2: fill any leftover global capacity, quota lifted (best remaining first)
    for task in deferred:
        attempt(task)

    # pass 3: bounded CRITICAL-debt reserve admitted above the cap (cash-at-risk floor). The reserve
    # is a total active allowance (cap + reserve), not "+reserve every generation run".
    reserve = s.crit_debt_reserve
    if reserve > 0:
        reserve_limit = cap + reserve
        crit_debt = [t for t in candidates
                     if t.task_type == TaskType.DEBT_FOLLOWUP and t.urgency == Urgency.CRITICAL]
        admitted = 0
        for task in crit_debt:
            if active >= reserve_limit:
                break
            if admitted >= reserve:
                break
            if lifecycle.get_task(task.task_key) is not None:
                continue
            if lifecycle.is_muted(manager_id, task.client_id, task.task_type.value):
                continue
            if per_client[task.client_id] >= s.max_tasks_per_client_per_day:
                counters["skipped_capped"] += 1
                continue
            lifecycle.upsert_generated(task)
            per_client[task.client_id] += 1
            per_type[TaskType.DEBT_FOLLOWUP] += 1
            active += 1
            counters["persisted"] += 1
            admitted += 1
        counters["crit_debt_reserved"] = admitted

    stats = {"manager_id": manager_id, "as_of": as_of, "candidates": len(candidates),
             "by_type": {tt.value: n for tt, n in per_type.items()}, **counters}
    log.info("generation_done", **stats)
    return stats
