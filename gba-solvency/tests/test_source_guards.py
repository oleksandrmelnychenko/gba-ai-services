"""Always-on source guards — no DB, run in normal pytest/CI.

These pin the just-fixed correctness bugs by asserting the repository SQL itself. The mocked
unit tests stayed green while these bugs shipped (only live smoke caught them), so these guards
inspect the real query source so a reintroduction fails CI immediately.

Guarded regressions:
  - turnover_eur / turnover_eur_by_currency / monthly_turnover_series must NOT wrap
    "oi.Qty * oi.PricePerItem" in dbo.GetExchangedToEuroValue (the x52 over-conversion: UAH
    turnover wrongly divided by the FX rate -- PricePerItem is already EUR).
  - overdue_amount_eur MUST still EUR-convert Debt.Total (that conversion is correct and stays).
  - client_exists guard must exist (no fabricated score for a phantom client).
  - overdue_amount_eur / open_unpaid_stats / open_unpaid_aging_buckets must anchor on :asof,
    never the non-deterministic GETDATE()/GETUTCDATE() (those appear only in docstrings).
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from app.data import solvency_repository as repo
from app.risk import dataset

_TURNOVER_FNS = (
    repo.turnover_eur,
    repo.turnover_eur_by_currency,
    repo.monthly_turnover_series,
)
_ASOF_FNS = (
    repo.overdue_amount_eur,
    repo.open_unpaid_stats,
    repo.open_unpaid_aging_buckets,
)
_TRANSACTION_HISTORY_FNS = (
    repo.payment_status_counts,
    repo.open_unpaid_stats,
    repo.open_unpaid_aging_buckets,
    repo.total_sales_count,
    repo.turnover_eur,
    repo.turnover_eur_by_currency,
    repo.activity_stats,
    repo.return_qty_rate,
    repo.monthly_turnover_series,
    dataset.feat_debt_trajectory,
    dataset.feat_rfm,
    dataset.feat_returns,
    dataset.features_one,
    dataset.features_many,
)


def _sql_body(fn) -> str:
    """Source of fn with its docstring removed.

    The docstrings intentionally mention GETDATE()/GETUTCDATE() ("reproduces ... when :asof is
    today"), so absence checks must run against the executable body, not the raw source.
    """
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    func = tree.body[0]
    body = func.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.get_source_segment(src, node) for node in body)


def test_turnover_functions_do_not_euro_convert_priceperitem():
    for fn in _TURNOVER_FNS:
        body = _sql_body(fn)
        assert "CAST(oi.Qty AS decimal(19, 6))" in body, fn.__name__
        assert "CAST(oi.PricePerItem AS decimal(19, 6))" in body, fn.__name__
        assert "GetExchangedToEuroValue(oi" not in body, (
            f"{fn.__name__} re-introduced the x52 over-conversion of already-EUR PricePerItem"
        )
        assert "GetExchangedToEuroValue(oi.Qty" not in body, fn.__name__


def test_overdue_still_euro_converts_debt_total():
    body = _sql_body(repo.overdue_amount_eur)
    assert "GetExchangedToEuroValue(" in body and "d.Total" in body, (
        "overdue_amount_eur must keep converting Debt.Total to EUR"
    )


def test_client_exists_guard_present():
    assert hasattr(repo, "client_exists")
    src = inspect.getsource(repo.client_exists)
    assert "FROM dbo.Client" in src
    assert "WHERE ID = :cid" in src


def test_asof_anchored_never_clock_calls():
    for fn in _ASOF_FNS:
        body = _sql_body(fn)
        assert ":asof" in body, fn.__name__
        assert "GETDATE()" not in body, (
            f"{fn.__name__} must anchor on :asof, not the non-deterministic GETDATE()"
        )
        assert "GETUTCDATE()" not in body, (
            f"{fn.__name__} must anchor on :asof, not the non-deterministic GETUTCDATE()"
        )


def test_all_return_feature_paths_sum_canonical_salereturnitem_qty():
    functions = (
        repo.return_qty_rate,
        dataset.feat_returns,
        dataset.features_one,
        dataset.features_many,
    )
    for fn in functions:
        body = _sql_body(fn)
        assert "SUM(sri.Qty) AS returned_qty" in body, fn.__name__
        assert "sri.SaleReturnID, sri.OrderItemID" in body, fn.__name__
        assert "MAX(oi.Qty)" not in body, (
            f"{fn.__name__} replaced partial/multiple returns with original sold quantity"
        )
        assert "oi.ReturnedQty" not in body, fn.__name__


def test_every_transaction_history_path_uses_the_shared_source_floor():
    for fn in _TRANSACTION_HISTORY_FNS:
        body = _sql_body(fn)
        assert ":history_start" in body, (
            f"{fn.__name__} can read or fabricate behavior before SOURCE_HISTORY_START_DATE"
        )
        assert "2000-01-01" not in body, fn.__name__
        assert "1980-01-01" not in body, fn.__name__
