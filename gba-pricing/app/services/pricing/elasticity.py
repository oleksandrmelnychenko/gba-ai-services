"""Per-SKU own-price elasticity (pure math, no I/O) for the gba-pricing engine.

Estimates a constant-elasticity demand curve from the observational sales panel:

    ln(Q) = a - e*ln(EUR_price) + agreement_FE + month_FE + eps

where, per product:
  Q          = SUM(OrderItem.Qty) by (product x agreement x month)
  EUR_price  = volume-weighted OrderItem.PricePerItem in the same cell (PricePerItem is ALREADY
               EUR for every agreement currency -- do NOT FX-wrap; verified live: a UAH agreement's
               PricePerItem sits in the same 11-30 band as EUR, not ~52x higher).
  e          = own-price elasticity (the reported magnitude; demand falls as price rises so the
               sign on ln(price) is negative and we report e = -beta > 0 for a normal good).

Agreement + month fixed effects (dummy encoding, one level dropped each, absorbed by the intercept)
control the two dominant confounders the discovery flagged: cross-agreement negotiated discount
levels and the secular price drift / seasonality. This is panel FE -- observational, NOT causal
identification -- so estimates are gated to high-data SKUs and surfaced only as a SECONDARY signal.

The data is sparse and contaminated (median basket = 1 unit/line; occasional piece-vs-box UoM mix),
so the fit is hardened:
  * UoM outlier rejection on the cell price by per-product median/MAD modified-z (same Iglewicz-
    Hoaglin k as the peer band) before the regression.
  * Drop fixed-effect levels that appear in only one retained cell (they would be perfectly
    absorbed and add no within-variation while inflating the design rank).
  * Require residual degrees of freedom and genuine within-price variation after de-meaning, else
    return NO estimate (None) rather than a noisy/degenerate number.

ProductGroup-level POOLING: the same estimator runs on the stacked panel of every product in a
group with an added product fixed effect, giving one pooled elasticity that sparser SKUs in the
group borrow. The caller decides per-SKU vs pooled vs none from the cell/line counts.

elasticity-optimal price: for a profit-maximizing monopolistic markup under constant elasticity
e>1, the optimal price is  p* = cost * e/(e-1)  (the standard Lerner/markup rule). For e<=1 the
unconstrained optimum is unbounded, so no elastic price is emitted. This is exposed as an ADDITIONAL
field; the A+B margin-floor+peer-band price stays PRIMARY unless validation proves the elastic
signal wins.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAD_CONSISTENCY = 1.4826


@dataclass
class PanelCell:
    """One (agreement x month) observation for a product: total quantity Q at volume-weighted
    EUR unit price. product_id carries the SKU when cells from several products are stacked for
    the ProductGroup pooled fit (it becomes an extra fixed effect)."""
    agreement_id: int
    month: str
    qty: float
    price: float
    product_id: int | None = None


@dataclass
class ElasticityFit:
    elasticity: float | None
    intercept: float | None
    n_cells: int
    n_kept: int
    r_squared: float | None
    price_var_within: float | None
    pooled: bool
    note: str


def _mad_keep_mask(values: np.ndarray, k: float, min_rows: int) -> np.ndarray:
    """Per-product UoM outlier reject on ln(price): keep |x-median| <= k*1.4826*MAD. Keep-all when
    below min_rows or MAD<=0 (degenerate/all-tied) so a thin product is never over-trimmed -- the
    same robust rule the peer band uses, applied here in log space (cell prices are strictly >0)."""
    n = values.shape[0]
    if n < min_rows:
        return np.ones(n, dtype=bool)
    med = float(np.median(values))
    mad = float(np.median(np.abs(values - med)))
    if mad <= 0.0:
        return np.ones(n, dtype=bool)
    return np.abs(values - med) <= k * MAD_CONSISTENCY * mad


def _dummy_block(labels: list, drop_singletons_against: np.ndarray | None = None) -> np.ndarray:
    """One-hot a categorical with the FIRST level dropped (absorbed by the intercept). Returns an
    (n x (levels-1)) float matrix; empty (n x 0) when there is only one level."""
    uniq: dict = {}
    for lab in labels:
        if lab not in uniq:
            uniq[lab] = len(uniq)
    n = len(labels)
    k = len(uniq)
    if k <= 1:
        return np.zeros((n, 0), dtype=float)
    mat = np.zeros((n, k), dtype=float)
    for i, lab in enumerate(labels):
        mat[i, uniq[lab]] = 1.0
    return mat[:, 1:]


def fit_elasticity(
    cells: list[PanelCell],
    *,
    pooled: bool = False,
    mad_k: float = 3.5,
    mad_min_rows: int = 4,
    min_residual_df: int = 3,
    min_price_levels: int = 3,
    min_price_var: float = 1e-4,
) -> ElasticityFit:
    """OLS of ln(Q) on ln(price) + agreement FE + month FE (+ product FE when pooled).

    Returns ElasticityFit with elasticity = -beta_lnprice (>0 for a normal good) or elasticity=None
    with a note when the panel is too thin/degenerate to fit. No estimate is the honest answer for
    sparse SKUs -- never a fabricated number.
    """
    n_cells = len(cells)
    if n_cells == 0:
        return ElasticityFit(None, None, 0, 0, None, None, pooled, "no-cells")

    price = np.array([c.price for c in cells], dtype=float)
    qty = np.array([c.qty for c in cells], dtype=float)
    valid = (price > 0.0) & (qty > 0.0)
    price, qty = price[valid], qty[valid]
    kept_cells = [c for c, v in zip(cells, valid, strict=True) if v]
    if price.shape[0] == 0:
        return ElasticityFit(None, None, n_cells, 0, None, None, pooled, "no-positive-cells")

    lnp = np.log(price)
    keep = _mad_keep_mask(lnp, mad_k, mad_min_rows)
    lnp = lnp[keep]
    lnq = np.log(qty[keep])
    kept_cells = [c for c, kp in zip(kept_cells, keep, strict=True) if kp]
    n_kept = lnp.shape[0]
    if n_kept == 0:
        return ElasticityFit(None, None, n_cells, 0, None, None, pooled, "all-rejected")

    distinct_prices = np.unique(np.round(lnp, 6)).shape[0]
    if distinct_prices < min_price_levels:
        return ElasticityFit(
            None, None, n_cells, n_kept, None, None, pooled, "insufficient-price-levels"
        )

    agr_labels = [c.agreement_id for c in kept_cells]
    mon_labels = [c.month for c in kept_cells]
    blocks = [np.ones((n_kept, 1), dtype=float), lnp.reshape(-1, 1)]
    blocks.append(_dummy_block(agr_labels))
    blocks.append(_dummy_block(mon_labels))
    if pooled:
        prod_labels = [c.product_id for c in kept_cells]
        blocks.append(_dummy_block(prod_labels))
    x = np.hstack(blocks)

    n, p = x.shape
    resid_df = n - np.linalg.matrix_rank(x)
    if resid_df < min_residual_df:
        return ElasticityFit(
            None, None, n_cells, n_kept, None, None, pooled, "insufficient-residual-df"
        )

    price_var_within = _within_variance(lnp, agr_labels, mon_labels)
    if price_var_within < min_price_var:
        return ElasticityFit(
            None, None, n_cells, n_kept, None, price_var_within, pooled, "no-within-price-variation"
        )

    beta, _res, rank, _sv = np.linalg.lstsq(x, lnq, rcond=None)
    if rank < p:
        return ElasticityFit(
            None, None, n_cells, n_kept, None, price_var_within, pooled, "rank-deficient"
        )

    fitted = x @ beta
    ss_res = float(np.sum((lnq - fitted) ** 2))
    ss_tot = float(np.sum((lnq - np.mean(lnq)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None

    beta_lnp = float(beta[1])
    elasticity = -beta_lnp
    return ElasticityFit(
        elasticity=elasticity,
        intercept=float(beta[0]),
        n_cells=n_cells,
        n_kept=n_kept,
        r_squared=r2,
        price_var_within=price_var_within,
        pooled=pooled,
        note="ok",
    )


def _within_variance(lnp: np.ndarray, agr_labels: list, mon_labels: list) -> float:
    """Variance of ln(price) after sweeping out the agreement and month means (a one-pass additive
    approximation of the FE de-meaning). Near-zero => the FE absorb essentially all price movement
    and beta_lnprice is unidentified, so we refuse the fit instead of reporting noise."""
    x = lnp.copy()
    for labels in (agr_labels, mon_labels):
        groups: dict = {}
        for i, lab in enumerate(labels):
            groups.setdefault(lab, []).append(i)
        means = np.zeros_like(x)
        for idx in groups.values():
            means[idx] = float(np.mean(x[idx]))
        x = x - means + float(np.mean(x))
    return float(np.var(x))


def elastic_optimal_price(elasticity: float | None, unit_cost_eur: float | None) -> float | None:
    """Constant-elasticity profit-max markup: p* = cost * e/(e-1) for e>1. None when e<=1 (optimum
    unbounded), no cost, or no elasticity. e exactly at/just above 1 explodes the markup, so callers
    confidence-gate this and the A+B price stays primary."""
    if elasticity is None or unit_cost_eur is None:
        return None
    if elasticity <= 1.0:
        return None
    return unit_cost_eur * elasticity / (elasticity - 1.0)


def is_sane_elasticity(
    elasticity: float | None, lo: float = 0.5, hi: float = 5.0
) -> bool:
    """Economic sanity gate: positive (normal good) and in a plausible magnitude band. Wrong-signed
    (e<=0, demand rising in price) or extreme estimates are sparse-data failures and are suppressed."""
    if elasticity is None:
        return False
    return lo <= elasticity <= hi
