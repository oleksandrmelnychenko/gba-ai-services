"""Always-on source guards (no DB) — pin the just-fixed currency-conversion SQL into CI.

Every mocked unit test stayed GREEN while a real currency bug shipped (UAH summed as if EUR,
~50x inflated), because mocks never see the SQL. These guards read the repository module source
with inspect.getsource and assert the fixed conversion patterns are present and the reverted
(raw-sum) patterns are absent — so reintroducing the bug fails CI immediately, no DB required.
"""
from __future__ import annotations

import inspect
import re

from app.core import config
from app.data import signals_repository as sig
from app.ml import dataset as ds

_OVERDUE_SRC = inspect.getsource(sig.overdue_debts_for_manager)
_PAID_SRC = inspect.getsource(sig.monthly_paid)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


# Spine (dbo.[Order]/dbo.OrderItem) validity. dbo.Order/OrderItem are ~84% Deleted=1, so the old
# `o.Deleted=0` / `oi.Deleted=0` filter kept only ~16% of sales — the whole feature + backfill
# pipeline ran on a sliver. Sale validity is oi.IsValidForCurrentSale=1 (no Deleted on the spine).
# These functions JOIN the Order/OrderItem spine for sales/reorder/churn/monetary signals.
_SALES_SPINE_FUNCS = [
    sig.ubiquitous_product_ids, sig.new_clients_for_manager, sig.active_clients_for_manager,
    sig.reorder_candidates_for_manager, sig.churn_candidates_for_manager,
    sig.client_monetary, sig.client_features, sig.monthly_shipped,
    ds.client_features, ds.reorder_candidates, ds.churn_candidates, ds.active_clients,
    ds.label_reorder, ds.label_any_order, ds.label_buy_products,
]


def test_overdue_debt_wraps_total_in_euro_conversion():
    norm = _norm(_OVERDUE_SRC)
    assert "GetExchangedToEuroValue(d.Total" in norm, \
        "overdue debt must convert Debt.Total to EUR via dbo.GetExchangedToEuroValue"
    assert "ISNULL(a.CurrencyID, 2)" in norm, \
        "conversion must default missing agreement currency to EUR (2), not assume the debt is EUR"


def test_overdue_debt_does_not_raw_sum_total():
    norm = _norm(_OVERDUE_SRC)
    assert "SUM(d.Total)" not in norm, \
        "raw SUM(d.Total) treats UAH debt as EUR (~50x inflated) — must stay wrapped in conversion"
    assert re.search(r"SUM\(\s*d\.Total\s*\)", _OVERDUE_SRC) is None, \
        "no raw SUM over Debt.Total in any whitespace form"


def test_monthly_paid_converts_amount_not_euroamount():
    norm = _norm(_PAID_SRC)
    assert "GetExchangedToEuroValue(p.Amount" in norm, \
        "monthly_paid must convert the local Amount to EUR (EuroAmount is unreliable on this data)"


def test_monthly_paid_does_not_trust_euroamount():
    norm = _norm(_PAID_SRC)
    assert "p.EuroAmount" not in norm, \
        "p.EuroAmount is ~16-23x too high for UAH payments — must not be used"
    assert "SUM(p.EuroAmount)" not in norm, \
        "summing EuroAmount reintroduces the inflated-paid bug"


def test_overdue_debt_uses_parameterized_filters():
    assert ":mid" in _OVERDUE_SRC and ":asof" in _OVERDUE_SRC
    assert ":maxage" in _OVERDUE_SRC and ":minamt" in _OVERDUE_SRC


def test_sales_spine_uses_validity_flag_not_deleted():
    """Every Order/OrderItem-spine sales query must gate on oi.IsValidForCurrentSale=1 and must NOT
    use the reverted o.Deleted=0 / oi.Deleted=0 (which kept only ~16% of sales). DB-free guard."""
    for fn in _SALES_SPINE_FUNCS:
        src = _norm(inspect.getsource(fn))
        assert "IsValidForCurrentSale = 1" in src, \
            f"{fn.__module__}.{fn.__name__} must gate sales on oi.IsValidForCurrentSale = 1"
        assert not re.search(r"\bo\.Deleted\s*=\s*0", src), \
            f"{fn.__module__}.{fn.__name__} reintroduced o.Deleted=0 on the Order spine (~16% bug)"
        assert not re.search(r"\boi\.Deleted\s*=\s*0", src), \
            f"{fn.__module__}.{fn.__name__} reintroduced oi.Deleted=0 on the OrderItem spine"


# --- Hard synthetic-product exclusion (independent of the ubiquity filter) ---------------------
# The synthetic debt-injection line 25422404 ("Ввід боргів з 1С") is today excluded from
# turnover/feature signals ONLY because it clears the dynamic ubiquity threshold (~0.77 > 0.20).
# It is the ONLY product clearing that threshold — if its rolling 12-month ubiquity ever dipped
# below 0.20, client_monetary/client_features turnover would silently re-absorb 100K+ EUR of
# synthetic debt. These guards pin a HARD exclusion via settings.synthetic_product_ids that holds
# regardless of ubiquity, mirroring every other GBA service (gba-reco / gba-products).
_SYNTHETIC_ID = 25422404

# Turnover/feature queries that must hard-exclude the synthetic ids (in addition to ubiquity).
_TURNOVER_FUNCS = [sig.client_monetary, sig.client_features, sig.monthly_shipped, ds.client_features]


def test_synthetic_product_id_pinned_in_settings_default():
    """25422404 must be the DEFAULT synthetic exclusion (pinned in source), not just an env override."""
    assert _SYNTHETIC_ID in config.get_settings().synthetic_product_ids, (
        "synthetic debt-entry line 25422404 must be pinned in Settings.synthetic_product_ids"
    )
    field = config.Settings.model_fields["synthetic_product_ids"]
    assert _SYNTHETIC_ID in field.default, (
        "25422404 must be the *default* synthetic exclusion (pinned in source, not only env)"
    )


def test_excluded_helpers_union_synthetic_ids_unconditionally():
    """Both exclusion helpers must UNION settings.synthetic_product_ids so the synthetic exclusion is
    independent of the data-driven ubiquity set — it can never be lost if ubiquity stops firing."""
    for fn in (sig._excluded, ds._excluded_pids):
        src = inspect.getsource(fn)
        assert "synthetic_product_ids" in src, (
            f"{fn.__module__}.{fn.__name__} must reference settings.synthetic_product_ids so the "
            "hard exclusion does not depend on the ubiquity threshold catching 25422404"
        )
        assert re.search(r"synthetic_product_ids\s*\)?\s*\|", src), (
            f"{fn.__module__}.{fn.__name__} must UNION (|) the synthetic ids with the ubiquity set, "
            "so the pinned exclusion is unconditional"
        )


def test_turnover_queries_emit_hard_not_in_exclusion():
    """Every turnover/feature query must apply an `AND oi.ProductID NOT IN (...)` exclusion built from
    the _excluded()/_excluded_pids() set (which now always contains the synthetic ids), so 25422404
    is dropped regardless of ubiquity. DB-free guard."""
    for fn in _TURNOVER_FUNCS:
        src = _norm(inspect.getsource(fn))
        assert "_excluded" in src, (
            f"{fn.__module__}.{fn.__name__} must source its exclusion set from the _excluded helper "
            "(which unions the hard synthetic ids), not from ubiquity alone"
        )
        assert re.search(r"oi\.ProductID NOT IN", src), (
            f"{fn.__module__}.{fn.__name__} must emit `AND oi.ProductID NOT IN (...)` to hard-exclude "
            "the synthetic accounting line from turnover"
        )


def test_synthetic_exclusion_holds_when_ubiquity_does_not_fire(monkeypatch):
    """Simulate the latent failure: ubiquity returns EMPTY (25422404 dropped below 0.20). The hard
    guard must STILL exclude the synthetic id from the effective exclusion set."""
    monkeypatch.setattr(sig, "ubiquitous_product_ids", lambda pct: frozenset())
    assert _SYNTHETIC_ID in sig._excluded(), (
        "with ubiquity not firing, the synthetic id must still be excluded via the hard guard"
    )
    assert _SYNTHETIC_ID in ds._excluded_pids(), (
        "dataset path must also hard-exclude the synthetic id when ubiquity does not fire"
    )


def test_debt_and_payment_paths_keep_their_deleted_flags():
    """The debt/payment paths are correct as-is — they must keep d.Deleted=0 / p.Deleted=0 (these
    are NOT the all-Deleted=1 sales spine) and must NOT adopt the sales validity flag by mistake."""
    debt_src = _norm(_OVERDUE_SRC)
    assert "d.Deleted = 0" in debt_src, "debt path must keep d.Deleted=0"
    assert "IsValidForCurrentSale" not in debt_src, "debt path must not use the sales validity flag"
    paid_src = _norm(_PAID_SRC)
    assert "p.Deleted = 0" in paid_src, "payment path must keep p.Deleted=0"
    assert "IsValidForCurrentSale" not in paid_src, "payment path must not use the sales validity flag"
