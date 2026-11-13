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
from app.data import debt_snapshot
from app.data import signals_repository as sig
from app.ml import dataset as ds

_PAID_SRC = inspect.getsource(sig.monthly_paid)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


# Legacy signal guards below predate the current confirmed-invoice source.
# monthly_shipped now uses canonical confirmed ownership plus Deleted=0, verified against
# current imported lineage and real SQL fixtures in test_monthly_invoice_reference_sql.
# Do not extend this historical guard to those source-backed documentary queries.
_SALES_SPINE_FUNCS = [
    sig.ubiquitous_product_ids,
]


def test_training_dataset_reads_candidates_and_labels_through_the_serving_readers():
    """The dataset owns no sales SQL: a training row and a served task share one rule."""
    source = inspect.getsource(ds)
    assert "query(" not in source and "dbo." not in source
    for fn, reader in (
        (ds.reorder_candidates, "sig.reorder_candidates_for_manager( None,"),
        (ds.churn_candidates, "sig.churn_candidates_for_manager(None,"),
        (ds.active_clients, "sig.active_clients_for_manager( None,"),
        (ds.debt_candidates, "debt_snapshot.task_candidates("),
        (ds._bought, "sig.purchases_between("),
        (ds.label_debt_paydown, "sig.payments_between("),
    ):
        assert reader in _norm(inspect.getsource(fn)), fn.__name__
    for fn in (sig.purchases_between, sig.active_clients_for_manager,
               sig.churn_candidates_for_manager, sig.repeat_products_for_manager):
        assert "retained_purchase_cte(" in inspect.getsource(fn), fn.__name__
    paid = _norm(inspect.getsource(sig.payments_between))
    assert "p.IsCanceled = 0" in paid and "carrier.OwnershipType = 1" in paid
    assert "SUM(p.EuroAmount)" in paid and "_kyiv_midnight_utc" in paid


def test_book_scope_is_one_manager_or_the_whole_managed_book():
    assert sig._book_scope(7) == ("c.MainManagerID = :mid", {"mid": 7})
    assert sig._book_scope(None) == ("c.MainManagerID IS NOT NULL", {})


def test_dataset_labels_are_read_on_the_days_after_the_candidate_cutoff(monkeypatch):
    calls = []

    def purchases(client_ids, start, end):
        calls.append((sorted(client_ids), start, end))
        return [{"client_id": 1, "product_id": 10}, {"client_id": 2, "product_id": 30}]

    monkeypatch.setattr(sig, "purchases_between", purchases)
    assert ds.label_reorder("2026-09-01", [(1, 10), (1, 11), (2, 20)]) == {(1, 10)}
    assert ds.label_any_order("2026-09-01", [1, 2, 3]) == {1, 2}
    assert ds.label_buy_products("2026-09-01", {1: [10, 99], 2: [31]}) == {1}
    assert {call[1:] for call in calls} == {("2026-09-01", "2026-10-31")}
    window = ("2026-09-01", "2026-10-31")
    monkeypatch.setattr(sig, "payments_between",
                        lambda ids, start, end: {1: 60, 2: 49.99} if (start, end) == window else {})
    assert ds.label_debt_paydown("2026-09-01", {1: 100.0, 2: 100.0, 3: 100.0}) == {1}


def test_cross_sell_pool_is_the_generator_cap_per_manager(monkeypatch):
    rows = [{"client_id": cid, "manager_id": 1 if cid < 10 else 2, "client_net_uid": f"u{cid}"}
            for cid in (1, 2, 3, 11, 12)]
    monkeypatch.setattr(sig, "active_clients_for_manager", lambda *a, **k: rows)
    monkeypatch.setattr(sig, "client_features",
                        lambda ids, asof: {cid: {"monetary": float(cid % 10)} for cid in ids})
    settings = config.get_settings().model_copy(update={"cross_sell_max_clients": 2})
    monkeypatch.setattr(ds, "get_settings", lambda: settings)
    assert [c["client_id"] for c in ds.active_clients("2026-09-01")] == [3, 2, 12, 11]


# Payment conversion semantics are executed against fixture rows in
# test_payment_target_filter.py. The former EuroAmount prohibition was based on
# pre-fix sync data and is invalid after the current document-conversion sync.


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
# The 1C refresh re-mints «Ввід боргів», so its ID must be resolved from the current Product row.
# The hard exclusion must remain independent of the data-driven ubiquity threshold.



def test_synthetic_product_id_is_resolved_dynamically():
    field = config.Settings.model_fields["synthetic_product_ids"]
    assert field.default == frozenset()
    src = inspect.getsource(sig.synthetic_product_ids)
    assert "Ввід боргів" in src
    assert "Deleted = 0" in src
    assert "ORDER BY ID DESC" in src


def test_excluded_helpers_union_synthetic_ids_unconditionally():
    """Both exclusion helpers must UNION settings.synthetic_product_ids so the synthetic exclusion is
    independent of the data-driven ubiquity set — it can never be lost if ubiquity stops firing."""
    for fn in (sig._excluded, ds._excluded_pids):
        src = inspect.getsource(fn)
        assert "synthetic_product_ids" in src, (
            f"{fn.__module__}.{fn.__name__} must reference settings.synthetic_product_ids so the "
            "hard exclusion does not depend on the ubiquity threshold catching 25422404"
        )
        assert re.search(r"synthetic_product_ids\(\)\s*\)?\s*\|", src), (
            f"{fn.__module__}.{fn.__name__} must UNION (|) the synthetic ids with the ubiquity set, "
            "so the pinned exclusion is unconditional"
        )


def test_client_features_share_the_turnover_column_population(monkeypatch):
    """monetary is «Оборот (12м)»: retained confirmed purchases, synthetic lines excluded, and the
    training dataset reads the very same function."""
    calls = []
    monkeypatch.setattr(sig, "query", lambda sql, params=None: calls.append((_norm(sql), params)) or [
        {"client_id": 11, "monetary": 120.5, "order_count": 3, "recency_days": 4}])
    monkeypatch.setattr(sig, "_excluded", lambda as_of: frozenset({42}))
    features = ds.client_features([11, 12], "2026-07-25")
    sql, params = calls[0]
    assert features == {11: {"monetary": 120.5, "order_count": 3, "recency_days": 4},
                        12: {"monetary": 0.0, "order_count": 0, "recency_days": None}}
    assert "s.ChangedToInvoice IS NOT NULL" in sql and "s.Deleted = 0 AND s.IsMerged = 0" in sql
    assert "oi.Qty - COALESCE(oi.ReturnedQty, 0)" in sql
    assert "SUM(turnover_eur) AS monetary" in sql and "COUNT(DISTINCT order_id) AS order_count" in sql
    assert "FROM retained_purchases WHERE product_id NOT IN (:x0)" in sql and params["x0"] == 42
    assert "c.ID IN (:c0,:c1)" in sql
    overview = inspect.getsource(sig.clients_overview_for_manager)
    assert "retained_purchase_cte" in overview and "turnover_eur" in overview


def test_synthetic_exclusion_holds_when_ubiquity_does_not_fire(monkeypatch):
    """Simulate the latent failure: ubiquity returns EMPTY. The hard
    guard must STILL exclude the synthetic id from the effective exclusion set."""
    dynamic_id = 42_424_242
    monkeypatch.setattr(sig, "synthetic_product_ids", lambda: frozenset({dynamic_id}))
    monkeypatch.setattr(sig, "ubiquitous_product_ids", lambda pct, as_of: frozenset())
    assert dynamic_id in sig._excluded("2026-07-25"), (
        "with ubiquity not firing, the synthetic id must still be excluded via the hard guard"
    )
    assert dynamic_id in ds._excluded_pids("2026-07-25"), (
        "dataset path must also hard-exclude the synthetic id when ubiquity does not fire"
    )


def test_debt_and_payment_paths_keep_their_deleted_flags():
    """The debt/payment paths are correct as-is — they must keep d.Deleted=0 / p.Deleted=0 (these
    are NOT the all-Deleted=1 sales spine) and must NOT adopt the sales validity flag by mistake."""
    debt_src = _norm(debt_snapshot._DOCUMENTS_SQL)
    assert "d.Deleted = 0" in debt_src, "debt path must keep d.Deleted=0"
    assert "IsValidForCurrentSale" not in debt_src, "debt path must not use the sales validity flag"
    paid_src = _norm(_PAID_SRC)
    assert "p.Deleted = 0" in paid_src, "payment path must keep p.Deleted=0"
    assert "IsValidForCurrentSale" not in paid_src, "payment path must not use the sales validity flag"


def test_retained_seller_queries_delegate_to_canonical_source():
    # Current imported sales are not the historical all-deleted calibration snapshot.
    # Executed SQL fixtures validate cancellation, merge, returns and current source provenance.
    for fn in (sig.new_clients_for_manager, sig.clients_overview_for_manager,
               sig.active_clients_for_manager, sig.repeat_products_for_manager,
               sig.churn_candidates_for_manager):
        assert "retained_purchase_cte(" in inspect.getsource(fn)
    assert "repeat_products_for_manager(" in inspect.getsource(sig.reorder_candidates_for_manager)
