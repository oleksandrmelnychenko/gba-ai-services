from __future__ import annotations

import pytest

from app.data import solvency_repository as repo


@pytest.mark.parametrize(
    ("returned_qty", "expected_rate"),
    [
        (1.0, 0.2),  # partial return from a sold Qty=5 line
        (3.0, 0.6),  # multiple active SaleReturnItem rows: 1 + 2
    ],
)
def test_return_qty_rate_preserves_canonical_aggregate(
    monkeypatch, returned_qty, expected_rate
):
    monkeypatch.setattr(
        repo,
        "query",
        lambda *_args, **_kwargs: [{"return_qty": returned_qty, "sold_qty": 5.0}],
    )
    monkeypatch.setattr(repo, "_synthetic_not_in", lambda: ("(:synthetic0)", {"synthetic0": 9}))

    assert repo.return_qty_rate(42, "2026-07-25", 12) == expected_rate
