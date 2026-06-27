"""Benchmark the new current-state scorecard vs the OLD expert CreditScore-100 (live API).

Pulls the old score for all 3006 buyers via POST /score/batch (chunked, internal key),
computes old-score AUC/Gini vs label_sev180, compares to the new model, and prints where the
two flagged clients (411780 ТРАМП ОЙЛ, 411801 АБРАМЧЕНКО) land under the new scorecard.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pandas as pd
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "app" / "risk" / "artifacts"
DATA = ROOT / "data"
API = "http://127.0.0.1:8003/score/batch"
KEY = os.getenv("SOLVENCY_INTERNAL_API_KEY", "")
CHUNK = 50
TIMEOUT = 60
AS_OF = "2026-06-25"  # the dataset's label as-of date


def _post(chunk: list[int]) -> list[dict]:
    body = json.dumps({"client_ids": chunk, "as_of_date": AS_OF, "use_cache": False}).encode()
    req = urllib.request.Request(
        API, data=body,
        headers={"Content-Type": "application/json", "X-Internal-Api-Key": KEY},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read())
    return data["results"] if isinstance(data, dict) else data


def fetch_old_scores(client_ids: list[int]) -> dict[int, int | None]:
    """Chunked, failure-isolated. A slow/failing chunk is bisected; a failing single client is
    skipped (left out of the benchmark) rather than hanging the whole pull."""
    out: dict[int, int | None] = {}

    def grab(chunk: list[int], depth: int = 0) -> None:
        try:
            for row in _post(chunk):
                out[int(row["client_id"])] = row.get("score")
        except Exception as e:  # noqa: BLE001
            if len(chunk) == 1:
                print(f"  SKIP client {chunk[0]}: {e}")
                return
            mid = len(chunk) // 2
            print(f"  chunk[{len(chunk)}] failed ({e}); bisecting")
            grab(chunk[:mid], depth + 1)
            grab(chunk[mid:], depth + 1)

    for i in range(0, len(client_ids), CHUNK):
        grab(client_ids[i:i + CHUNK])
        print(f"  fetched ~{min(i + CHUNK, len(client_ids))}/{len(client_ids)} (got {len(out)})",
              flush=True)
    return out


def main() -> None:
    scores = pd.read_parquet(DATA / "current_state_scores.parquet")  # client_id, pd, score, band, label
    df = pd.read_parquet(DATA / "risk_dataset_v3.parquet")[["client_id", "label_sev180"]]
    scores = scores.merge(df, on="client_id", how="left")
    cids = scores["client_id"].astype(int).tolist()
    y = scores["label_sev180"].values.astype(int)

    print(f"Pulling OLD expert score for {len(cids)} buyers (as_of={AS_OF})...")
    old = fetch_old_scores(cids)
    scores["old_score"] = scores["client_id"].map(old)

    valid = scores["old_score"].notna()
    n_missing = int((~valid).sum())
    yv = scores.loc[valid, "label_sev180"].values.astype(int)
    old_v = scores.loc[valid, "old_score"].values.astype(float)
    # old score: higher = SAFER, so risk-rank = -old_score
    old_auc = roc_auc_score(yv, -old_v)
    new_auc_score = roc_auc_score(y, -scores["score"].values)   # higher score = safer
    new_auc_pd = roc_auc_score(y, scores["pd"].values)

    # where do the two named clients land?
    named = {411780: "ТРАМП ОЙЛ", 411801: "АБРАМЧЕНКО"}
    placements = []
    for cid, name in named.items():
        r = scores[scores["client_id"] == cid]
        if r.empty:
            placements.append({"client_id": cid, "name": name, "present": False})
            continue
        r = r.iloc[0]
        rank = int((scores["score"] <= r["score"]).sum())  # rank among 3006 (1=lowest score=riskiest)
        placements.append({
            "client_id": cid, "name": name, "present": True,
            "label": int(r["label_sev180"]),
            "new_score": round(float(r["score"]), 2), "new_pd": round(float(r["pd"]), 4),
            "new_band": r["band"],
            "old_score": None if pd.isna(r["old_score"]) else int(r["old_score"]),
            "new_score_pctile": round(100 * rank / len(scores), 1),
        })

    bench = {
        "as_of": AS_OF,
        "n_clients": len(cids),
        "old_score_missing": n_missing,
        "old_expert_score_auc": round(float(old_auc), 4),
        "old_expert_score_gini": round(float(2 * old_auc - 1), 4),
        "new_scorecard_auc": round(float(new_auc_score), 4),
        "new_scorecard_gini": round(float(2 * new_auc_score - 1), 4),
        "new_scorecard_pd_auc": round(float(new_auc_pd), 4),
        "new_beats_old": bool(new_auc_score > old_auc),
        "auc_improvement": round(float(new_auc_score - old_auc), 4),
        "named_clients": placements,
    }
    print("\n== BENCHMARK ==")
    print(json.dumps(bench, indent=2, default=float))

    # append to cv_report
    report_path = ART / "cv_report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    report["benchmark_vs_old"] = bench
    report_path.write_text(json.dumps(report, indent=2, default=float))
    print("\nappended benchmark to", report_path)


if __name__ == "__main__":
    main()
