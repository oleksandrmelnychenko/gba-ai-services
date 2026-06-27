"""Fast unit tests — no DB/Redis required (those are integration, run separately)."""
from __future__ import annotations

from app.core.config import Settings
from app.data.db import in_clause
from app.domain.models import (
    CapType,
    DebtLoadSource,
    Rating,
    RetailPaymentStatusType,
    SalePaymentStatusType,
    SolvencyScore,
    SubFactor,
    SubFactors,
)


def test_in_clause_parameterized():
    ph, params = in_clause("st", [0, 3])
    assert ph == "(:st0,:st1)"
    assert params == {"st0": 0, "st1": 3}


def test_in_clause_empty_is_safe():
    ph, params = in_clause("st", [])
    assert ph == "(NULL)"
    assert params == {}


def test_payment_enums_distinct_per_sale_type():
    # Regular sales
    assert SalePaymentStatusType.Paid == 1
    assert SalePaymentStatusType.Refund == 4
    # Retail collision: Paid=4 == SalePaymentStatusType.Refund value, so never conflate.
    assert RetailPaymentStatusType.Paid == 4
    assert RetailPaymentStatusType.PartialPaid == SalePaymentStatusType.PartialPaid == 3
    assert RetailPaymentStatusType.Paid.value == SalePaymentStatusType.Refund.value


def test_rating_bands_exist():
    assert {r.value for r in Rating} == {"A", "B", "C", "D"}


def test_config_solvency_defaults():
    s = Settings(db_password="x")
    assert s.api_port == 8003
    assert s.redis_db == 2
    assert s.model_version == "creditscore-v3"
    assert s.window_months == 12
    assert s.synthetic_line_product_ids == {25422404}
    assert s.synthetic_line_product_id == 25422404  # back-compat single-value accessor


def test_synthetic_ids_coerce_from_env_forms():
    assert Settings(db_password="x", synthetic_line_product_id="25422404").synthetic_line_product_ids \
        == {25422404}
    assert Settings(db_password="x", synthetic_line_product_id="25422404, 99999") \
        .synthetic_line_product_ids == {25422404, 99999}
    assert Settings(db_password="x", synthetic_line_product_ids="[25422404, 88888]") \
        .synthetic_line_product_ids == {25422404, 88888}


def test_fx_snapshot_resolution():
    s = Settings(db_password="x", fx_snapshot_date="2026-01-01")
    assert s.resolve_fx_date("2026-06-01") == "2026-01-01"   # pinned overrides as_of
    s2 = Settings(db_password="x")
    assert s2.resolve_fx_date("2026-06-01") == "2026-06-01"  # falls back to as_of


def test_score_contract_shape():
    sf = SubFactors(
        discipline=SubFactor(value=0.9, points=31.5, weight=0.35),
        debt_load=SubFactor(value=0.8, points=20.0, weight=0.25),
        activity=SubFactor(value=0.7, points=14.0, weight=0.20),
        tenure=SubFactor(value=1.0, points=10.0, weight=0.10),
        return_quality=SubFactor(value=1.0, points=10.0, weight=0.10),
    )
    score = SolvencyScore(
        client_id=42, score=85, rating=Rating.A, sub_factors=sf,
        caps_applied=[], debt_load_source=DebtLoadSource.LIVE_PROXY, raw_score=85.5,
    )
    dumped = score.model_dump(mode="json")
    for field in ("client_id", "score", "rating", "sub_factors", "caps_applied",
                  "debt_load_source", "raw_score", "model_version"):
        assert field in dumped
    assert dumped["sub_factors"]["discipline"]["value"] == 0.9
    assert dumped["sub_factors"]["discipline"]["points"] == 31.5
    assert dumped["debt_load_source"] == "live_proxy"
    assert dumped["model_version"] == "creditscore-v3"


def test_caps_serialize_as_strings():
    sf = SubFactors(
        discipline=SubFactor(value=0.0, points=0.0, weight=0.35),
        debt_load=SubFactor(value=0.0, points=0.0, weight=0.25),
        activity=SubFactor(value=0.0, points=0.0, weight=0.20),
        tenure=SubFactor(value=0.0, points=0.0, weight=0.10),
        return_quality=SubFactor(value=0.0, points=0.0, weight=0.10),
    )
    score = SolvencyScore(
        client_id=1, score=20, rating=Rating.D, sub_factors=sf,
        caps_applied=[CapType.UTILIZATION_HARD_40, CapType.BLOCKED_HALF],
        debt_load_source=DebtLoadSource.DEBT_TABLE, raw_score=72.0,
    )
    dumped = score.model_dump(mode="json")
    assert dumped["caps_applied"] == ["utilization_hard_40", "blocked_half"]


def test_cache_key_stable_and_versioned():
    from app.data.cache import make_charts_key, make_key
    k1 = make_key(123, "2026-06-01", 12)
    k2 = make_key(123, "2026-06-01", 12)
    assert k1 == k2
    assert k1.startswith("solv:")
    assert ":123:" in k1
    assert "creditscore-v3" in k1
    ck = make_charts_key(123, "2026-06-01", 12)
    assert ck.startswith("solvchart:")
