"""Fast unit tests — no DB/Redis required (those are integration, run separately)."""
from __future__ import annotations

from app.data.db import in_clause
from app.domain.models import ProductRec, RecommendationResult, RecSource, Segment
from scripts import smoke_test


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


def _smoke_result(recs: list[ProductRec], *, count: int | None = None) -> RecommendationResult:
    return RecommendationResult(
        customer_id=1,
        recommendations=recs,
        count=len(recs) if count is None else count,
        discovery_count=0,
        segment="LIGHT",
        latency_ms=1.0,
        model_version="test-model",
    )


def test_smoke_validator_accepts_valid_contract():
    result = _smoke_result([
        ProductRec(product_id=5, score=0.9, rank=1, segment="LIGHT", source=RecSource.REPURCHASE),
    ])

    assert smoke_test.validate_result(1, result, top_n=10) == []


def test_smoke_validator_rejects_empty_result():
    errors = smoke_test.validate_result(1, _smoke_result([]), top_n=10)

    assert any("empty recommendation list" in err for err in errors)


def test_smoke_validator_rejects_duplicate_products_and_bad_ranks():
    result = _smoke_result([
        ProductRec(product_id=5, score=0.9, rank=2, segment="LIGHT", source=RecSource.REPURCHASE),
        ProductRec(product_id=5, score=0.8, rank=2, segment="LIGHT", source=RecSource.DISCOVERY),
    ])

    errors = smoke_test.validate_result(1, result, top_n=10)

    assert any("duplicate product ids" in err for err in errors)
    assert any("ranks=" in err for err in errors)
