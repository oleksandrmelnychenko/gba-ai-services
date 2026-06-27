"""Demand-pattern classification — the Syntetos-Boylan (2005) quadrant.

A monthly EUR sale series is classified by two statistics computed over the dense
(zero-filled) window:

  ADI  = average inter-demand interval = (# months) / (# non-zero months).
         How often a sale actually happens. ADI = 1 means every month has a sale;
         large ADI means long droughts between sales.
  CV2  = squared coefficient of variation of the NON-ZERO sale sizes
         = (std(sizes) / mean(sizes)) ** 2. How erratic the size of a sale is when
         one happens.

The Syntetos-Boylan-Croston cut-offs are the textbook constants (ADI = 1.32,
CV2 = 0.49); they partition series into four quadrants:

  SMOOTH        ADI < 1.32 and CV2 < 0.49  — regular cadence, stable size.
  ERRATIC       ADI < 1.32 and CV2 >= 0.49 — regular cadence, volatile size.
  INTERMITTENT  ADI >= 1.32 and CV2 < 0.49 — sporadic, but stable size when it happens.
  LUMPY         ADI >= 1.32 and CV2 >= 0.49 — sporadic AND volatile (the hard case).

The classification is used by the method selector (selection.py) to pick the
forecaster best suited to each pattern, and by the backtest to aggregate accuracy
per segment.
"""

from __future__ import annotations

# Syntetos-Boylan-Croston quadrant cut-offs (the standard literature constants).
ADI_CUTOFF = 1.32
CV2_CUTOFF = 0.49

SMOOTH = "smooth"
ERRATIC = "erratic"
INTERMITTENT = "intermittent"
LUMPY = "lumpy"
# A series with no demand at all — distinct so the selector can default safely.
NO_DEMAND = "no_demand"


def _active_span(series: list[float]) -> list[float]:
    """Trim LEADING zeros (the pre-history region before a series' first-ever sale).

    A trailing/dense window padded back further than the data exists (e.g. a 24-month window
    over a relationship that only started 18 months ago) carries structural leading zeros that
    are NOT demand droughts — they are "the client/product did not exist yet". Counting them as
    inter-demand interval wrongly inflates ADI and mislabels an otherwise-smooth series as
    intermittent. Cadence is therefore measured from the first real sale onward. Interior and
    trailing zeros (genuine droughts / recent silence) are kept.
    """
    first = next((i for i, v in enumerate(series) if v > 0), None)
    return series[first:] if first is not None else []


def adi(series: list[float]) -> float:
    """Average inter-demand interval = active-span length / number of non-zero months.

    Returns +inf for an all-zero series (no demand events). The span is measured from the
    first real sale onward (leading pre-history zeros trimmed) so structural padding does not
    masquerade as sparsity; interior zero months still count as droughts.
    """
    span = _active_span(series)
    events = sum(1 for v in span if v > 0)
    if events == 0:
        return float("inf")
    return len(span) / events


def cv2(series: list[float]) -> float:
    """Squared coefficient of variation of the NON-ZERO sale sizes.

    Measures size volatility independent of cadence. Returns 0.0 when there are fewer
    than two demand events (no spread is observable) or the mean size is non-positive.
    """
    sizes = [v for v in series if v > 0]
    if len(sizes) < 2:
        return 0.0
    mean = sum(sizes) / len(sizes)
    if mean <= 0:
        return 0.0
    var = sum((x - mean) ** 2 for x in sizes) / len(sizes)
    std = var ** 0.5
    return (std / mean) ** 2


def classify(series: list[float]) -> str:
    """Map a dense monthly series to its Syntetos-Boylan quadrant label.

    Returns one of SMOOTH / ERRATIC / INTERMITTENT / LUMPY, or NO_DEMAND when the series
    has no non-zero months. The cadence axis uses ADI; the volatility axis uses CV2.
    """
    a = adi(series)
    if a == float("inf"):
        return NO_DEMAND
    c = cv2(series)
    intermittent_cadence = a >= ADI_CUTOFF
    volatile_size = c >= CV2_CUTOFF
    if not intermittent_cadence and not volatile_size:
        return SMOOTH
    if not intermittent_cadence and volatile_size:
        return ERRATIC
    if intermittent_cadence and not volatile_size:
        return INTERMITTENT
    return LUMPY
