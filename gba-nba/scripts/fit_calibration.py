"""Fit the optional probability calibration for the CURRENT propensity artefact.

Input: a parquet/CSV of out-of-time rows with columns task_type, p (probability the model design
showed at the as-of date) and y (0/1 outcome inside the label window). The base model is not
touched. Dry-run prints the fitted maps; --write stores artifacts/calibration.json:
    .venv/bin/python -m scripts.fit_calibration --pairs oot_pairs.parquet [--write]
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from app.ml import calibration


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    frame = pd.read_parquet(args.pairs) if args.pairs.endswith(".parquet") else pd.read_csv(args.pairs)
    sha = calibration.model_sha256()
    if sha is None:
        print("no propensity model artefact")
        return 1
    pairs = {name: (group["p"].astype(float).tolist(), group["y"].astype(int).tolist())
             for name, group in frame.groupby("task_type")
             if len(group) >= calibration.MIN_ROWS and group["y"].nunique() == 2}
    document = calibration.fit_document(pairs, sha)
    print(json.dumps(document, indent=2))
    if args.write:
        target = calibration.ART / calibration.FILE
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document, indent=2))
        temporary.replace(target)
        print(f"written {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
