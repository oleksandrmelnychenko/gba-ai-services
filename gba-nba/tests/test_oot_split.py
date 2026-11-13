"""The temporal OOT split must survive a rolling retrain window.

scripts/retrain.py rebuilds the newest fully-labelled vintages, so the frozen OOT constants in
app/ml/train.py eventually select an empty train fold and the weekly retrain dies inside sklearn
("Found array with 0 sample(s)"). These tests pin the resolver that keeps the historical split
untouched and rolls forward once the frozen window falls out of the data.
"""
import pandas as pd
import pytest

from app.ml.train import OOT_TEST_HI, OOT_TEST_LO, OOT_TRAIN_MAX, resolve_oot_split


def _months(start: str, end: str) -> list[pd.Timestamp]:
    return pd.date_range(start, end, freq="MS").tolist()


def test_historical_dataset_keeps_the_frozen_split():
    assert resolve_oot_split(_months("2025-06-01", "2026-04-01")) == (
        OOT_TRAIN_MAX,
        OOT_TEST_LO,
        OOT_TEST_HI,
    )


def test_rolling_window_falls_back_to_the_newest_vintages():
    train_max, test_lo, test_hi = resolve_oot_split(_months("2026-02-01", "2026-06-01"))

    assert (train_max, test_lo, test_hi) == ("2026-04-01", "2026-05-01", "2026-06-01")


def test_both_folds_are_non_empty_for_every_rolling_window():
    for last_month in range(1, 13):
        end = pd.Timestamp(2026, last_month, 1)
        vintages = pd.date_range(end - pd.DateOffset(months=8), end, freq="MS").tolist()
        train_max, test_lo, test_hi = resolve_oot_split(vintages)

        frame = pd.DataFrame({"vd": vintages})
        train = frame[frame["vd"] <= train_max]
        test = frame[(frame["vd"] >= test_lo) & (frame["vd"] <= test_hi)]

        assert len(train) > 0, f"порожній train для вікна, що завершується {end.date()}"
        assert len(test) > 0, f"порожній test для вікна, що завершується {end.date()}"
        assert train["vd"].max() < test["vd"].min(), "фолди не мають перетинатися в часі"


def test_two_vintages_still_split():
    assert resolve_oot_split(_months("2026-05-01", "2026-06-01")) == (
        "2026-05-01",
        "2026-06-01",
        "2026-06-01",
    )


def test_single_vintage_fails_loudly():
    with pytest.raises(ValueError, match="at least 2 vintages"):
        resolve_oot_split([pd.Timestamp("2026-06-01")])
