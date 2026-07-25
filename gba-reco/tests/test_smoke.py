"""Fast unit tests — no DB/Redis required (those are integration, run separately)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.data.db import in_clause
from app.domain.models import (
    ProductRec,
    RecommendationResult,
    RecSource,
    RecSourceDetail,
    Segment,
)


def test_in_clause_parameterized():
    ph, params = in_clause("p", [10, 20, 30])
    assert ph == "(:p0,:p1,:p2)"
    assert params == {"p0": 10, "p1": 20, "p2": 30}


def test_in_clause_empty_is_safe():
    ph, params = in_clause("p", [])
    assert ph == "(NULL)"
    assert params == {}


def test_segments_exist():
    assert Segment.HEAVY.value == "HEAVY"
    assert {s.value for s in Segment} == {
        "HEAVY", "REGULAR_CONSISTENT", "REGULAR_EXPLORATORY", "LIGHT"
    }


def test_result_contract_shape():
    r = RecommendationResult(
        customer_id=1,
        recommendations=[ProductRec(product_id=5, score=0.9, rank=1, segment="LIGHT",
                                    source=RecSource.REPURCHASE,
                                    source_detail=RecSourceDetail.REPURCHASE_HISTORY)],
        count=1, discovery_count=0, segment="LIGHT",
        source_history_start="2025-01-01",
        effective_start="2025-01-01",
        history_complete=True,
    )
    dumped = r.model_dump(mode="json")
    # contract fields the .NET DTO expects
    for field in ("customer_id", "recommendations", "count", "discovery_count",
                  "precision_estimate", "latency_ms", "cached"):
        assert field in dumped
    assert dumped["recommendations"][0]["source"] == "repurchase"
    assert dumped["recommendations"][0]["source_detail"] == "repurchase_history"
    assert dumped["source_history_start"] == "2025-01-01"
    assert dumped["effective_start"] == "2025-01-01"
    assert dumped["history_complete"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"count": 2},
        {"discovery_count": 1},
        {
            "recommendations": [
                ProductRec(
                    product_id=5,
                    score=0.9,
                    rank=1,
                    segment="LIGHT",
                    source=RecSource.REPURCHASE,
                    source_detail=RecSourceDetail.REPURCHASE_HISTORY,
                ),
                ProductRec(
                    product_id=5,
                    score=0.8,
                    rank=2,
                    segment="LIGHT",
                    source=RecSource.REPURCHASE,
                    source_detail=RecSourceDetail.REPURCHASE_HISTORY,
                ),
            ],
            "count": 2,
        },
    ],
)
def test_result_contract_rejects_mismatched_counts_and_duplicate_products(overrides):
    payload = {
        "customer_id": 1,
        "recommendations": [
            ProductRec(
                product_id=5,
                score=0.9,
                rank=1,
                segment="LIGHT",
                source=RecSource.REPURCHASE,
                source_detail=RecSourceDetail.REPURCHASE_HISTORY,
            )
        ],
        "count": 1,
        "discovery_count": 0,
        "segment": "LIGHT",
        "source_history_start": "2025-01-01",
        "effective_start": "2025-01-01",
        "history_complete": True,
        **overrides,
    }
    with pytest.raises(ValidationError):
        RecommendationResult(**payload)


def test_result_contract_rejects_row_segment_drift():
    with pytest.raises(ValidationError, match="response segment"):
        RecommendationResult(
            customer_id=1,
            recommendations=[
                ProductRec(
                    product_id=5,
                    score=0.9,
                    rank=1,
                    segment="HEAVY",
                    source=RecSource.REPURCHASE,
                    source_detail=RecSourceDetail.REPURCHASE_HISTORY,
                )
            ],
            count=1,
            discovery_count=0,
            segment="LIGHT",
            source_history_start="2025-01-01",
            effective_start="2025-01-01",
            history_complete=True,
        )


def test_cache_key_stable_and_versioned():
    from app.data.cache import make_key
    k1 = make_key(123, "2026-06-01", 25, True)
    k2 = make_key(123, "2026-06-01", 25, True)
    assert k1 == k2
    assert k1.startswith("reco:")
    assert ":123:" in k1
