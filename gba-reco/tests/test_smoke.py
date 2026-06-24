"""Fast unit tests — no DB/Redis required (those are integration, run separately)."""
from __future__ import annotations

from app.data.db import in_clause
from app.domain.models import ProductRec, RecommendationResult, RecSource, Segment


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
                                    source=RecSource.REPURCHASE)],
        count=1, discovery_count=0, segment="LIGHT",
    )
    dumped = r.model_dump(mode="json")
    # contract fields the .NET DTO expects
    for field in ("customer_id", "recommendations", "count", "discovery_count",
                  "precision_estimate", "latency_ms", "cached"):
        assert field in dumped
    assert dumped["recommendations"][0]["source"] == "repurchase"


def test_cache_key_stable_and_versioned():
    from app.data.cache import make_key
    k1 = make_key(123, "2026-06-01", 25, True)
    k2 = make_key(123, "2026-06-01", 25, True)
    assert k1 == k2
    assert k1.startswith("reco:")
    assert ":123:" in k1
