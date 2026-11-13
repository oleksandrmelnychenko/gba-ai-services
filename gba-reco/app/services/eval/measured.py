"""Offline accuracy measured for the served model version (written by eval.holdout)."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).with_name("measured.json")


@lru_cache
def load() -> dict:
    return json.loads(_PATH.read_text(encoding="utf-8"))


def precision(kind: str, top_n: int) -> float:
    table = load()["precision"][kind]
    return float(table["10"] if top_n <= 10 else table["25"])


def write(payload: dict) -> None:
    _PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    load.cache_clear()
