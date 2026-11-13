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
    return producer_lead_time_evidence(producer_id, as_of)[:3]


def producer_lead_time_evidence(producer_id: int, as_of: str) -> tuple[float, float, str, int]:
    s = get_settings()
    samples = repo.producer_lead_times(
        producer_id, as_of, s.lead_time_min_days, s.lead_time_max_days
    )
    currency = (
        repo.producer_agreement_currency(producer_id, as_of)
        if len(samples) < s.lead_time_min_samples
        else None
    )
    return (*lead_time_from(samples, currency), len(samples))


EMPIRICAL = "empirical"
EMPIRICAL_TRIMMED = "empirical_trimmed"


def basis_of(source: str) -> str:
    if source in (EMPIRICAL, EMPIRICAL_TRIMMED):
        return "measured"
    return "override" if source == "override" else "default"


def payload_source(source: str) -> str:
    return EMPIRICAL if source == EMPIRICAL_TRIMMED else source


def statistic_of(source: str) -> str | None:
    if source == EMPIRICAL:
        return "mean"
    return "trimmed_mean" if source == EMPIRICAL_TRIMMED else None


def trimmed_mean(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    cut = int(len(ordered) * fraction)
    kept = ordered[cut : len(ordered) - cut] or ordered
    return sum(kept) / len(kept)


def lead_time_from(samples: list[float], currency_id: int | None) -> tuple[float, float, str]:
    """Measured transit plus the order-processing leg, or the currency default.

    A producer whose receipts scatter more than lead_time_robust_cv around their mean is
    measured by the trimmed mean, and its spread is capped at the default coefficient of
    variation: a noisy measurement carries no more uncertainty than no measurement.
    """
    s = get_settings()
    if len(samples) < s.lead_time_min_samples:
        geo = _LEAD_DAYS_BY_CURRENCY.get(currency_id)
        if geo is not None:
            return geo, geo * s.lead_time_cv, "geo"
        mean = float(s.default_lead_time_days)
        return mean, mean * s.lead_time_cv, "default"
    processing = float(s.lead_time_order_processing_days)
    transit = sum(samples) / len(samples)
    std = math.sqrt(sum((x - transit) ** 2 for x in samples) / len(samples))
    if transit > 0 and std / transit > s.lead_time_robust_cv:
        mean = trimmed_mean(samples, s.lead_time_trim_fraction) + processing
        spread = min(std, mean * s.lead_time_cv) if s.lead_time_cap_noisy_spread else std
        return mean, max(spread, mean * 0.1), EMPIRICAL_TRIMMED
    mean = transit + processing
    return mean, max(std, mean * 0.1), EMPIRICAL
