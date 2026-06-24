"""Per-producer lead-time model from plausible SupplyOrder history, with a default fallback."""
from __future__ import annotations

import math

from app.core.config import get_settings
from app.data import supply_repository as repo

# Geography proxy via agreement currency: domestic UAH ships fast, EU/PL longer,
# USD-denominated trade (China/Turkey) longest. Used when real arrival history is
# insufficient (the dominant case — 1C-synced order dates carry no real timing).
_LEAD_DAYS_BY_CURRENCY = {10038: 7.0, 4: 14.0, 2: 18.0, 3: 35.0}


def producer_lead_time(producer_id: int, as_of: str) -> tuple[float, float, str]:
    """Return (mean_days, std_days, source). source is 'empirical', 'geo', or 'default'.

    Empirical only fires with at least lead_time_min_samples plausible samples; otherwise a
    geography-based default (by agreement currency), falling back to the flat config default.
    std is CV-derived so safety stock keeps a lead-time variability term.
    """
    s = get_settings()
    samples = repo.producer_lead_times(
        producer_id, as_of, s.lead_time_min_days, s.lead_time_max_days
    )
    if len(samples) < s.lead_time_min_samples:
        ccy = repo.producer_agreement_currency(producer_id)
        geo = _LEAD_DAYS_BY_CURRENCY.get(ccy)
        if geo is not None:
            return geo, geo * s.lead_time_cv, "geo"
        mean = float(s.default_lead_time_days)
        return mean, mean * s.lead_time_cv, "default"
    mean = sum(samples) / len(samples)
    var = sum((x - mean) ** 2 for x in samples) / len(samples)
    std = math.sqrt(var)
    return mean, max(std, mean * 0.1), "empirical"
