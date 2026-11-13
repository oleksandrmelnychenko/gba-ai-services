"""Build the creditscore-v4 payment-behaviour index card from a monthly client panel.

Target: a buyer that is not delinquent at month-end T reaches at least MATERIAL_EUR overdue
by 30+ days within the next 91 days. Receivables history is rebuilt by FIFO settlement of
sales against payments and returns; the same settlement code serves live scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.risk import dataset as risk_dataset  # noqa: E402
from app.risk import payment_behaviour as pb  # noqa: E402
from app.risk.score_index import (  # noqa: E402
    INDEX_MODEL_NAME,
    INDEX_MODEL_VERSION,
    MATERIAL_DEBT_EUR,
    validate_card,
)

ART = ROOT / "app" / "risk" / "artifacts"
CARD_PATH = ART / "index_card.json"

EVENT_DPD = 30
SEVERE_DPD = 90
MATERIAL_EUR = 100.0
HORIZON_DAYS = 91
FIRST_MONTH = "2025-04-30"
PRIOR_ROWS = 30.0
BAND_FLOORS = {"A": 80, "B": 60, "C": 30, "D": 0}
MIN_EVENT_CLIENTS = 30
MIN_OOT_AUC = 0.85
MIN_OOT_AUC_LOWER = 0.80
BOOTSTRAPS = 400
SEED = 20261112

COMPONENTS: dict[str, tuple[list[float], int]] = {
    "overdue_now_eur": ([0, 20, 100, 500], 1),
    "oldest_overdue_days": ([0, 7, 14, 29], 1),
    "worst_delay_12m_days": ([7, 14, 30, 60], 1),
    "share_late_invoices_12m": ([0.0, 0.2, 0.5], 1),
    "delay_trend_3m_days": ([-3, 3, 10], 1),
    "debt_to_monthly_turnover": ([0.0, 0.5, 1.0, 2.0], 1),
    "limit_utilization": ([0.0, 0.5, 1.0], 1),
}
HISTORY_COMPONENTS = ["worst_delay_12m_days", "share_late_invoices_12m", "delay_trend_3m_days"]


def _rows_by_client(frame: pd.DataFrame, date_cols: tuple[str, ...]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {}
    for row in frame.to_dict("records"):
        for col in date_cols:
            row[col] = pd.Timestamp(row[col]).date()
        out.setdefault(int(row.pop("client_id")), []).append(row)
    return out


def load_frames(as_of: str, cache_dir: Path | None):
    cached = cache_dir is not None and (cache_dir / "ledger_invoices.parquet").exists()
    if cached:
        inv = pd.read_parquet(cache_dir / "ledger_invoices.parquet")
        cre = pd.read_parquet(cache_dir / "ledger_credits.parquet")
        limits_frame = pd.read_parquet(cache_dir / "credit_limits.parquet")
        invoices = _rows_by_client(inv, ("date", "due"))
        credits = _rows_by_client(cre, ("date",))
    else:
        buyers = risk_dataset.buyer_ids()
        invoices, credits = pb.load_ledger(buyers, as_of)
        inv = pd.DataFrame(
            [{"client_id": cid, **row} for cid, rows in invoices.items() for row in rows]
        )
        cre = pd.DataFrame(
            [{"client_id": cid, **row} for cid, rows in credits.items() for row in rows]
        )
        terms = risk_dataset.feat_credit_terms(as_of)
        limits_frame = terms[["client_id", "credit_limit_eur"]]
    for frame, cols in ((inv, ("date", "due")), (cre, ("date",))):
        for col in cols:
            frame[col] = pd.to_datetime(frame[col])
    if cache_dir is not None and not cached:
        cache_dir.mkdir(parents=True, exist_ok=True)
        inv.to_parquet(cache_dir / "ledger_invoices.parquet")
        cre.to_parquet(cache_dir / "ledger_credits.parquet")
        limits_frame.to_parquet(cache_dir / "credit_limits.parquet")
    cutoff = pd.Timestamp(as_of)
    inv = inv[(inv.amount > 0) & (inv.date <= cutoff)].reset_index(drop=True)
    cre = cre[(cre.eur > 0) & (cre.date <= cutoff)]
    limits = dict(
        zip(limits_frame.client_id.astype(int), limits_frame.credit_limit_eur.astype(float), strict=True)
    )
    return inv, cre, invoices, credits, limits, ("cache" if cached else "database")


def fifo_segments(inv: pd.DataFrame, cre: pd.DataFrame) -> pd.DataFrame:
    credit_groups = {
        cid: (g.date.to_numpy(), g.eur.to_numpy(dtype=float))
        for cid, g in cre.sort_values(["client_id", "date"]).groupby("client_id")
    }
    segments = []
    ordered = inv.sort_values(["client_id", "date", "sale_id"])
    for cid, g in ordered.groupby("client_id"):
        dates, amounts = credit_groups.get(cid, (np.array([], dtype="datetime64[ns]"), np.array([])))
        amounts = amounts.copy()
        j = 0
        for sale_id, day, amount in zip(
            g.sale_id.to_numpy(), g.date.to_numpy(), g.amount.to_numpy(), strict=True
        ):
            remaining = amount
            while remaining > 1e-9 and j < len(amounts):
                use = min(remaining, amounts[j])
                segments.append((sale_id, max(dates[j], day), use))
                remaining -= use
                amounts[j] -= use
                if amounts[j] <= 1e-9:
                    j += 1
    return pd.DataFrame(segments, columns=["sale_id", "paid_on", "paid"])


def add_settlement(inv: pd.DataFrame, seg: pd.DataFrame) -> pd.DataFrame:
    seg = seg.sort_values(["sale_id", "paid_on"]).copy()
    seg["cum"] = seg.groupby("sale_id").paid.cumsum()
    seg = seg.merge(inv[["sale_id", "amount"]], on="sale_id")
    tolerance = np.maximum(pb.SETTLE_TOLERANCE_EUR, pb.SETTLE_TOLERANCE_SHARE * seg.amount)
    settled = seg[seg.cum >= seg.amount - tolerance].groupby("sale_id").paid_on.min()
    out = inv.copy()
    out["settled"] = out.sale_id.map(settled)
    tiny = out.settled.isna() & (out.amount <= pb.SETTLE_TOLERANCE_EUR)
    out.loc[tiny, "settled"] = out.loc[tiny, "date"]
    return out


def open_state(inv: pd.DataFrame, seg: pd.DataFrame, when: pd.Timestamp) -> pd.DataFrame:
    known = inv[(inv.date <= when) & (~inv.legacy)]
    paid = seg[seg.paid_on <= when].groupby("sale_id").paid.sum()
    opened = (known.amount - known.sale_id.map(paid).fillna(0.0)).clip(lower=0.0)
    state = known.assign(open=opened)
    state = state[state.open > 0.005].copy()
    state["od"] = (when - state.due).dt.days
    grouped = state.groupby("client_id")
    out = pd.DataFrame({"open_eur": grouped.open.sum()})
    out["overdue_now_eur"] = state[state.od >= 1].groupby("client_id").open.sum()
    out["od_event"] = state[state.od >= EVENT_DPD].groupby("client_id").open.sum()
    out["od_severe"] = state[state.od >= SEVERE_DPD].groupby("client_id").open.sum()
    out["oldest_overdue_days"] = (
        state[state.open >= pb.MATERIAL_LINE_EUR].groupby("client_id").od.max().clip(lower=0)
    )
    return out.fillna(0.0)


def behaviour_frame(inv: pd.DataFrame, when: pd.Timestamp) -> pd.DataFrame:
    known = inv[(inv.date <= when) & (~inv.legacy) & (inv.amount >= pb.MIN_INVOICE_EUR)].copy()
    known = known[known.due <= when]
    end = known.settled.where(known.settled <= when, when)
    known["delay"] = (end - known.due).dt.days.clip(lower=0)
    year = known[known.due > when - pd.Timedelta(days=365)]
    recent = known[known.due > when - pd.Timedelta(days=91)]
    previous = known[
        (known.due <= when - pd.Timedelta(days=91)) & (known.due > when - pd.Timedelta(days=182))
    ]
    out = pd.DataFrame({"worst_delay_12m_days": year.groupby("client_id").delay.max()})
    out["share_late_invoices_12m"] = (
        year.assign(late=year.delay > pb.LATE_DAYS).groupby("client_id").late.mean()
    )
    out["matured_invoices_12m"] = year.groupby("client_id").delay.size()
    trend = pd.DataFrame(
        {
            "recent": recent.groupby("client_id").delay.mean(),
            "previous": previous.groupby("client_id").delay.mean(),
        }
    ).fillna(0.0)
    out = out.join(
        (trend.recent - trend.previous).clip(-pb.TREND_CLIP_DAYS, pb.TREND_CLIP_DAYS).rename(
            "delay_trend_3m_days"
        ),
        how="outer",
    )
    return out.fillna(0.0)


def build_panel(inv: pd.DataFrame, seg: pd.DataFrame, limits: dict[int, float], end: pd.Timestamp):
    grid = pd.date_range("2025-01-05", end, freq="7D")
    weekly = {when: open_state(inv, seg, when) for when in grid}
    month_ends = pd.date_range(FIRST_MONTH, end, freq="ME")
    snapshots = sorted({grid[grid <= m].max() for m in month_ends})
    sales = inv[~inv.legacy]
    rows = []
    for when in snapshots:
        future = [g for g in grid if when < g <= when + pd.Timedelta(days=HORIZON_DAYS)]
        if len(future) < 13:
            continue
        window = sales[(sales.date <= when) & (sales.date > when - pd.Timedelta(days=365))]
        frame = pd.DataFrame({"turnover_12m": window.groupby("client_id").amount.sum()})
        frame["orders_12m"] = window.groupby("client_id").amount.size()
        frame = frame.join(weekly[when], how="outer").join(behaviour_frame(inv, when), how="outer")
        worst = pd.concat([weekly[g][["od_event"]] for g in future]).groupby(level=0).od_event.max()
        frame["future_event_eur"] = worst
        frame = frame.fillna(0.0)
        frame["T"] = when
        rows.append(frame.rename_axis("client_id").reset_index())
    panel = pd.concat(rows, ignore_index=True)
    turnover = panel.turnover_12m.to_numpy()
    debt = panel.open_eur.to_numpy()
    debt = np.where(debt >= MATERIAL_DEBT_EUR, debt, 0.0)
    panel["debt_to_monthly_turnover"] = np.where(
        turnover > 0, np.minimum(debt / np.maximum(turnover, 1e-9) * 12.0, 12.0),
        np.where(debt > 0, 12.0, 0.0),
    )
    limit = panel.client_id.map(limits).fillna(0.0).to_numpy()
    panel["limit_utilization"] = np.where(limit > 0, np.minimum(debt / np.maximum(limit, 1e-9), 3.0), 0.0)
    panel["y"] = (panel.future_event_eur >= MATERIAL_EUR).astype(int)
    active = (panel.orders_12m > 0) | (panel.open_eur > 0)
    return panel[active & (panel.od_event < MATERIAL_EUR)].reset_index(drop=True), weekly, snapshots


def bins_of(frame: pd.DataFrame, name: str) -> np.ndarray:
    edges = np.asarray(COMPONENTS[name][0], dtype=float)
    idx = np.searchsorted(edges, frame[name].to_numpy(dtype=float), side="left")
    if name in HISTORY_COMPONENTS:
        idx = np.where(frame.matured_invoices_12m.to_numpy() > 0, idx, -1)
    return idx


def fit_components(train: pd.DataFrame) -> tuple[float, dict]:
    base = float(train.y.mean())
    base_logodds = np.log(base / (1 - base))
    y = train.y.to_numpy()
    components = {}
    for name, (edges, direction) in COMPONENTS.items():
        idx = bins_of(train, name)
        n = np.array([(idx == k).sum() for k in range(len(edges) + 1)])
        events = np.array([y[idx == k].sum() for k in range(len(edges) + 1)])
        rate = (events + PRIOR_ROWS * base) / (n + PRIOR_ROWS)
        positions = np.arange(len(rate))
        rate = IsotonicRegression(increasing=direction > 0).fit(
            positions, rate, sample_weight=n + PRIOR_ROWS
        ).predict(positions)
        components[name] = {
            "edges": [float(e) for e in edges],
            "logodds": [round(float(np.log(r / (1 - r)) - base_logodds), 4) for r in rate],
            "rows": n.tolist(),
            "events": events.tolist(),
        }
    return base, components


def linear(components: dict, frame: pd.DataFrame) -> np.ndarray:
    z = np.zeros(len(frame))
    for name, comp in components.items():
        idx = bins_of(frame, name)
        points = np.asarray(comp["logodds"])
        z += np.where(idx >= 0, points[np.clip(idx, 0, len(points) - 1)], 0.0)
    return z


def scale_of(components: dict) -> dict[str, float]:
    return {
        "z_min": round(sum(min(c["logodds"]) for c in components.values()), 4),
        "z_max": round(sum(max(c["logodds"]) for c in components.values()), 4),
    }


def to_score(z: np.ndarray, scale: dict[str, float]) -> np.ndarray:
    raw = 100.0 * (scale["z_max"] - z) / (scale["z_max"] - scale["z_min"])
    return np.clip(np.rint(raw), 0, 100)


def to_band(score: np.ndarray) -> np.ndarray:
    return np.where(
        score >= BAND_FLOORS["A"], "A",
        np.where(score >= BAND_FLOORS["B"], "B", np.where(score >= BAND_FLOORS["C"], "C", "D")),
    )


def cluster_weights(client_ids: np.ndarray, rng: np.random.Generator):
    ids, inverse = np.unique(client_ids, return_inverse=True)
    for _ in range(BOOTSTRAPS):
        yield rng.multinomial(len(ids), np.full(len(ids), 1.0 / len(ids)))[inverse]


def fold_auc(frame: pd.DataFrame, column: str, weights: np.ndarray | None = None) -> float:
    values, events = [], []
    for fold in sorted(frame.fold.unique()):
        mask = (frame.fold == fold).to_numpy()
        y = frame.y.to_numpy()[mask]
        w = None if weights is None else weights[mask]
        if w is not None:
            keep = w > 0
            y, w, x = y[keep], w[keep], frame[column].to_numpy()[mask][keep]
        else:
            x = frame[column].to_numpy()[mask]
        if 0 < y.sum() < len(y):
            values.append(roc_auc_score(y, x, sample_weight=w))
            events.append(y.sum() if w is None else (y * w).sum())
    return float(np.average(values, weights=events)) if values else float("nan")


def temporal_validation(panel: pd.DataFrame, snapshots: list, rng: np.random.Generator) -> dict:
    months = sorted(panel["T"].unique())
    thirds = np.array_split(months[len(months) * 3 // 8:], 3)
    parts = []
    folds = []
    for k, test_months in enumerate(thirds):
        cut = pd.Timestamp(test_months[0]) - pd.Timedelta(days=HORIZON_DAYS)
        train = panel[panel["T"] <= cut]
        test = panel[panel["T"].isin(test_months)].copy()
        _, components = fit_components(train)
        test["z"] = linear(components, test)
        test["fold"] = k
        parts.append(test)
        folds.append(
            {
                "train_until": str(cut.date()),
                "train_rows": int(len(train)),
                "train_event_clients": int(train[train.y == 1].client_id.nunique()),
                "test_from": str(pd.Timestamp(test_months[0]).date()),
                "test_to": str(pd.Timestamp(test_months[-1]).date()),
                "test_rows": int(len(test)),
                "test_events": int(test.y.sum()),
                "test_event_clients": int(test[test.y == 1].client_id.nunique()),
                "auc": round(float(roc_auc_score(test.y, test.z)), 4),
            }
        )
    oot = pd.concat(parts, ignore_index=True)
    boots = [fold_auc(oot, "z", w) for w in cluster_weights(oot.client_id.to_numpy(), rng)]
    with_debt = oot[oot.open_eur > 0].reset_index(drop=True)
    boots_debt = [
        fold_auc(with_debt, "z", w) for w in cluster_weights(with_debt.client_id.to_numpy(), rng)
    ]
    return {
        "design": "expanding window, 91-day purge, three out-of-time blocks",
        "folds": folds,
        "oot_rows": int(len(oot)),
        "oot_events": int(oot.y.sum()),
        "oot_event_clients": int(oot[oot.y == 1].client_id.nunique()),
        "oot_auc": round(fold_auc(oot, "z"), 4),
        "oot_auc_ci95": [round(float(np.nanpercentile(boots, q)), 4) for q in (2.5, 97.5)],
        "oot_auc_open_debt": round(fold_auc(with_debt, "z"), 4),
        "oot_auc_open_debt_ci95": [
            round(float(np.nanpercentile(boots_debt, q)), 4) for q in (2.5, 97.5)
        ],
        "oot_frame": oot,
    }


def band_table(frame: pd.DataFrame, score: np.ndarray, rng: np.random.Generator) -> dict:
    band = to_band(score)
    y = frame.y.to_numpy()
    samples: dict[str, list[float]] = {b: [] for b in BAND_FLOORS}
    for w in cluster_weights(frame.client_id.to_numpy(), rng):
        for b in BAND_FLOORS:
            mask = band == b
            total = w[mask].sum()
            samples[b].append((w[mask] * y[mask]).sum() / total if total > 0 else np.nan)
    out = {}
    for b in BAND_FLOORS:
        mask = band == b
        out[b] = {
            "rows": int(mask.sum()),
            "share": round(float(mask.mean()), 4),
            "clients": int(frame.client_id[mask].nunique()),
            "events": int(y[mask].sum()),
            "rate": round(float(y[mask].mean()), 5) if mask.any() else None,
            "rate_ci95": [round(float(np.nanpercentile(samples[b], q)), 5) for q in (2.5, 97.5)],
        }
    return out


def persistence(weekly: dict, snapshots: list, column: str, exclude: str | None) -> dict:
    hit = total = 0
    clients: set[int] = set()
    for when in snapshots:
        later = when + pd.Timedelta(days=HORIZON_DAYS)
        if later not in weekly:
            continue
        now = weekly[when]
        mask = now[column] >= MATERIAL_EUR
        if exclude is not None:
            mask &= now[exclude] < MATERIAL_EUR
        ids = now.index[mask]
        after = weekly[later].od_severe.reindex(ids).fillna(0.0)
        hit += int((after >= MATERIAL_EUR).sum())
        total += len(ids)
        clients.update(int(i) for i in ids)
    return {"client_months": total, "clients": len(clients), "severe_after_91d": hit,
            "share": round(hit / total, 4) if total else None}


def parity_check(inv: pd.DataFrame, invoices: dict, credits: dict, snapshots: list, rng) -> int:
    checked = 0
    for when in rng.choice(np.array(snapshots, dtype="datetime64[ns]"), 4, replace=False):
        when = pd.Timestamp(when)
        frame = behaviour_frame(inv, when)
        for cid in rng.choice(frame.index.to_numpy(), min(60, len(frame)), replace=False):
            served = pb.behaviour_features(invoices[int(cid)], credits.get(int(cid), []), when.date())
            for key in ("worst_delay_12m_days", "share_late_invoices_12m", "delay_trend_3m_days",
                        "matured_invoices_12m"):
                if abs(served[key] - float(frame.loc[cid, key])) > 1e-6:
                    raise SystemExit(
                        f"serving/training parity failed: client {cid} at {when.date()} {key}: "
                        f"{served[key]} vs {frame.loc[cid, key]}"
                    )
            checked += 1
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", default=date.today().isoformat())
    parser.add_argument("--evaluate-only", action="store_true")
    parser.add_argument("--panel-out", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    rng = np.random.default_rng(SEED)
    end = pd.Timestamp(args.as_of)

    inv, cre, invoices, credits, limits, source = load_frames(args.as_of, args.cache_dir)
    seg = fifo_segments(inv, cre)
    inv = add_settlement(inv, seg)
    panel, weekly, snapshots = build_panel(inv, seg, limits, end)
    checked = parity_check(inv, invoices, credits, snapshots, rng)
    event_clients = int(panel[panel.y == 1].client_id.nunique())
    print(f"panel: months={panel['T'].nunique()} rows={len(panel)} clients={panel.client_id.nunique()} "
          f"events={int(panel.y.sum())} event_clients={event_clients} parity_checked={checked}")

    validation = temporal_validation(panel, snapshots, rng)
    oot = validation.pop("oot_frame")
    base, components = fit_components(panel)
    scale = scale_of(components)
    bands_in = band_table(panel, to_score(linear(components, panel), scale), rng)
    bands_oot = band_table(oot, to_score(oot.z.to_numpy(), scale), rng)
    rates = [bands_oot[b]["rate"] for b in BAND_FLOORS]
    severe = persistence(weekly, snapshots, "od_severe", None)
    delinquent = persistence(weekly, snapshots, "od_event", "od_severe")
    gates = {
        "min_event_clients": MIN_EVENT_CLIENTS,
        "event_clients": event_clients,
        "min_oot_auc": MIN_OOT_AUC,
        "min_oot_auc_ci_lower": MIN_OOT_AUC_LOWER,
        "bands_monotone_out_of_time": all(a <= b for a, b in zip(rates, rates[1:], strict=False)),
    }
    gates["passed"] = bool(
        event_clients >= MIN_EVENT_CLIENTS
        and validation["oot_auc"] >= MIN_OOT_AUC
        and validation["oot_auc_ci95"][0] >= MIN_OOT_AUC_LOWER
        and gates["bands_monotone_out_of_time"]
    )
    print(json.dumps({"validation": validation, "gates": gates, "bands_oot": bands_oot,
                      "bands_in_sample": bands_in, "severe": severe, "delinquent": delinquent},
                     indent=1, default=str))
    if args.panel_out is not None:
        panel.to_parquet(args.panel_out)
    if not gates["passed"]:
        print("GATE FAILED: index card not written")
        return 1
    if args.evaluate_only:
        print("EVALUATE-ONLY: index card not written")
        return 0

    floors = list(BAND_FLOORS.items())
    bands = {}
    for i, (name, floor) in enumerate(floors):
        observed = bands_oot[name]
        bands[name] = {
            "min_score": floor,
            "max_score": 100 if i == 0 else floors[i - 1][1] - 1,
            "pd": observed["rate"],
            "pd_ci95": observed["rate_ci95"],
            "out_of_time": observed,
            "in_sample": bands_in[name],
        }
    trained_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    payload = {
        "model": INDEX_MODEL_NAME,
        "model_version": INDEX_MODEL_VERSION,
        "trained_at": trained_at,
        "target": {
            "event": f"at least EUR {MATERIAL_EUR:.0f} overdue by {EVENT_DPD}+ days",
            "horizon_days": HORIZON_DAYS,
            "population": "buyers with sales in 12 months or open debt, not in the event state",
            "pd_meaning": "observed out-of-time event share of the band, not a per-client forecast",
        },
        "panel": {
            "months": int(panel["T"].nunique()),
            "first_month": str(pd.Timestamp(panel["T"].min()).date()),
            "last_month": str(pd.Timestamp(panel["T"].max()).date()),
            "rows": int(len(panel)),
            "clients": int(panel.client_id.nunique()),
            "events": int(panel.y.sum()),
            "event_clients": event_clients,
            "base_rate": round(base, 5),
            "as_of": args.as_of,
            "source": source,
        },
        "components": components,
        "history_components": HISTORY_COMPONENTS,
        "material_debt_eur": MATERIAL_DEBT_EUR,
        "scale": scale,
        "bands": bands,
        "policy": {
            "severe_min_eur": MATERIAL_EUR,
            "severe_days": SEVERE_DPD,
            "severe_pd": severe["share"],
            "severe_evidence": severe,
            "delinquent_min_eur": MATERIAL_EUR,
            "delinquent_days": EVENT_DPD,
            "delinquent_pd": delinquent["share"],
            "delinquent_evidence": delinquent,
        },
        "validation": validation,
        "gates": gates,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    payload["run_id"] = f"index-{trained_at.replace(':', '').replace('-', '')}-{digest[:12]}"
    validate_card(payload)
    if CARD_PATH.exists():
        backup = ART / f"backup_index_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
        backup.mkdir(parents=True)
        shutil.copy2(CARD_PATH, backup / CARD_PATH.name)
    tmp = CARD_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False) + "\n")
    tmp.replace(CARD_PATH)
    print(f"index card written: {CARD_PATH} run_id={payload['run_id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
