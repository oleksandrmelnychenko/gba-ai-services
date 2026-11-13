"""Vintaged backfill training-set builder for the NBA propensity model.

For each monthly snapshot T the candidates are enumerated by the SAME readers the live
generators call (app/data/signals_repository.py), scoped to the whole managed client book
instead of one manager: reorder_candidates_for_manager, churn_candidates_for_manager,
active_clients_for_manager, debt_snapshot.task_candidates. A training row and a served task
therefore come from one rule over one population: retained confirmed purchases (invoiced,
non-deleted, non-merged sales that are not accounting carriers), dated by the invoice on its
Europe/Kyiv day. The features are the generator's own (type signals as-of T + the shared
client features of sig.client_features + task_type one-hot).

The per-type outcome label is read on the Kyiv days T <= day < T+H with the same purchase
and payment rules (sig.purchases_between, sig.payments_between), strictly after the cut-off
the candidates were enumerated with.

This measures NATURAL conversion (propensity P(outcome | task)), NOT manager causal lift: the
outcome window covers everyone whether or not a manager would have acted. It is the correct
target for RANKING the inbox by P(outcome)xE[value]; it is not a treatment-effect estimate.

Synthetic/ubiquitous SKUs (the debt-entry «Ввід боргів», resolved live) are excluded from
reorder pairs and cross-sell products, as in serving.
The four types: reorder_due, debt_followup, churn_winback, cross_sell.
new_client_activation is intentionally dropped (Client.Created is a 1C sync stamp, not a real signal).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from app.clients import reco_client
from app.core import history
from app.core.config import get_settings
from app.data import debt_snapshot
from app.data import signals_repository as sig

H_DAYS = 60
DATASET_RULE = "serving-readers-confirmed-invoice-kyiv-v2"
TRAINING_WINDOW_DAYS = 365
DEBT_PAYDOWN_FRACTION = 0.5  # >=50% of overdue@T paid in (T,T+H] = debt_followup success.


def _t_plus_h(asof: str, h: int = H_DAYS) -> str:
    d = dt.date.fromisoformat(asof) + dt.timedelta(days=h)
    return d.isoformat()


def _excluded_pids(asof: str) -> set[int]:
    """The synthetic accounting ids (HARD guard — debt-entry «Ввід боргів», resolved live via
    sig.synthetic_product_ids so re-mints can't stale the pin) UNION the data-driven ubiquity set
    (generator-calibrated pct). The synthetic ids are excluded unconditionally so the guard holds
    even if the debt-entry's rolling ubiquity ever dips below ubiquity_exclude_pct. Identical
    exclusion semantics to app.data.signals_repository._excluded so the live feature row matches
    the training distribution."""
    s = get_settings()
    return set(sig.synthetic_product_ids()) | set(
        sig.ubiquitous_product_ids(s.ubiquity_exclude_pct, asof)
    )


# --------------------------------------------------------------------------------------
# Shared as-of client features (turnover/recency/order_count), trailing 365d, manager-free.
# --------------------------------------------------------------------------------------
def client_features(client_ids: list[int], asof: str, window_days: int = 365) -> dict[int, dict]:
    """The serving features themselves, so a training row and a live row share one definition."""
    return sig.client_features(client_ids, asof, window_days)


# --------------------------------------------------------------------------------------
# Candidate enumeration — the generators' own readers over the whole managed book, as_of=T.
# --------------------------------------------------------------------------------------
def reorder_candidates(asof: str) -> list[dict]:
    s = get_settings()
    rows = sig.reorder_candidates_for_manager(
        None, asof, min_cycle_days=s.reorder_min_cycle_days,
        max_overdue_mult=s.reorder_max_overdue_mult)
    excl = _excluded_pids(history.require_as_of(asof).isoformat())
    out = []
    for r in rows:
        pid = int(r["product_id"])
        if pid in excl:
            continue
        cyc = float(r["cycle_days"] or 0)
        out.append({
            "client_id": int(r["client_id"]),
            "product_id": pid,
            "n_orders": int(r["n_orders"]),
            "cycle_days": cyc,
            "elapsed_days": float(r["elapsed_days"]),
            "overdue_ratio": (float(r["elapsed_days"]) / cyc) if cyc > 0 else 1.0,
        })
    return out


def debt_candidates(asof: str) -> list[dict]:
    settings = get_settings()
    return [{
        "client_id": int(r["client_id"]),
        "overdue_amount": float(r["overdue_amount"]),
        "max_overdue_days": int(r["max_overdue_days"]),
        "days_past_terms": int(r["max_days_past_terms"]),
        "debt_lines": int(r["debt_lines"]),
    } for r in debt_snapshot.task_candidates(
        debt_snapshot.overdue_clients(asof), settings.debt_min_amount, settings.debt_grace_days)]


def churn_candidates(asof: str) -> list[dict]:
    out = []
    for r in sig.churn_candidates_for_manager(None, asof):
        recent = int(r["recent_orders"] or 0)
        prior = int(r["prior_orders"] or 0)
        out.append({
            "client_id": int(r["client_id"]),
            "recent_orders": recent,
            "prior_orders": prior,
            "silence_days": int(r["silence_days"] or 0),
            "drop_ratio": round(recent / prior, 2) if prior else 0.0,
        })
    return out


def active_clients(asof: str) -> list[dict]:
    """cross_sell pool: per manager the cross_sell_max_clients most valuable clients with
    >= cross_sell_min_orders confirmed orders in cross_sell_recent_days — the clients the
    generator asks reco about."""
    s = get_settings()
    rows = sig.active_clients_for_manager(
        None, asof, recent_days=s.cross_sell_recent_days, min_orders=s.cross_sell_min_orders)
    feats = sig.client_features([int(r["client_id"]) for r in rows], asof)
    by_manager: dict[int, list[dict]] = {}
    for r in rows:
        by_manager.setdefault(int(r["manager_id"]), []).append(r)
    out: list[dict] = []
    for manager_id in sorted(by_manager):
        clients = sorted(
            by_manager[manager_id],
            key=lambda c: (-(feats.get(int(c["client_id"]), {}).get("monetary") or 0.0),
                           int(c["client_id"])))
        out.extend({"client_id": int(c["client_id"]), "client_net_uid": str(c["client_net_uid"])}
                   for c in clients[:s.cross_sell_max_clients])
    return out


_RECO_CACHE_DIR = "data/reco_cache"


def _reco_cache_dir() -> str:
    import os

    path = os.path.join(_RECO_CACHE_DIR, f"source_{history.source_history_start().isoformat()}")
    os.makedirs(path, exist_ok=True)
    return path


def _reco_raw_cached(client: dict, asof: str, reco_timeout: int) -> tuple[list[dict], bool]:
    """(recommendations, answered) of reco /recommend/copurchase for one client as of T.

    An answer is persisted under the client's NetUID together with the client id it was given
    for and is reused only while both still name the same client: a database rebuild re-mints
    ids, and an entry of the old numbering can never be joined to a current client. An empty
    answer of a healthy reco is an answer and is cached; a failed call is not, so the next run
    asks again.
    """
    import json
    import os

    cid, net_uid = int(client["client_id"]), str(client["client_net_uid"]).lower()
    canonical_asof = history.require_as_of(asof).isoformat()
    source_start = history.source_history_start().isoformat()
    path = os.path.join(_reco_cache_dir(), f"{canonical_asof}_{net_uid}.json")
    if os.path.exists(path):
        with open(path) as fh:
            cached = json.load(fh)
        if (
            isinstance(cached, dict)
            and cached.get("source_history_start") == source_start
            and cached.get("as_of") == canonical_asof
            and cached.get("client_id") == cid
            and cached.get("client_net_uid") == net_uid
            and isinstance(cached.get("recommendations"), list)
        ):
            return cached["recommendations"], True
    from app.services.generators.cross_sell import _RECO_REQUEST_N
    recs: list[dict] = []
    answered = False
    for _attempt in range(2):
        failures = reco_client.recommend_failures()
        recs = reco_client.recommend(cid, top_n=_RECO_REQUEST_N, as_of_date=canonical_asof,
                                     path="/recommend/copurchase", timeout=reco_timeout)
        answered = reco_client.recommend_failures() == failures
        if answered:
            break
    if not answered:
        return [], False
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(
            {
                "source_history_start": source_start,
                "as_of": canonical_asof,
                "client_id": cid,
                "client_net_uid": net_uid,
                "recommendations": recs,
            },
            fh,
        )
    os.replace(tmp, path)
    return recs, True


def prune_reco_cache(known_net_uids: set[str]) -> int:
    """Drop cache entries that cannot be joined to a current client: the files of the old
    client-id numbering and those of clients that no longer exist."""
    import os

    removed = 0
    directory = _reco_cache_dir()
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        key = name[:-5].split("_", 1)[-1]
        if key not in known_net_uids:
            os.remove(os.path.join(directory, name))
            removed += 1
    return removed


RECO_COVERAGE: dict[str, dict] = {}


def cross_sell_candidates(asof: str, max_workers: int | None = None,
                          reco_timeout: int | None = None) -> list[dict]:
    """One candidate per client of the cross-sell pool that reco (as_of T) returns >=1 discovery
    product for. Mirrors the generator: copurchase engine, discovery source, score>=0.05,
    top-5 kept. The discovered product ids are captured so the label can check a real buy.

    Reco answers one request in seconds, so the fan-out is small and patient
    (retrain_reco_workers, retrain_reco_timeout): a wide burst only times out and leaves the
    type without rows. How many clients were asked and how many calls failed is kept in
    RECO_COVERAGE[asof] for the retrain report.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.services.generators.cross_sell import _MAX_PRODUCTS, _MIN_SCORE
    settings = get_settings()
    coverage = RECO_COVERAGE[asof] = {"asked": 0, "failed": 0, "offline": False}
    if not reco_client.is_healthy():
        coverage["offline"] = True
        return []
    clients = active_clients(asof)
    excl = _excluded_pids(asof)
    timeout = reco_timeout or settings.retrain_reco_timeout

    def _one(client: dict) -> tuple[dict | None, bool]:
        recs, answered = _reco_raw_cached(client, asof, timeout)
        disc = [r for r in recs if r.get("source") == "discovery"
                and float(r.get("score", 0)) >= _MIN_SCORE
                and int(r.get("product_id", 0)) not in excl]
        if not disc:
            return None, answered
        disc = disc[:_MAX_PRODUCTS]
        return {
            "client_id": int(client["client_id"]),
            "reco_product_ids": [int(r["product_id"]) for r in disc],
            "top_score": round(float(disc[0].get("score", 0)), 3),
            "candidates": len(disc),
        }, answered

    with ThreadPoolExecutor(max_workers=max_workers or settings.retrain_reco_workers) as ex:
        results = list(ex.map(_one, clients))
    coverage["asked"] = len(results)
    coverage["failed"] = sum(1 for _row, answered in results if not answered)
    return [row for row, _answered in results if row is not None]


# --------------------------------------------------------------------------------------
# Leak-safe outcome labels on the Kyiv days T <= day < T+H, by the serving purchase rule.
# --------------------------------------------------------------------------------------
def _bought(asof: str, client_ids: list[int]) -> dict[int, set[int]]:
    bought: dict[int, set[int]] = {}
    for r in sig.purchases_between(client_ids, asof, _t_plus_h(asof)):
        bought.setdefault(int(r["client_id"]), set()).add(int(r["product_id"]))
    return bought


def label_reorder(asof: str, pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """(client_id, product_id) pairs where the client buys THAT product again in the window."""
    if not pairs:
        return set()
    bought = _bought(asof, [c for c, _ in pairs])
    return {(c, p) for c, p in pairs if p in bought.get(c, set())}


def label_any_order(asof: str, client_ids: list[int]) -> set[int]:
    """clients with ANY confirmed purchase in the window — used for churn_winback success."""
    if not client_ids:
        return set()
    return set(_bought(asof, client_ids))


def label_debt_paydown(asof: str, overdue_by_client: dict[int, float]) -> set[int]:
    """clients whose EUR payments in the window cover >= DEBT_PAYDOWN_FRACTION of overdue@T."""
    if not overdue_by_client:
        return set()
    paid = sig.payments_between(list(overdue_by_client), asof, _t_plus_h(asof))
    return {cid for cid, ov in overdue_by_client.items()
            if ov > 0 and float(paid.get(cid, 0)) >= DEBT_PAYDOWN_FRACTION * ov}


def label_buy_products(asof: str, client_products: dict[int, list[int]]) -> set[int]:
    """clients who buy ANY of their reco-discovered product ids in the window."""
    if not client_products:
        return set()
    bought = _bought(asof, list(client_products))
    return {cid for cid, ps in client_products.items() if bought.get(cid, set()) & set(ps)}


# --------------------------------------------------------------------------------------
# Old (expert-guessed) priority — recomputed here so it can be benchmarked against the label.
# --------------------------------------------------------------------------------------
def _old_priority(task_type: str, row: dict, monetary: float) -> float:
    from app.services import scoring
    if task_type == "debt_followup":
        u = scoring.debt_urgency(int(row["days_past_terms"]))
        v = scoring.value_from_monetary(float(row["overdue_amount"]))
        return scoring.priority(u, v, 1.0)
    if task_type == "reorder_due":
        u = scoring.reorder_urgency(float(row["elapsed_days"]), float(row["cycle_days"]))
        v = scoring.value_from_monetary(monetary)
        conf = min(1.0, 0.4 + 0.1 * float(row["n_orders"]))
        return scoring.priority(u, v, conf)
    if task_type == "churn_winback":
        u = scoring.churn_urgency(float(row["drop_ratio"]), int(row["silence_days"]))
        v = scoring.value_from_monetary(monetary)
        conf = min(1.0, 0.5 + 0.05 * int(row["prior_orders"]))
        return scoring.priority(u, v, conf)
    if task_type == "cross_sell":
        u = scoring.crosssell_urgency(float(row["top_score"]))
        v = scoring.value_from_monetary(monetary)
        conf = float(row["top_score"])
        return scoring.priority(u, v, conf)
    return 0.0


# --------------------------------------------------------------------------------------
# Per-snapshot assembly.
# --------------------------------------------------------------------------------------
def build_snapshot(asof: str) -> pd.DataFrame:
    """Enumerate all 4 types at vintage T, compute features + leak-safe labels, return rows."""
    coverage = history.training_window(asof, TRAINING_WINDOW_DAYS)
    recs: list[dict] = []

    # --- gather candidates per type ---
    reorder = reorder_candidates(asof)
    debt = debt_candidates(asof)
    churn = churn_candidates(asof)
    cross = cross_sell_candidates(asof)

    # --- shared as-of client features (union of all candidate clients) ---
    all_cids = sorted({r["client_id"] for r in reorder}
                      | {r["client_id"] for r in debt}
                      | {r["client_id"] for r in churn}
                      | {r["client_id"] for r in cross})
    feats = client_features(all_cids, asof)

    def f(cid: int, key: str, default=0.0):
        v = feats.get(cid, {}).get(key)
        return default if v is None else v

    # --- labels ---
    reorder_pairs = [(r["client_id"], r["product_id"]) for r in reorder]
    reorder_pos = label_reorder(asof, reorder_pairs)
    debt_overdue = {r["client_id"]: r["overdue_amount"] for r in debt}
    debt_pos = label_debt_paydown(asof, debt_overdue)
    churn_pos = label_any_order(asof, [r["client_id"] for r in churn])
    cross_products = {r["client_id"]: r["reco_product_ids"] for r in cross}
    cross_pos = label_buy_products(asof, cross_products)

    def base(cid: int, ttype: str) -> dict:
        return {
            "vintage": coverage.as_of.isoformat(),
            "source_history_start": coverage.source_history_start.isoformat(),
            "effective_start": coverage.effective_start.isoformat(),
            "history_complete": coverage.history_complete,
            "dataset_rule": DATASET_RULE,
            "task_type": ttype, "client_id": cid,
            "monetary": f(cid, "monetary"),
            "recency_days": f(cid, "recency_days", 9999),
            "order_count": int(f(cid, "order_count", 0)),
            "is_reorder_due": int(ttype == "reorder_due"),
            "is_debt_followup": int(ttype == "debt_followup"),
            "is_churn_winback": int(ttype == "churn_winback"),
            "is_cross_sell": int(ttype == "cross_sell"),
            # type signals default 0; the owning type fills its own:
            "sig_overdue_amount": 0.0, "sig_days_past_terms": 0.0, "sig_max_overdue_days": 0.0,
            "sig_debt_lines": 0.0,
            "sig_elapsed_days": 0.0, "sig_cycle_days": 0.0, "sig_overdue_ratio": 0.0,
            "sig_n_orders": 0.0,
            "sig_drop_ratio": 0.0, "sig_silence_days": 0.0, "sig_recent_orders": 0.0,
            "sig_prior_orders": 0.0,
            "sig_top_score": 0.0, "sig_reco_candidates": 0.0,
        }

    for r in reorder:
        cid = r["client_id"]
        row = base(cid, "reorder_due")
        row.update({"product_id": r["product_id"],
                    "sig_elapsed_days": r["elapsed_days"], "sig_cycle_days": r["cycle_days"],
                    "sig_overdue_ratio": r["overdue_ratio"], "sig_n_orders": r["n_orders"]})
        row["label"] = int((cid, r["product_id"]) in reorder_pos)
        row["old_priority"] = _old_priority("reorder_due", r, row["monetary"])
        recs.append(row)

    for r in debt:
        cid = r["client_id"]
        row = base(cid, "debt_followup")
        row.update({"sig_overdue_amount": r["overdue_amount"],
                    "sig_days_past_terms": r["days_past_terms"],
                    "sig_max_overdue_days": r["max_overdue_days"], "sig_debt_lines": r["debt_lines"]})
        row["label"] = int(cid in debt_pos)
        row["old_priority"] = _old_priority("debt_followup", r, row["monetary"])
        recs.append(row)

    for r in churn:
        cid = r["client_id"]
        row = base(cid, "churn_winback")
        row.update({"sig_drop_ratio": r["drop_ratio"], "sig_silence_days": r["silence_days"],
                    "sig_recent_orders": r["recent_orders"], "sig_prior_orders": r["prior_orders"]})
        row["label"] = int(cid in churn_pos)
        row["old_priority"] = _old_priority("churn_winback", r, row["monetary"])
        recs.append(row)

    for r in cross:
        cid = r["client_id"]
        row = base(cid, "cross_sell")
        row.update({"sig_top_score": r["top_score"], "sig_reco_candidates": r["candidates"]})
        row["label"] = int(cid in cross_pos)
        row["old_priority"] = _old_priority("cross_sell", r, row["monetary"])
        recs.append(row)

    return pd.DataFrame(recs)


def build_dataset(snapshots: list[str]) -> pd.DataFrame:
    canonical = [
        history.training_window(snapshot, TRAINING_WINDOW_DAYS).as_of.isoformat()
        for snapshot in snapshots
    ]
    if len(set(canonical)) != len(canonical):
        raise ValueError("training snapshots must be unique")
    if canonical != sorted(canonical):
        raise ValueError("training snapshots must be sorted ascending")
    frames = []
    for t in canonical:
        df = build_snapshot(t)
        frames.append(df)
        by_type = df.groupby("task_type")["label"].agg(["size", "mean"]) if len(df) else None
        print(f"[{t}] rows={len(df)}")
        if by_type is not None:
            for ttype, r in by_type.iterrows():
                print(f"      {ttype:16s} n={int(r['size']):5d}  base_rate={r['mean']:.1%}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------------------------------
# LIVE labels — real manager-logged outcomes from Mongo, shaped into the training schema.
#
# Once managers start logging Outcome(sold) on terminal tasks, those rows are GROUND TRUTH
# (a manager touched the client and recorded whether it converted) and are far more valuable
# than the backfill's natural-conversion proxy. This pulls terminal tasks that carry an
# explicit outcome and maps each task's stored `signals` dict (the exact feature payload the
# generator computed at creation time) onto the dataset's `sig_*` / shared / one-hot columns,
# so a live row is row-for-row unionable with a backfill row.
#
# TODAY: 0 live labels exist (managers haven't begun logging), so this returns an empty frame
# and the training set is backfill-only. The path is wired and tested so the moment real
# outcomes land they flow into the next retrain with no code change.
# --------------------------------------------------------------------------------------

# Map a Task.signals key -> the dataset feature column. Mirrors each generator's signals dict.
_LIVE_SIGNAL_MAP = {
    # debt_followup
    "overdue_amount": "sig_overdue_amount",
    "days_past_terms": "sig_days_past_terms",
    "max_overdue_days": "sig_max_overdue_days",
    "debt_lines": "sig_debt_lines",
    # reorder_due
    "elapsed_days": "sig_elapsed_days",
    "cycle_days": "sig_cycle_days",
    "overdue_ratio": "sig_overdue_ratio",
    "n_orders": "sig_n_orders",
    # churn_winback
    "drop_ratio": "sig_drop_ratio",
    "silence_days": "sig_silence_days",
    "recent_orders": "sig_recent_orders",
    "prior_orders": "sig_prior_orders",
    # cross_sell
    "top_score": "sig_top_score",
    "candidates": "sig_reco_candidates",
}

_LIVE_TASK_TYPES = {"reorder_due", "debt_followup", "churn_winback", "cross_sell"}


def _live_row_from_task(doc: dict) -> dict | None:
    """Map one terminal Mongo task (with an explicit outcome) to a training row, or None to skip.

    The label is the manager-recorded conversion: outcome.sold. Features come from the task's
    persisted `signals` dict (what the generator computed at creation), so they match the
    backfill's leak-safe as-of feature vector exactly.
    """
    tt = doc.get("task_type")
    if tt not in _LIVE_TASK_TYPES:
        return None  # new_client_activation is out of the model
    outcome = doc.get("outcome")
    if not outcome or outcome.get("sold") is None:
        return None  # no recorded outcome -> not a label
    sig = doc.get("signals") or {}
    source_start = history.source_history_start().isoformat()
    signal_as_of = sig.get("as_of")
    try:
        coverage = history.training_window(signal_as_of, TRAINING_WINDOW_DAYS)
    except (TypeError, ValueError):
        return None
    if (
        sig.get("source_history_start") != source_start
        or sig.get("history_complete") is not True
        or sig.get("effective_start") != coverage.effective_start.isoformat()
    ):
        return None
    cid = doc.get("client_id")
    if cid is None:
        return None

    row: dict = {
        "vintage": coverage.as_of.isoformat(),
        "source_history_start": source_start,
        "effective_start": str(sig["effective_start"]),
        "history_complete": True,
        "task_type": tt,
        "client_id": int(cid),
        "monetary": float(sig.get("monetary") or 0.0),
        "recency_days": float(sig.get("recency_days") if sig.get("recency_days") is not None else 9999),
        "order_count": int(sig.get("order_count") or 0),
        "is_reorder_due": int(tt == "reorder_due"),
        "is_debt_followup": int(tt == "debt_followup"),
        "is_churn_winback": int(tt == "churn_winback"),
        "is_cross_sell": int(tt == "cross_sell"),
        "sig_overdue_amount": 0.0, "sig_days_past_terms": 0.0, "sig_max_overdue_days": 0.0,
        "sig_debt_lines": 0.0,
        "sig_elapsed_days": 0.0, "sig_cycle_days": 0.0, "sig_overdue_ratio": 0.0, "sig_n_orders": 0.0,
        "sig_drop_ratio": 0.0, "sig_silence_days": 0.0, "sig_recent_orders": 0.0, "sig_prior_orders": 0.0,
        "sig_top_score": 0.0, "sig_reco_candidates": 0.0,
        "label": int(bool(outcome.get("sold"))),
        "old_priority": float(doc.get("priority") or 0.0),
        "is_live": 1,
    }
    for k, col in _LIVE_SIGNAL_MAP.items():
        if k in sig and sig[k] is not None:
            row[col] = float(sig[k])
    return row


def live_labels() -> pd.DataFrame:
    """Terminal tasks with a manager-recorded outcome, shaped into the training-row schema.

    Returns an EMPTY DataFrame (no rows) when Mongo is unreachable or no outcomes are logged yet,
    so callers can unconditionally `pd.concat` it onto the backfill. Best-effort: never raises.
    """
    try:
        from app.data import mongo
        from app.domain.models import TaskStatus
        cur = mongo.tasks().find(
            {"status": {"$in": [TaskStatus.DONE.value, TaskStatus.DISMISSED.value]},
             "outcome.sold": {"$ne": None}},
            {"task_type": 1, "client_id": 1, "signals": 1, "outcome": 1, "priority": 1},
        )
        rows = [r for r in (_live_row_from_task(d) for d in cur) if r is not None]
    except Exception as exc:  # noqa: BLE001 — Mongo down / not configured -> backfill-only retrain
        print(f"[live_labels] none pulled ({type(exc).__name__}: {exc})")
        return pd.DataFrame()
    print(f"[live_labels] pulled {len(rows)} manager-logged outcome rows")
    return pd.DataFrame(rows)
