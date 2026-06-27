"""Vintaged backfill training-set builder for the NBA propensity model.

For each monthly snapshot T, this replays the EXACT as-of signal SQL that the live generators
use (app/data/signals_repository.py), with the per-manager filter DROPPED so every candidate
task instance in the book is enumerated at the historical vintage. It then computes a leak-safe
feature vector (type-specific signals as-of T + shared client_monetary/recency/order_count as-of T
+ task_type one-hot) and joins the per-type H-day OUTCOME label evaluated strictly in (T, T+H].

This measures NATURAL conversion (propensity P(outcome | task)), NOT manager causal lift: the
outcome window covers everyone whether or not a manager would have acted. It is the correct
target for RANKING the inbox by P(outcome)xE[value]; it is not a treatment-effect estimate.

Data traps honored (ConcordDb_V5), identical to the generators:
  * Sale/Order/OrderItem validity = oi.IsValidForCurrentSale=1 (NOT Deleted: dbo.Order/OrderItem
    are nearly all Deleted=1, so Deleted=0 kept only ~16% of sales); Order.Created is the event
    time used for windows.
  * PricePerItem is already EUR (turnover via SUM(Qty*PricePerItem)).
  * Debt.Total -> EUR via dbo.GetExchangedToEuroValue(Total, ISNULL(Agreement.CurrencyID,2), Created).
  * Payments: IncomePaymentOrder.Amount -> EUR via GetExchangedToEuroValue(Amount,CurrencyID,FromDate);
    payment event time is FromDate (NOT the bulk-sync Created).
  * Synthetic/ubiquitous SKUs (e.g. debt-entry) excluded from reorder + monetary.
  * Hard-exclude the configured synthetic ids (settings.synthetic_product_ids, default {25422404})
    everywhere — unconditional, independent of the data-driven ubiquity threshold.
The four types: reorder_due, debt_followup, churn_winback, cross_sell.
new_client_activation is intentionally dropped (Client.Created is a 1C sync stamp, not a real signal).
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from app.clients import reco_client
from app.core.config import get_settings
from app.data import signals_repository as sig
from app.data.db import in_clause, query

H_DAYS = 60
DEBT_PAYDOWN_FRACTION = 0.5  # >=50% of overdue@T paid in (T,T+H] = debt_followup success.


def _t_plus_h(asof: str, h: int = H_DAYS) -> str:
    d = dt.date.fromisoformat(asof) + dt.timedelta(days=h)
    return d.isoformat()


def _excluded_pids() -> set[int]:
    """The configured synthetic accounting ids (HARD guard, pinned in settings.synthetic_product_ids
    — e.g. debt-entry 25422404) UNION the data-driven ubiquity set (generator-calibrated pct). The
    synthetic ids are excluded unconditionally so the guard holds even if 25422404's rolling ubiquity
    ever dips below ubiquity_exclude_pct. Identical exclusion semantics to
    app.data.signals_repository._excluded so the live feature row matches the training distribution."""
    s = get_settings()
    return set(s.synthetic_product_ids) | set(sig.ubiquitous_product_ids(s.ubiquity_exclude_pct))


# --------------------------------------------------------------------------------------
# Shared as-of client features (turnover/recency/order_count), trailing 365d, manager-free.
# --------------------------------------------------------------------------------------
def client_features(client_ids: list[int], asof: str, window_days: int = 365) -> dict[int, dict]:
    """Per client as-of T: trailing-365d EUR turnover, days-since-last-order (recency),
    and trailing-365d distinct order_count. Synthetic + hard-excluded SKUs removed from turnover."""
    if not client_ids:
        return {}
    out: dict[int, dict] = {cid: {"monetary": 0.0, "recency_days": None, "order_count": 0}
                            for cid in client_ids}
    ph, params = in_clause("c", client_ids)
    excl = _excluded_pids()
    eph, eparams = in_clause("x", sorted(excl))
    rows = query(
        f"""
        SELECT ca.ClientID AS client_id,
               SUM(oi.Qty * oi.PricePerItem) AS monetary,
               COUNT(DISTINCT o.ID) AS order_count,
               DATEDIFF(day, MAX(o.Created), :asof) AS recency_days
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created >= DATEADD(day, -:win, :asof) AND o.Created < :asof
              AND oi.ProductID IS NOT NULL AND ca.ClientID IN {ph}
              AND oi.ProductID NOT IN {eph}
        GROUP BY ca.ClientID
        """,
        {"asof": asof, "win": window_days, **params, **eparams},
    )
    for r in rows:
        cid = int(r["client_id"])
        out[cid] = {
            "monetary": float(r["monetary"] or 0.0),
            "order_count": int(r["order_count"] or 0),
            "recency_days": int(r["recency_days"]) if r["recency_days"] is not None else None,
        }
    return out


# --------------------------------------------------------------------------------------
# Candidate enumeration — generator signal SQL, manager filter dropped, as_of=T.
# --------------------------------------------------------------------------------------
def reorder_candidates(asof: str) -> list[dict]:
    s = get_settings()
    rows = query(
        """
        WITH per_product AS (
            SELECT ca.ClientID AS client_id, oi.ProductID AS product_id,
                   COUNT(DISTINCT o.ID) AS n_orders,
                   MIN(o.Created) AS first_buy, MAX(o.Created) AS last_buy
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
            JOIN dbo.Client c ON c.ID = ca.ClientID
            WHERE oi.IsValidForCurrentSale = 1 AND o.Created < :asof AND oi.ProductID IS NOT NULL
                  AND c.Deleted = 0
            GROUP BY ca.ClientID, oi.ProductID
            HAVING COUNT(DISTINCT o.ID) >= 3
        ),
        cyc AS (
            SELECT client_id, product_id, n_orders,
                   DATEDIFF(day, last_buy, :asof) AS elapsed_days,
                   CASE WHEN DATEDIFF(day, first_buy, last_buy) * 1.0 / NULLIF(n_orders - 1, 0) < :mincyc
                        THEN :mincyc * 1.0
                        ELSE DATEDIFF(day, first_buy, last_buy) * 1.0 / NULLIF(n_orders - 1, 0)
                   END AS cycle_days
            FROM per_product
            WHERE DATEDIFF(day, first_buy, last_buy) > 0
        )
        SELECT client_id, product_id, n_orders, cycle_days, elapsed_days
        FROM cyc
        WHERE elapsed_days >= cycle_days AND elapsed_days <= cycle_days * :maxmult
        """,
        {"asof": asof, "mincyc": s.reorder_min_cycle_days, "maxmult": s.reorder_max_overdue_mult},
    )
    excl = _excluded_pids()
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
    s = get_settings()
    rows = query(
        """
        SELECT c.ID AS client_id,
               SUM(dbo.GetExchangedToEuroValue(d.Total, ISNULL(a.CurrencyID, 2), d.Created))
                   AS overdue_amount,
               MAX(DATEDIFF(day, d.Created, :asof)) AS max_overdue_days,
               MAX(DATEDIFF(day, d.Created, :asof) - ISNULL(a.NumberDaysDebt, 0)) AS max_days_past_terms,
               COUNT(*) AS debt_lines
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID AND d.Deleted = 0
        JOIN dbo.Client c ON c.ID = cid.ClientID AND c.Deleted = 0
        LEFT JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        WHERE cid.Deleted = 0 AND d.Total > 0
              AND DATEDIFF(day, d.Created, :asof) > ISNULL(a.NumberDaysDebt, 0)
              AND DATEDIFF(day, d.Created, :asof) <= :maxage
        GROUP BY c.ID
        HAVING SUM(dbo.GetExchangedToEuroValue(d.Total, ISNULL(a.CurrencyID, 2), d.Created)) >= :minamt
        """,
        {"asof": asof, "maxage": s.debt_max_age_days, "minamt": s.debt_min_amount},
    )
    return [{
        "client_id": int(r["client_id"]),
        "overdue_amount": float(r["overdue_amount"] or 0.0),
        "max_overdue_days": int(r["max_overdue_days"] or 0),
        "days_past_terms": int(r["max_days_past_terms"] or 0),
        "debt_lines": int(r["debt_lines"] or 0),
    } for r in rows]


def churn_candidates(asof: str, recent_days: int = 90, baseline_days: int = 365) -> list[dict]:
    rows = query(
        """
        WITH client_orders AS (
            SELECT DISTINCT ca.ClientID AS client_id, o.ID AS order_id, o.Created AS dt
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.IsValidForCurrentSale = 1
            JOIN dbo.Client c ON c.ID = ca.ClientID
            WHERE o.Created < :asof AND c.Deleted = 0
        ),
        agg AS (
            SELECT client_id,
                   SUM(CASE WHEN dt >= DATEADD(day, -:recent, :asof) THEN 1 ELSE 0 END) AS recent_orders,
                   SUM(CASE WHEN dt >= DATEADD(day, -:base, :asof)
                            AND dt < DATEADD(day, -:recent, :asof) THEN 1 ELSE 0 END) AS prior_orders,
                   MAX(dt) AS last_order
            FROM client_orders GROUP BY client_id
        )
        SELECT client_id, recent_orders, prior_orders,
               DATEDIFF(day, last_order, :asof) AS silence_days
        FROM agg
        WHERE prior_orders >= 2
              AND (recent_orders * 1.0 / :recent) < 0.5 * (prior_orders * 1.0 / (:base - :recent))
        """,
        {"asof": asof, "recent": recent_days, "base": baseline_days},
    )
    out = []
    for r in rows:
        recent = int(r["recent_orders"] or 0)
        prior = int(r["prior_orders"] or 0)
        out.append({
            "client_id": int(r["client_id"]),
            "recent_orders": recent,
            "prior_orders": prior,
            "silence_days": int(r["silence_days"] or 0),
            "drop_ratio": (recent / prior) if prior else 0.0,
        })
    return out


def active_clients(asof: str) -> list[int]:
    """cross_sell pool: clients with >= min_orders distinct orders in last recent_days (manager-free)."""
    s = get_settings()
    rows = query(
        """
        WITH act AS (
            SELECT ca.ClientID AS cid
            FROM dbo.ClientAgreement ca
            JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
                 AND o.Created >= DATEADD(day, -:recent, :asof) AND o.Created < :asof
            JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.IsValidForCurrentSale = 1
            JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0
            GROUP BY ca.ClientID
            HAVING COUNT(DISTINCT o.ID) >= :minord
        )
        SELECT cid FROM act
        """,
        {"asof": asof, "recent": s.cross_sell_recent_days, "minord": s.cross_sell_min_orders},
    )
    return [int(r["cid"]) for r in rows]


_RECO_CACHE_DIR = "data/reco_cache"


def _reco_raw_cached(cid: int, asof: str, reco_timeout: int) -> list[dict]:
    """reco /recommend/copurchase result for (cid, asof), persisted to disk so the backfill is
    idempotent/resumable and the contended live reco service is hit at most once per pair. A timed-out
    or errored call returns [] (graceful, exactly like the live generator) and is NOT cached, so it
    can be retried on a later run."""
    import json
    import os

    os.makedirs(_RECO_CACHE_DIR, exist_ok=True)
    path = os.path.join(_RECO_CACHE_DIR, f"{asof}_{cid}.json")
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    from app.services.generators.cross_sell import _RECO_REQUEST_N
    recs = reco_client.recommend(cid, top_n=_RECO_REQUEST_N, as_of_date=asof,
                                 path="/recommend/copurchase", timeout=reco_timeout)
    if recs:  # only cache non-empty (empty may be a transient timeout — allow retry)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(recs, fh)
        os.replace(tmp, path)
    return recs


def cross_sell_candidates(asof: str, max_workers: int = 32, reco_timeout: int = 8) -> list[dict]:
    """One candidate per active client that reco (as_of T) returns >=1 discovery product for.
    Mirrors the generator: copurchase engine, discovery source, score>=0.05, top-5 kept.
    The discovered product ids are captured so the label can check a real cross-sell buy.

    Reco calls are independent HTTP I/O fanned out over a thread pool and persisted to a disk cache
    (so the backfill is resumable and the contended live reco service is hit at most once per
    (client, as_of)). A short reco_timeout bounds any one slow call; a timed-out client yields no
    cross-sell candidate that run — graceful degradation identical to the live generator's 8s bound.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app.services.generators.cross_sell import _MAX_PRODUCTS, _MIN_SCORE
    if not reco_client.is_healthy():
        return []
    cids = active_clients(asof)
    excl = _excluded_pids()

    def _one(cid: int) -> dict | None:
        recs = _reco_raw_cached(cid, asof, reco_timeout)
        disc = [r for r in recs if r.get("source") == "discovery"
                and float(r.get("score", 0)) >= _MIN_SCORE
                and int(r.get("product_id", 0)) not in excl]
        if not disc:
            return None
        disc = disc[:_MAX_PRODUCTS]
        return {
            "client_id": cid,
            "reco_product_ids": [int(r["product_id"]) for r in disc],
            "top_score": float(disc[0].get("score", 0)),
            "candidates": len(disc),
        }

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return [r for r in ex.map(_one, cids) if r is not None]


# --------------------------------------------------------------------------------------
# Leak-safe outcome labels in (T, T+H].
# --------------------------------------------------------------------------------------
def label_reorder(asof: str, pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    """(client_id, product_id) pairs where the client re-buys THAT product in (T, T+H]."""
    if not pairs:
        return set()
    cids = sorted({c for c, _ in pairs})
    pids = sorted({p for _, p in pairs})
    cph, cparams = in_clause("c", cids)
    pph, pparams = in_clause("p", pids)
    rows = query(
        f"""
        SELECT DISTINCT ca.ClientID AS client_id, oi.ProductID AS product_id
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created > :asof AND o.Created <= :end
              AND ca.ClientID IN {cph} AND oi.ProductID IN {pph}
        """,
        {"asof": asof, "end": _t_plus_h(asof), **cparams, **pparams},
    )
    bought = {(int(r["client_id"]), int(r["product_id"])) for r in rows}
    wanted = set(pairs)
    return bought & wanted


def label_any_order(asof: str, client_ids: list[int]) -> set[int]:
    """clients placing ANY valid order in (T, T+H] — used for churn_winback success."""
    if not client_ids:
        return set()
    ph, params = in_clause("c", client_ids)
    rows = query(
        f"""
        SELECT DISTINCT ca.ClientID AS client_id
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID AND oi.IsValidForCurrentSale = 1
        WHERE o.Created > :asof AND o.Created <= :end AND ca.ClientID IN {ph}
        """,
        {"asof": asof, "end": _t_plus_h(asof), **params},
    )
    return {int(r["client_id"]) for r in rows}


def label_debt_paydown(asof: str, overdue_by_client: dict[int, float]) -> set[int]:
    """clients whose EUR payments in (T, T+H] cover >= DEBT_PAYDOWN_FRACTION of overdue@T."""
    cids = list(overdue_by_client)
    if not cids:
        return set()
    ph, params = in_clause("c", cids)
    rows = query(
        f"""
        SELECT c.ID AS client_id,
               SUM(dbo.GetExchangedToEuroValue(p.Amount, p.CurrencyID, p.FromDate)) AS paid
        FROM dbo.IncomePaymentOrder p
        JOIN dbo.Client c ON c.ID = p.ClientID AND c.Deleted = 0
        WHERE p.Deleted = 0 AND p.FromDate > :asof AND p.FromDate <= :end AND c.ID IN {ph}
        GROUP BY c.ID
        """,
        {"asof": asof, "end": _t_plus_h(asof), **params},
    )
    paid = {int(r["client_id"]): float(r["paid"] or 0.0) for r in rows}
    return {cid for cid, ov in overdue_by_client.items()
            if ov > 0 and paid.get(cid, 0.0) >= DEBT_PAYDOWN_FRACTION * ov}


def label_buy_products(asof: str, client_products: dict[int, list[int]]) -> set[int]:
    """clients who buy ANY of their reco-discovered product ids in (T, T+H] — cross_sell success."""
    cids = list(client_products)
    if not cids:
        return set()
    all_pids = sorted({p for ps in client_products.values() for p in ps})
    cph, cparams = in_clause("c", cids)
    pph, pparams = in_clause("p", all_pids)
    rows = query(
        f"""
        SELECT DISTINCT ca.ClientID AS client_id, oi.ProductID AS product_id
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON o.ClientAgreementID = ca.ID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND o.Created > :asof AND o.Created <= :end
              AND ca.ClientID IN {cph} AND oi.ProductID IN {pph}
        """,
        {"asof": asof, "end": _t_plus_h(asof), **cparams, **pparams},
    )
    bought: dict[int, set[int]] = {}
    for r in rows:
        bought.setdefault(int(r["client_id"]), set()).add(int(r["product_id"]))
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
            "vintage": asof, "task_type": ttype, "client_id": cid,
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
    frames = []
    for t in snapshots:
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
    cid = doc.get("client_id")
    if cid is None:
        return None

    row: dict = {
        "vintage": "live", "task_type": tt, "client_id": int(cid),
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
