"""Configured history windows used by product portfolio and stock analytics."""

from __future__ import annotations

from app.core.config import Settings
from app.core.history import (
    HistoryWindow,
    combined_history_metadata,
    day_history_window,
    month_history_window,
)


def portfolio_windows(as_of: str, cfg: Settings) -> dict[str, HistoryWindow]:
    floor = cfg.source_history_start_date
    return {
        "velocity": day_history_window(as_of, cfg.velocity_window_days, floor),
        "dead": day_history_window(as_of, cfg.dead_window_days, floor),
        "returns": day_history_window(as_of, cfg.return_window_days, floor),
        "classification": month_history_window(as_of, cfg.classify_months, floor),
    }


def stock_windows(as_of: str, cfg: Settings) -> dict[str, HistoryWindow]:
    floor = cfg.source_history_start_date
    return {
        "velocity": day_history_window(as_of, cfg.velocity_window_days, floor),
        "dead": day_history_window(as_of, cfg.dead_window_days, floor),
    }


def portfolio_metadata(as_of: str, cfg: Settings) -> dict:
    return combined_history_metadata(portfolio_windows(as_of, cfg))


def stock_metadata(as_of: str, cfg: Settings) -> dict:
    return combined_history_metadata(stock_windows(as_of, cfg))
