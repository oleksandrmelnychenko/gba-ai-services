"""Leakage-safe modeling dataset builder for the supervised solvency model (v3).

This is the DATA LAYER for a real supervised credit-risk model. It produces a clean,
point-in-time feature matrix over every role-1 buyer plus a forward-looking label, ready
for the (later) modeling stage. Read-only against ConcordDb_V5.

Design principles (all verified against the live DB during build):

LABEL — SEV180 (severe delinquency, 180+ days past grace, >= 100 EUR):
    label(client, AS_OF) = 1 iff the buyer's summed overdue EUR exposure on debt lines that
    are more than (Agreement.NumberDaysDebt + 180) days past their Debt.Created anchor is
    >= 100 EUR; else 0. Reproduces 136 / 3006 = 4.52% as-of 2026-06-25.

FEATURES — ~22, every one strictly as-of FEATURE_DATE (the leakage rule: only rows with
    Created <= FEATURE_DATE participate). Each group is a single set-based query keyed by
    client_id so the whole buyer population is scored in a handful of round-trips.

DATA TRAPS honored (verified):
  - Buyers = role «Покупці Україна»: Client.Deleted=0 AND IsSubClient=0 AND EXISTS
    ClientInRole(Deleted=0, ClientTypeRoleID=1).
  - Sale/Order/OrderItem are ALL Deleted=1 -> NEVER filter Deleted=0; validity via
    OrderItem.IsValidForCurrentSale=1 and SaleReturn.IsCanceled=0.
  - Synthetic ProductID 25422404 («Ввід боргів з 1С») EXCLUDED from turnover/sales/RFM but
    KEPT in debt/exposure (real carried debt).
  - OrderItem.PricePerItem is ALREADY EUR (never wrap in FX).
  - Debt.Total is AGREEMENT currency -> EUR via dbo.GetExchangedToEuroValue(Total,
    Agreement.CurrencyID, AS_OF). Debt.Days is dead (always 0) -> ignored.
  - UNPAID ground truth = Debt/ClientInDebt table, NOT BaseSalePaymentStatus.
  - Debt aging anchor = Debt.Created; overdue_days = DATEDIFF(day, Debt.Created, AS_OF) -
    Agreement.NumberDaysDebt. Link: ClientInDebt(Deleted=0).DebtID->Debt(Deleted=0);
    ClientInDebt.AgreementID->Agreement (verified 0 nulls on live debt rows).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

from app.core.config import get_settings
from app.data.db import in_clause, query

# SEV180 severity threshold (EUR). A buyer is a positive iff overdue 180+ EUR >= this.
SEV180_MIN_EUR = 100.0

# Feature column order (the ~22 modeling features), grouped for readability.
FEATURE_COLUMNS: list[str] = [
    # debt aging & exposure (EUR, as-of FEATURE_DATE)
    "overdue_eur_180plus",
    "overdue_eur_91_180",
    "overdue_eur_61_90",
    "overdue_eur_31_60",
    "overdue_eur_1_30",
    "current_debt_eur",
    "total_debt_eur",
    "pct_debt_180plus",
    "max_overdue_days",
    "n_open_debt_lines",
    # trajectory
    "debt_growth_3mo",
    "months_with_debt_last12",
    "new_debt_eur_3mo",
    # credit terms / utilization
    "credit_limit_eur",
    "limit_utilization",
    "grace_days",
    "has_credit_control",
    # RFM / volume
    "turnover_eur_12mo",
    "order_count_12mo",
    "recency_days",
    "tenure_months",
    # returns
    "return_rate_12mo",
]


def _synthetic_not_in() -> tuple[str, dict[str, Any]]:
    """Parameterized 'NOT IN (...)' over every configured synthetic 1С debt-entry ProductID."""
    ids = sorted(get_settings().synthetic_line_product_ids)
    placeholder, params = in_clause("synthetic", ids)
    return placeholder, params


# --------------------------------------------------------------------------------------------
# Buyer universe
# --------------------------------------------------------------------------------------------
def buyer_ids() -> list[int]:
    """All role-1 buyers («Покупці Україна»). Deterministic order by ID."""
    rows = query(
        """
        SELECT c.ID AS client_id
        FROM dbo.Client c
        WHERE c.Deleted = 0
              AND c.IsSubClient = 0
              AND EXISTS (
                  SELECT 1 FROM dbo.ClientInRole cir
                  WHERE cir.ClientID = c.ID
                        AND cir.Deleted = 0
                        AND cir.ClientTypeRoleID = 1
              )
        ORDER BY c.ID
        """
    )
    return [int(r["client_id"]) for r in rows]


# --------------------------------------------------------------------------------------------
# LABEL — SEV180 (point-in-time, parameterized by AS_OF)
# --------------------------------------------------------------------------------------------
def _sev180_eur_by_client(as_of: str) -> dict[int, float]:
    """SUM of overdue-180+ EUR exposure per buyer, as-of `as_of`.

    A debt line counts iff DATEDIFF(day, Debt.Created, as_of) > Agreement.NumberDaysDebt + 180.
    EUR via GetExchangedToEuroValue revalued at `as_of`. Created <= as_of (no future leak).
    """
    rows = query(
        """
        SELECT cid.ClientID AS client_id,
               SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)) AS sev180_eur
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        WHERE cid.Deleted = 0
              AND d.Deleted = 0
              AND d.Created <= :asof
              AND DATEDIFF(day, d.Created, :asof) > a.NumberDaysDebt + 180
        GROUP BY cid.ClientID
        """,
        {"asof": as_of},
    )
    return {int(r["client_id"]): float(r["sev180_eur"] or 0.0) for r in rows}


def label_sev180(as_of: str, clients: list[int] | None = None) -> dict[int, int]:
    """SEV180 label per buyer as-of `as_of`: 1 iff overdue-180+ EUR >= SEV180_MIN_EUR else 0.

    `clients` bounds the returned dict (defaults to all role-1 buyers, each defaulting to 0).
    """
    if clients is None:
        clients = buyer_ids()
    sev = _sev180_eur_by_client(as_of)
    return {cid: int(sev.get(cid, 0.0) >= SEV180_MIN_EUR) for cid in clients}


def label_sev180_one(client_id: int, as_of: str) -> int:
    """Single-client SEV180 label (used by the smoke test)."""
    rows = query(
        """
        SELECT ISNULL(SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)), 0) AS sev180_eur
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        WHERE cid.Deleted = 0
              AND d.Deleted = 0
              AND d.Created <= :asof
              AND cid.ClientID = :cid
              AND DATEDIFF(day, d.Created, :asof) > a.NumberDaysDebt + 180
        """,
        {"asof": as_of, "cid": client_id},
    )
    return int(float(rows[0]["sev180_eur"] or 0.0) >= SEV180_MIN_EUR)


# --------------------------------------------------------------------------------------------
# FEATURE GROUP 1 — debt aging & exposure (EUR, as-of FEATURE_DATE)
# --------------------------------------------------------------------------------------------
def feat_debt_aging(feature_date: str) -> pd.DataFrame:
    """Per-buyer aging buckets, totals, max overdue days, open-line count.

    overdue_days = DATEDIFF(day, Debt.Created, FD) - Agreement.NumberDaysDebt.
      > 0   -> past grace (split into 1-30 / 31-60 / 61-90 / 91-180 / 180+)
      <= 0  -> not yet past grace -> current_debt_eur
    All debt lines (incl. synthetic 1С ProductID) count — exposure is real carried debt.
    """
    rows = query(
        """
        SELECT client_id,
            SUM(CASE WHEN od > 180 THEN e ELSE 0 END) AS overdue_eur_180plus,
            SUM(CASE WHEN od BETWEEN 91 AND 180 THEN e ELSE 0 END) AS overdue_eur_91_180,
            SUM(CASE WHEN od BETWEEN 61 AND 90 THEN e ELSE 0 END) AS overdue_eur_61_90,
            SUM(CASE WHEN od BETWEEN 31 AND 60 THEN e ELSE 0 END) AS overdue_eur_31_60,
            SUM(CASE WHEN od BETWEEN 1 AND 30 THEN e ELSE 0 END) AS overdue_eur_1_30,
            SUM(CASE WHEN od <= 0 THEN e ELSE 0 END) AS current_debt_eur,
            SUM(e) AS total_debt_eur,
            MAX(od) AS max_overdue_days,
            COUNT(*) AS n_open_debt_lines
        FROM (
            SELECT cid.ClientID AS client_id,
                   DATEDIFF(day, d.Created, :fd) - a.NumberDaysDebt AS od,
                   dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :fd) AS e
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            JOIN dbo.Agreement a ON a.ID = cid.AgreementID
            WHERE cid.Deleted = 0
                  AND d.Deleted = 0
                  AND d.Created <= :fd
        ) t
        GROUP BY client_id
        """,
        {"fd": feature_date},
    )
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(
            columns=[
                "client_id", "overdue_eur_180plus", "overdue_eur_91_180", "overdue_eur_61_90",
                "overdue_eur_31_60", "overdue_eur_1_30", "current_debt_eur", "total_debt_eur",
                "max_overdue_days", "n_open_debt_lines",
            ]
        )
    num = [c for c in df.columns if c != "client_id"]
    df[num] = df[num].astype(float)
    df["client_id"] = df["client_id"].astype(int)
    # pct of debt that is 180+ overdue (derived; 0 when no debt)
    df["pct_debt_180plus"] = (
        df["overdue_eur_180plus"] / df["total_debt_eur"].where(df["total_debt_eur"] > 0)
    ).fillna(0.0)
    # max_overdue_days: negative (all current) clamps to 0
    df["max_overdue_days"] = df["max_overdue_days"].clip(lower=0)
    return df


# --------------------------------------------------------------------------------------------
# FEATURE GROUP 2 — trajectory
# --------------------------------------------------------------------------------------------
def feat_debt_trajectory(feature_date: str) -> pd.DataFrame:
    """debt_growth_3mo, months_with_debt_last12, new_debt_eur_3mo (by Debt.Created window).

    new_debt_eur_3mo   = EUR of debt Created in (FD-3mo, FD].
    debt_growth_3mo    = new_debt_eur_3mo / EUR of debt Created in (FD-6mo, FD-3mo], capped [0,10].
                         When the prior window is empty: 10.0 if recent>0 else 0.0.
    months_with_debt_last12 = distinct yyyy-MM with a debt Created in (FD-12mo, FD].
    """
    rows = query(
        """
        SELECT cid.ClientID AS client_id,
               SUM(CASE WHEN d.Created > DATEADD(month, -3, :fd) THEN e ELSE 0 END) AS new3,
               SUM(CASE WHEN d.Created > DATEADD(month, -6, :fd)
                         AND d.Created <= DATEADD(month, -3, :fd) THEN e ELSE 0 END) AS prev3,
               COUNT(DISTINCT CASE WHEN d.Created > DATEADD(month, -12, :fd)
                                   THEN FORMAT(d.Created, 'yyyy-MM') END) AS months12
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        CROSS APPLY (SELECT dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :fd) AS e) x
        WHERE cid.Deleted = 0
              AND d.Deleted = 0
              AND d.Created <= :fd
        GROUP BY cid.ClientID
        """,
        {"fd": feature_date},
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(
            columns=["client_id", "debt_growth_3mo", "months_with_debt_last12", "new_debt_eur_3mo"]
        )
    df["client_id"] = df["client_id"].astype(int)
    df["new3"] = df["new3"].astype(float)
    df["prev3"] = df["prev3"].astype(float)
    df["months_with_debt_last12"] = df["months12"].astype(float)
    df["new_debt_eur_3mo"] = df["new3"]

    def _growth(r: pd.Series) -> float:
        if r["prev3"] > 0:
            return min(r["new3"] / r["prev3"], 10.0)
        return 10.0 if r["new3"] > 0 else 0.0

    df["debt_growth_3mo"] = df.apply(_growth, axis=1)
    return df[["client_id", "debt_growth_3mo", "months_with_debt_last12", "new_debt_eur_3mo"]]


# --------------------------------------------------------------------------------------------
# FEATURE GROUP 3 — credit terms / utilization (ClientAgreement / Agreement)
# --------------------------------------------------------------------------------------------
def feat_credit_terms(feature_date: str) -> pd.DataFrame:
    """credit_limit_eur, limit_utilization, grace_days, has_credit_control per buyer.

    A buyer may hold several agreements; we aggregate to the buyer's headline credit posture:
      credit_limit_eur  = SUM(AmountDebt->EUR) over agreements with IsControlAmountDebt=1.
      limit_utilization = SUM(CurrentAmount->EUR) / credit_limit_eur, clamped [0, 2].
      grace_days        = MAX(NumberDaysDebt) across the buyer's agreements (most lenient term).
      has_credit_control= 1 iff ANY agreement has IsControlAmountDebt OR IsControlNumberDaysDebt.
    ClientAgreement.CurrentAmount is in agreement currency -> EUR via the FX fn at FD.
    Only agreements created on or before FD participate (leakage rule).
    """
    rows = query(
        """
        SELECT ca.ClientID AS client_id,
               SUM(CASE WHEN a.IsControlAmountDebt = 1
                        THEN dbo.GetExchangedToEuroValue(a.AmountDebt, a.CurrencyID, :fd)
                        ELSE 0 END) AS credit_limit_eur,
               SUM(CASE WHEN a.IsControlAmountDebt = 1
                        THEN dbo.GetExchangedToEuroValue(ca.CurrentAmount, a.CurrencyID, :fd)
                        ELSE 0 END) AS controlled_balance_eur,
               MAX(a.NumberDaysDebt) AS grace_days,
               MAX(CASE WHEN a.IsControlAmountDebt = 1 OR a.IsControlNumberDaysDebt = 1
                        THEN 1 ELSE 0 END) AS has_credit_control
        FROM dbo.ClientAgreement ca
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.Deleted = 0
              AND a.Deleted = 0
              AND ca.Created <= :fd
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date},
    )
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "client_id", "credit_limit_eur", "limit_utilization", "grace_days",
                "has_credit_control",
            ]
        )
    df["client_id"] = df["client_id"].astype(int)
    df["credit_limit_eur"] = df["credit_limit_eur"].astype(float)
    df["controlled_balance_eur"] = df["controlled_balance_eur"].astype(float)
    df["grace_days"] = df["grace_days"].fillna(0).astype(float)
    df["has_credit_control"] = df["has_credit_control"].fillna(0).astype(float)
    df["limit_utilization"] = (
        df["controlled_balance_eur"] / df["credit_limit_eur"].where(df["credit_limit_eur"] > 0)
    ).clip(lower=0, upper=2).fillna(0.0)
    return df[
        ["client_id", "credit_limit_eur", "limit_utilization", "grace_days", "has_credit_control"]
    ]


# --------------------------------------------------------------------------------------------
# FEATURE GROUP 4 — RFM / volume (Sale + Order + OrderItem)
# --------------------------------------------------------------------------------------------
def feat_rfm(feature_date: str, window_months: int = 12) -> pd.DataFrame:
    """turnover_eur_12mo, order_count_12mo, recency_days, tenure_months.

    Traps: NO Deleted=0 on Sale/Order/OrderItem; OrderItem.IsValidForCurrentSale=1; exclude
    synthetic ProductID; PricePerItem is ALREADY EUR (no FX). recency/tenure use ALL history
    up to FD; turnover/order_count use the (FD-window, FD] window.
    """
    ph, syn = _synthetic_not_in()
    s = get_settings()
    # turnover + order_count windowed by Sale.Created
    rows_win = query(
        f"""
        SELECT ca.ClientID AS client_id,
               ISNULL(SUM(oi.Qty * oi.PricePerItem), 0) AS turnover_eur_12mo,
               COUNT(DISTINCT s.ID) AS order_count_12mo
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph}
              AND s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND s.Created >= DATEADD(month, :neg_months, :fd)
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date, "neg_months": -window_months, **syn},
    )
    # recency + tenure over ALL history up to FD, REAL sales only. The synthetic 1С debt line is
    # written as ~21.7k standalone single-line Sales, so a bare Sale-existence signal lets those
    # contaminate MAX/MIN(Created) (recency/tenure). Gate to sales that carry a valid,
    # non-synthetic OrderItem so a purely-synthetic client reads as no-sales (NULL -> sentinel).
    rows_hist = query(
        f"""
        SELECT ca.ClientID AS client_id,
               DATEDIFF(day, MAX(s.Created), :fd) AS recency_days,
               DATEDIFF(month, MIN(CASE WHEN s.Created > :sentinel THEN s.Created END), :fd)
                   AS tenure_months
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND EXISTS (
                  SELECT 1 FROM dbo.OrderItem oi
                  WHERE oi.OrderID = s.OrderID
                        AND oi.ProductID NOT IN {ph}
                        AND oi.IsValidForCurrentSale = 1
              )
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date, "sentinel": s.tenure_sentinel_date, **syn},
    )
    win = pd.DataFrame(rows_win)
    hist = pd.DataFrame(rows_hist)
    if win.empty:
        win = pd.DataFrame(columns=["client_id", "turnover_eur_12mo", "order_count_12mo"])
    if hist.empty:
        hist = pd.DataFrame(columns=["client_id", "recency_days", "tenure_months"])
    for d in (win, hist):
        if not d.empty:
            d["client_id"] = d["client_id"].astype(int)
    df = win.merge(hist, on="client_id", how="outer")
    for c in ["turnover_eur_12mo", "order_count_12mo", "recency_days", "tenure_months"]:
        if c in df.columns:
            df[c] = df[c].astype(float)
    return df


# --------------------------------------------------------------------------------------------
# FEATURE GROUP 5 — returns
# --------------------------------------------------------------------------------------------
def feat_returns(feature_date: str, window_months: int = 12) -> pd.DataFrame:
    """return_rate_12mo = returned qty / sold qty over the window.

    Returned qty is reconstructed the SAME faithful way gba-products does (verified on
    ConcordDb_V5): the 1С DataSync write path omits SaleReturnItem.Qty (14/19,969 nonzero ~0), so
    SUM(sri.Qty) is a dead ~0 constant. Instead each non-deleted SaleReturnItem means "this
    OrderItem line came back", so we take the line's sold OrderItem.Qty once per distinct
    (SaleReturnID, OrderItemID) and sum. Window on SaleReturn.FromDate (sr.Created is a bulk-sync
    mirror stamp that mis-dates returns); active returns only (sr.Deleted=0 AND sr.IsCanceled=0);
    oi.Deleted is NOT filtered; exclude the synthetic 1С product.
    Sold qty: valid OrderItem.Qty (synthetic excluded) by buyer, windowed by Sale.Created<=FD.
    """
    ph, syn = _synthetic_not_in()
    sold = query(
        f"""
        SELECT ca.ClientID AS client_id, ISNULL(SUM(oi.Qty), 0) AS sold_qty
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph}
              AND s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND s.Created >= DATEADD(month, :neg_months, :fd)
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date, "neg_months": -window_months, **syn},
    )
    ret = query(
        f"""
        SELECT client_id, ISNULL(SUM(oi_qty), 0) AS return_qty
        FROM (
            SELECT sr.ClientID AS client_id, MAX(oi.Qty) AS oi_qty
            FROM dbo.SaleReturnItem sri
            JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
            JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
                 AND sr.Deleted = 0 AND sr.IsCanceled = 0
            WHERE sri.Deleted = 0
                  AND oi.ProductID IS NOT NULL
                  AND oi.ProductID NOT IN {ph}
                  AND sr.FromDate <= :fd
                  AND sr.FromDate >= DATEADD(month, :neg_months, :fd)
            GROUP BY sr.ClientID, sri.SaleReturnID, sri.OrderItemID
        ) line
        GROUP BY client_id
        """,
        {"fd": feature_date, "neg_months": -window_months, **syn},
    )
    sold_df = pd.DataFrame(sold) if sold else pd.DataFrame(columns=["client_id", "sold_qty"])
    ret_df = pd.DataFrame(ret) if ret else pd.DataFrame(columns=["client_id", "return_qty"])
    for d in (sold_df, ret_df):
        if not d.empty:
            d["client_id"] = d["client_id"].astype(int)
    df = sold_df.merge(ret_df, on="client_id", how="left")
    if df.empty:
        return pd.DataFrame(columns=["client_id", "return_rate_12mo"])
    df["sold_qty"] = df["sold_qty"].astype(float)
    df["return_qty"] = df["return_qty"].astype(float).fillna(0.0)
    df["return_rate_12mo"] = (
        df["return_qty"] / df["sold_qty"].where(df["sold_qty"] > 0)
    ).fillna(0.0)
    return df[["client_id", "return_rate_12mo"]]


# --------------------------------------------------------------------------------------------
# ASSEMBLE — full modeling dataset over all role-1 buyers
# --------------------------------------------------------------------------------------------
def build_dataset(
    feature_date: str,
    label_date: str,
    window_months: int = 12,
) -> pd.DataFrame:
    """Assemble the leakage-safe modeling dataset.

    Columns: client_id, the ~22 features (as-of feature_date), label_sev180 (as-of label_date),
    already_default_at_feature_date (SEV180 as-of feature_date). Every role-1 buyer is a row;
    buyers with no debt/sales get 0-filled exposure/volume features.
    """
    clients = buyer_ids()
    base = pd.DataFrame({"client_id": clients})

    aging = feat_debt_aging(feature_date)
    traj = feat_debt_trajectory(feature_date)
    terms = feat_credit_terms(feature_date)
    rfm = feat_rfm(feature_date, window_months)
    rets = feat_returns(feature_date, window_months)

    df = base
    for part in (aging, traj, terms, rfm, rets):
        df = df.merge(part, on="client_id", how="left")

    # labels (dicts keyed by client_id, default 0)
    lab_fd = label_sev180(feature_date, clients)
    lab_ld = label_sev180(label_date, clients)
    df["already_default_at_feature_date"] = df["client_id"].map(lab_fd).astype(int)
    df["label_sev180"] = df["client_id"].map(lab_ld).astype(int)

    # recency_days: a buyer with NO sales is NOT "bought today" (0) — that would conflate
    # never-active with just-active. Set no-sales recency to a worst-case sentinel = the worst
    # observed recency + 1 (monotone, model-friendly), before zero-filling the rest.
    if "recency_days" in df.columns:
        worst_recency = df["recency_days"].max()
        sentinel = (float(worst_recency) + 1.0) if pd.notna(worst_recency) else 0.0
        df["recency_days"] = df["recency_days"].fillna(sentinel)

    # fill remaining feature NaNs: exposure/volume/derived all default to 0 for a no-activity buyer.
    fill_zero = {c: 0.0 for c in FEATURE_COLUMNS}
    df = df.fillna(value=fill_zero)
    # ensure column order
    df = df[["client_id", *FEATURE_COLUMNS, "label_sev180", "already_default_at_feature_date"]]

    # type tidy
    for c in FEATURE_COLUMNS:
        df[c] = df[c].astype(float)
    df["client_id"] = df["client_id"].astype(int)
    return df


# --------------------------------------------------------------------------------------------
# SINGLE-CLIENT feature path (live, on-demand scoring)
# --------------------------------------------------------------------------------------------
# A no-sales buyer cannot observe the population's worst recency (the set-based builder uses
# worst_observed+1). For one client we pin a fixed large sentinel that sits in the lowest-risk
# (oldest-recency) bin of both scorecards: > the current card's only recency split (2948) and in
# the forward card's "recency_days > 1314.8" bin. Matches the dataset's "never-active != just-
# active" intent without a population scan.
_NO_SALES_RECENCY_SENTINEL = 3000.0


def features_one(client_id: int, feature_date: str, window_months: int = 12) -> dict[str, float]:
    """All 22 feature columns for ONE buyer, as-of `feature_date` (live, on-demand scoring).

    Reuses the exact set-based SQL of feat_debt_aging / feat_debt_trajectory / feat_credit_terms /
    feat_rfm / feat_returns, each filtered to a single client_id, and applies the same fill rules
    as build_dataset (0 for exposure/volume/derived; no-sales recency -> large sentinel). The
    result feeds score_current (uses the 20 primary features) and score_forward (uses up to 22).
    """
    ph, syn = _synthetic_not_in()
    s = get_settings()
    feats: dict[str, float] = {c: 0.0 for c in FEATURE_COLUMNS}

    # The 6 feature-group queries are mutually independent (each hits a different table set and
    # opens its OWN pooled connection inside query()), so we fan them out across a small thread
    # pool and join. Pure latency win — identical SQL, identical params, identical results as the
    # former sequential path.
    _sql_aging = (
        """
        SELECT
            SUM(CASE WHEN od > 180 THEN e ELSE 0 END) AS overdue_eur_180plus,
            SUM(CASE WHEN od BETWEEN 91 AND 180 THEN e ELSE 0 END) AS overdue_eur_91_180,
            SUM(CASE WHEN od BETWEEN 61 AND 90 THEN e ELSE 0 END) AS overdue_eur_61_90,
            SUM(CASE WHEN od BETWEEN 31 AND 60 THEN e ELSE 0 END) AS overdue_eur_31_60,
            SUM(CASE WHEN od BETWEEN 1 AND 30 THEN e ELSE 0 END) AS overdue_eur_1_30,
            SUM(CASE WHEN od <= 0 THEN e ELSE 0 END) AS current_debt_eur,
            SUM(e) AS total_debt_eur,
            MAX(od) AS max_overdue_days,
            COUNT(*) AS n_open_debt_lines
        FROM (
            SELECT DATEDIFF(day, d.Created, :fd) - a.NumberDaysDebt AS od,
                   dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :fd) AS e
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            JOIN dbo.Agreement a ON a.ID = cid.AgreementID
            WHERE cid.Deleted = 0
                  AND d.Deleted = 0
                  AND cid.ClientID = :cid
                  AND d.Created <= :fd
        ) t
        """,
        {"fd": feature_date, "cid": client_id},
    )

    # GROUP 2 — trajectory (mirrors feat_debt_trajectory, one client)
    _sql_traj = (
        """
        SELECT
            SUM(CASE WHEN d.Created > DATEADD(month, -3, :fd) THEN e ELSE 0 END) AS new3,
            SUM(CASE WHEN d.Created > DATEADD(month, -6, :fd)
                      AND d.Created <= DATEADD(month, -3, :fd) THEN e ELSE 0 END) AS prev3,
            COUNT(DISTINCT CASE WHEN d.Created > DATEADD(month, -12, :fd)
                                THEN FORMAT(d.Created, 'yyyy-MM') END) AS months12
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        CROSS APPLY (SELECT dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :fd) AS e) x
        WHERE cid.Deleted = 0
              AND d.Deleted = 0
              AND cid.ClientID = :cid
              AND d.Created <= :fd
        """,
        {"fd": feature_date, "cid": client_id},
    )

    # GROUP 3 — credit terms / utilization (mirrors feat_credit_terms, one client)
    _sql_terms = (
        """
        SELECT
            SUM(CASE WHEN a.IsControlAmountDebt = 1
                     THEN dbo.GetExchangedToEuroValue(a.AmountDebt, a.CurrencyID, :fd)
                     ELSE 0 END) AS credit_limit_eur,
            SUM(CASE WHEN a.IsControlAmountDebt = 1
                     THEN dbo.GetExchangedToEuroValue(ca.CurrentAmount, a.CurrencyID, :fd)
                     ELSE 0 END) AS controlled_balance_eur,
            MAX(a.NumberDaysDebt) AS grace_days,
            MAX(CASE WHEN a.IsControlAmountDebt = 1 OR a.IsControlNumberDaysDebt = 1
                     THEN 1 ELSE 0 END) AS has_credit_control
        FROM dbo.ClientAgreement ca
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.Deleted = 0
              AND a.Deleted = 0
              AND ca.ClientID = :cid
              AND ca.Created <= :fd
        """,
        {"fd": feature_date, "cid": client_id},
    )

    # GROUP 4 — RFM / volume (mirrors feat_rfm, one client)
    _sql_rfm_win = (
        f"""
        SELECT ISNULL(SUM(oi.Qty * oi.PricePerItem), 0) AS turnover_eur_12mo,
               COUNT(DISTINCT s.ID) AS order_count_12mo
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph}
              AND s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND s.Created >= DATEADD(month, :neg_months, :fd)
        """,
        {"fd": feature_date, "cid": client_id, "neg_months": -window_months, **syn},
    )
    _sql_rfm_hist = (
        f"""
        SELECT DATEDIFF(day, MAX(s.Created), :fd) AS recency_days,
               DATEDIFF(month, MIN(CASE WHEN s.Created > :sentinel THEN s.Created END), :fd)
                   AS tenure_months
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID = :cid
              AND s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND EXISTS (
                  SELECT 1 FROM dbo.OrderItem oi
                  WHERE oi.OrderID = s.OrderID
                        AND oi.ProductID NOT IN {ph}
                        AND oi.IsValidForCurrentSale = 1
              )
        """,
        {"fd": feature_date, "cid": client_id, "sentinel": s.tenure_sentinel_date, **syn},
    )

    # GROUP 5 — returns (mirrors feat_returns, one client). Returned qty reconstructed the
    # gba-products way: distinct (SaleReturnID, OrderItemID) sold OrderItem.Qty, windowed on
    # SaleReturn.FromDate, active returns only (sr.Created qty path is a dead ~0 constant).
    _sql_ret = (
        f"""
        SELECT
            (SELECT ISNULL(SUM(line.oi_qty), 0)
             FROM (
                SELECT MAX(oi.Qty) AS oi_qty
                FROM dbo.SaleReturnItem sri
                JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
                JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
                     AND sr.Deleted = 0 AND sr.IsCanceled = 0
                WHERE sri.Deleted = 0
                      AND oi.ProductID IS NOT NULL
                      AND oi.ProductID NOT IN {ph}
                      AND sr.ClientID = :cid
                      AND sr.FromDate <= :fd
                      AND sr.FromDate >= DATEADD(month, :neg_months, :fd)
                GROUP BY sri.SaleReturnID, sri.OrderItemID
             ) line) AS return_qty,
            (SELECT ISNULL(SUM(oi.Qty), 0)
             FROM dbo.Sale s
             JOIN dbo.[Order] o ON o.ID = s.OrderID
             JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
             JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
             WHERE ca.ClientID = :cid
                   AND oi.IsValidForCurrentSale = 1
                   AND oi.ProductID NOT IN {ph}
                   AND s.Created > '2000-01-01'
                   AND s.Created <= :fd
                   AND s.Created >= DATEADD(month, :neg_months, :fd)) AS sold_qty
        """,
        {"fd": feature_date, "cid": client_id, "neg_months": -window_months, **syn},
    )

    # Fan out the 6 independent group queries over a thread pool (each on its own pooled
    # connection); join, then apply the SAME post-processing as the former sequential path.
    groups = (_sql_aging, _sql_traj, _sql_terms, _sql_rfm_win, _sql_rfm_hist, _sql_ret)
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        aging, traj, terms, rfm_win, rfm_hist, ret = pool.map(
            lambda g: query(g[0], g[1]), groups
        )

    # GROUP 1 — debt aging & exposure
    r = aging[0] if aging else {}
    total_debt = float(r.get("total_debt_eur") or 0.0)
    if r.get("n_open_debt_lines"):
        for col in (
            "overdue_eur_180plus", "overdue_eur_91_180", "overdue_eur_61_90", "overdue_eur_31_60",
            "overdue_eur_1_30", "current_debt_eur", "total_debt_eur", "n_open_debt_lines",
        ):
            feats[col] = float(r.get(col) or 0.0)
        feats["max_overdue_days"] = max(float(r.get("max_overdue_days") or 0.0), 0.0)
        feats["pct_debt_180plus"] = (
            feats["overdue_eur_180plus"] / total_debt if total_debt > 0 else 0.0
        )

    # GROUP 2 — trajectory
    if traj:
        tr = traj[0]
        new3 = float(tr.get("new3") or 0.0)
        prev3 = float(tr.get("prev3") or 0.0)
        feats["new_debt_eur_3mo"] = new3
        feats["months_with_debt_last12"] = float(tr.get("months12") or 0.0)
        if prev3 > 0:
            feats["debt_growth_3mo"] = min(new3 / prev3, 10.0)
        else:
            feats["debt_growth_3mo"] = 10.0 if new3 > 0 else 0.0

    # GROUP 3 — credit terms / utilization
    if terms:
        tm = terms[0]
        climit = float(tm.get("credit_limit_eur") or 0.0)
        cbal = float(tm.get("controlled_balance_eur") or 0.0)
        feats["credit_limit_eur"] = climit
        feats["grace_days"] = float(tm.get("grace_days") or 0.0)
        feats["has_credit_control"] = float(tm.get("has_credit_control") or 0.0)
        util = (cbal / climit) if climit > 0 else 0.0
        feats["limit_utilization"] = min(max(util, 0.0), 2.0)

    # GROUP 4 — RFM / volume
    if rfm_win:
        feats["turnover_eur_12mo"] = float(rfm_win[0].get("turnover_eur_12mo") or 0.0)
        feats["order_count_12mo"] = float(rfm_win[0].get("order_count_12mo") or 0.0)
    recency = rfm_hist[0].get("recency_days") if rfm_hist else None
    tenure = rfm_hist[0].get("tenure_months") if rfm_hist else None
    feats["recency_days"] = (
        float(recency) if recency is not None else _NO_SALES_RECENCY_SENTINEL
    )
    feats["tenure_months"] = float(tenure) if tenure is not None else 0.0

    # GROUP 5 — returns
    if ret:
        sold = float(ret[0].get("sold_qty") or 0.0)
        feats["return_rate_12mo"] = (
            float(ret[0].get("return_qty") or 0.0) / sold if sold > 0 else 0.0
        )

    return feats


# --------------------------------------------------------------------------------------------
# SET-BASED multi-client feature path (live batch scoring)
# --------------------------------------------------------------------------------------------
def features_many(
    client_ids: list[int], feature_date: str, window_months: int = 12
) -> dict[int, dict[str, float]]:
    """All 22 feature columns for a LIST of buyers, as-of `feature_date`, in a handful of queries.

    Bit-identical to calling features_one(cid, feature_date, window_months) for each id: it runs
    the SAME set-based SQL of feat_debt_aging / feat_debt_trajectory / feat_credit_terms /
    feat_rfm / feat_returns, each filtered to the id list with an IN-clause and grouped by
    client_id, then applies the EXACT per-client fill rules of features_one (0 for
    exposure/volume/derived; no-sales recency -> the fixed _NO_SALES_RECENCY_SENTINEL, NOT the
    population-worst+1 that build_dataset uses). One round-trip per feature group regardless of N.

    Returns {client_id: {feature: value}} for every id in `client_ids` (zero-feature baseline for
    ids with no debt/sales).
    """
    ph_syn, syn = _synthetic_not_in()
    s = get_settings()
    ids = list(dict.fromkeys(int(c) for c in client_ids))  # de-dupe, preserve order
    out: dict[int, dict[str, float]] = {
        cid: {c: 0.0 for c in FEATURE_COLUMNS} for cid in ids
    }
    if not ids:
        return out
    ph_cid, cid_params = in_clause("cid", ids)

    # GROUP 1 — debt aging & exposure (set-based feat_debt_aging, filtered to id list)
    aging = query(
        f"""
        SELECT client_id,
            SUM(CASE WHEN od > 180 THEN e ELSE 0 END) AS overdue_eur_180plus,
            SUM(CASE WHEN od BETWEEN 91 AND 180 THEN e ELSE 0 END) AS overdue_eur_91_180,
            SUM(CASE WHEN od BETWEEN 61 AND 90 THEN e ELSE 0 END) AS overdue_eur_61_90,
            SUM(CASE WHEN od BETWEEN 31 AND 60 THEN e ELSE 0 END) AS overdue_eur_31_60,
            SUM(CASE WHEN od BETWEEN 1 AND 30 THEN e ELSE 0 END) AS overdue_eur_1_30,
            SUM(CASE WHEN od <= 0 THEN e ELSE 0 END) AS current_debt_eur,
            SUM(e) AS total_debt_eur,
            MAX(od) AS max_overdue_days,
            COUNT(*) AS n_open_debt_lines
        FROM (
            SELECT cid.ClientID AS client_id,
                   DATEDIFF(day, d.Created, :fd) - a.NumberDaysDebt AS od,
                   dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :fd) AS e
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            JOIN dbo.Agreement a ON a.ID = cid.AgreementID
            WHERE cid.Deleted = 0
                  AND d.Deleted = 0
                  AND cid.ClientID IN {ph_cid}
                  AND d.Created <= :fd
        ) t
        GROUP BY client_id
        """,
        {"fd": feature_date, **cid_params},
    )
    for r in aging:
        feats = out[int(r["client_id"])]
        total_debt = float(r.get("total_debt_eur") or 0.0)
        if r.get("n_open_debt_lines"):
            for col in (
                "overdue_eur_180plus", "overdue_eur_91_180", "overdue_eur_61_90",
                "overdue_eur_31_60", "overdue_eur_1_30", "current_debt_eur",
                "total_debt_eur", "n_open_debt_lines",
            ):
                feats[col] = float(r.get(col) or 0.0)
            feats["max_overdue_days"] = max(float(r.get("max_overdue_days") or 0.0), 0.0)
            feats["pct_debt_180plus"] = (
                feats["overdue_eur_180plus"] / total_debt if total_debt > 0 else 0.0
            )

    # GROUP 2 — trajectory (set-based feat_debt_trajectory, filtered to id list)
    traj = query(
        f"""
        SELECT cid.ClientID AS client_id,
               SUM(CASE WHEN d.Created > DATEADD(month, -3, :fd) THEN e ELSE 0 END) AS new3,
               SUM(CASE WHEN d.Created > DATEADD(month, -6, :fd)
                         AND d.Created <= DATEADD(month, -3, :fd) THEN e ELSE 0 END) AS prev3,
               COUNT(DISTINCT CASE WHEN d.Created > DATEADD(month, -12, :fd)
                                   THEN FORMAT(d.Created, 'yyyy-MM') END) AS months12
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        CROSS APPLY (SELECT dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :fd) AS e) x
        WHERE cid.Deleted = 0
              AND d.Deleted = 0
              AND cid.ClientID IN {ph_cid}
              AND d.Created <= :fd
        GROUP BY cid.ClientID
        """,
        {"fd": feature_date, **cid_params},
    )
    for r in traj:
        feats = out[int(r["client_id"])]
        new3 = float(r.get("new3") or 0.0)
        prev3 = float(r.get("prev3") or 0.0)
        feats["new_debt_eur_3mo"] = new3
        feats["months_with_debt_last12"] = float(r.get("months12") or 0.0)
        if prev3 > 0:
            feats["debt_growth_3mo"] = min(new3 / prev3, 10.0)
        else:
            feats["debt_growth_3mo"] = 10.0 if new3 > 0 else 0.0

    # GROUP 3 — credit terms / utilization (set-based feat_credit_terms, filtered to id list)
    terms = query(
        f"""
        SELECT ca.ClientID AS client_id,
               SUM(CASE WHEN a.IsControlAmountDebt = 1
                        THEN dbo.GetExchangedToEuroValue(a.AmountDebt, a.CurrencyID, :fd)
                        ELSE 0 END) AS credit_limit_eur,
               SUM(CASE WHEN a.IsControlAmountDebt = 1
                        THEN dbo.GetExchangedToEuroValue(ca.CurrentAmount, a.CurrencyID, :fd)
                        ELSE 0 END) AS controlled_balance_eur,
               MAX(a.NumberDaysDebt) AS grace_days,
               MAX(CASE WHEN a.IsControlAmountDebt = 1 OR a.IsControlNumberDaysDebt = 1
                        THEN 1 ELSE 0 END) AS has_credit_control
        FROM dbo.ClientAgreement ca
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.Deleted = 0
              AND a.Deleted = 0
              AND ca.ClientID IN {ph_cid}
              AND ca.Created <= :fd
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date, **cid_params},
    )
    for r in terms:
        feats = out[int(r["client_id"])]
        climit = float(r.get("credit_limit_eur") or 0.0)
        cbal = float(r.get("controlled_balance_eur") or 0.0)
        feats["credit_limit_eur"] = climit
        feats["grace_days"] = float(r.get("grace_days") or 0.0)
        feats["has_credit_control"] = float(r.get("has_credit_control") or 0.0)
        util = (cbal / climit) if climit > 0 else 0.0
        feats["limit_utilization"] = min(max(util, 0.0), 2.0)

    # GROUP 4a — RFM windowed turnover / order count (set-based feat_rfm, filtered to id list)
    rfm_win = query(
        f"""
        SELECT ca.ClientID AS client_id,
               ISNULL(SUM(oi.Qty * oi.PricePerItem), 0) AS turnover_eur_12mo,
               COUNT(DISTINCT s.ID) AS order_count_12mo
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID IN {ph_cid}
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph_syn}
              AND s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND s.Created >= DATEADD(month, :neg_months, :fd)
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date, "neg_months": -window_months, **cid_params, **syn},
    )
    for r in rfm_win:
        feats = out[int(r["client_id"])]
        feats["turnover_eur_12mo"] = float(r.get("turnover_eur_12mo") or 0.0)
        feats["order_count_12mo"] = float(r.get("order_count_12mo") or 0.0)

    # GROUP 4b — RFM recency / tenure over ALL history, REAL sales only (set-based, filtered to id
    # list). Gate to sales carrying a valid, non-synthetic OrderItem so the ~21.7k standalone 1С
    # debt-injection Sales don't contaminate MAX/MIN(Created) (matches feat_rfm / features_one).
    rfm_hist = query(
        f"""
        SELECT ca.ClientID AS client_id,
               DATEDIFF(day, MAX(s.Created), :fd) AS recency_days,
               DATEDIFF(month, MIN(CASE WHEN s.Created > :sentinel THEN s.Created END), :fd)
                   AS tenure_months
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID IN {ph_cid}
              AND s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND EXISTS (
                  SELECT 1 FROM dbo.OrderItem oi
                  WHERE oi.OrderID = s.OrderID
                        AND oi.ProductID NOT IN {ph_syn}
                        AND oi.IsValidForCurrentSale = 1
              )
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date, "sentinel": s.tenure_sentinel_date, **cid_params, **syn},
    )
    hist_by_cid = {int(r["client_id"]): r for r in rfm_hist}
    for cid in ids:
        feats = out[cid]
        r = hist_by_cid.get(cid)
        recency = r.get("recency_days") if r is not None else None
        tenure = r.get("tenure_months") if r is not None else None
        feats["recency_days"] = (
            float(recency) if recency is not None else _NO_SALES_RECENCY_SENTINEL
        )
        feats["tenure_months"] = float(tenure) if tenure is not None else 0.0

    # GROUP 5 — returns (set-based feat_returns; sold & returned qty, filtered to id list)
    sold = query(
        f"""
        SELECT ca.ClientID AS client_id, ISNULL(SUM(oi.Qty), 0) AS sold_qty
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        WHERE ca.ClientID IN {ph_cid}
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {ph_syn}
              AND s.Created > '2000-01-01'
              AND s.Created <= :fd
              AND s.Created >= DATEADD(month, :neg_months, :fd)
        GROUP BY ca.ClientID
        """,
        {"fd": feature_date, "neg_months": -window_months, **cid_params, **syn},
    )
    ret = query(
        f"""
        SELECT client_id, ISNULL(SUM(oi_qty), 0) AS return_qty
        FROM (
            SELECT sr.ClientID AS client_id, MAX(oi.Qty) AS oi_qty
            FROM dbo.SaleReturnItem sri
            JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
            JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
                 AND sr.Deleted = 0 AND sr.IsCanceled = 0
            WHERE sr.ClientID IN {ph_cid}
                  AND sri.Deleted = 0
                  AND oi.ProductID IS NOT NULL
                  AND oi.ProductID NOT IN {ph_syn}
                  AND sr.FromDate <= :fd
                  AND sr.FromDate >= DATEADD(month, :neg_months, :fd)
            GROUP BY sr.ClientID, sri.SaleReturnID, sri.OrderItemID
        ) line
        GROUP BY client_id
        """,
        {"fd": feature_date, "neg_months": -window_months, **cid_params, **syn},
    )
    sold_by_cid = {int(r["client_id"]): float(r.get("sold_qty") or 0.0) for r in sold}
    ret_by_cid = {int(r["client_id"]): float(r.get("return_qty") or 0.0) for r in ret}
    for cid in ids:
        sq = sold_by_cid.get(cid, 0.0)
        out[cid]["return_rate_12mo"] = (
            ret_by_cid.get(cid, 0.0) / sq if sq > 0 else 0.0
        )

    return out
