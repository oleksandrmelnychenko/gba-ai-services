"""Truthful recommendation-source contract and owned-history regressions."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.models import (
    ProductRec,
    RecommendationResult,
    RecSource,
    RecSourceDetail,
    Segment,
)
from app.services.eval import baselines
from app.services.recommendations import als, copurchase, recommender


def _result(*recommendations: ProductRec, segment: str = "COPURCHASE") -> RecommendationResult:
    return RecommendationResult(
        customer_id=7,
        recommendations=list(recommendations),
        count=len(recommendations),
        discovery_count=sum(
            item.source == RecSource.DISCOVERY for item in recommendations
        ),
        segment=segment,
        source_history_start="2025-01-01",
        effective_start="2025-01-01",
        history_complete=True,
    )


@pytest.mark.parametrize(
    ("source", "source_detail"),
    [
        (RecSource.REPURCHASE, RecSourceDetail.SIMILAR_CLIENTS),
        (RecSource.REPURCHASE, RecSourceDetail.COPURCHASE),
        (RecSource.REPURCHASE, RecSourceDetail.GLOBAL_POPULAR),
        (RecSource.DISCOVERY, RecSourceDetail.REPURCHASE_HISTORY),
    ],
)
def test_product_rec_rejects_inconsistent_source_detail(source, source_detail):
    with pytest.raises(ValidationError, match="repurchase source"):
        ProductRec(
            product_id=1,
            score=0.5,
            rank=1,
            segment="LIGHT",
            source=source,
            source_detail=source_detail,
        )


def test_primary_recommender_labels_history_and_similar_client_paths(monkeypatch):
    settings = SimpleNamespace(
        default_top_n=2,
        repurchase_count=1,
        max_per_group=10,
        ubiquity_exclude_pct=0.99,
    )
    monkeypatch.setattr(recommender, "get_settings", lambda: settings)
    monkeypatch.setattr(recommender, "classify", lambda customer_id, as_of: Segment.LIGHT)
    monkeypatch.setattr(recommender.repo, "ubiquitous_product_ids", lambda pct: frozenset())
    monkeypatch.setattr(recommender.cache, "get_negatives", lambda customer_id: frozenset())
    monkeypatch.setattr(
        recommender.repo,
        "owned_live_product_ids",
        lambda customer_id, as_of: {101},
    )
    monkeypatch.setattr(
        recommender.repo,
        "product_frequency",
        lambda customer_id, as_of: {101: 3},
    )
    monkeypatch.setattr(
        recommender,
        "_recency_scores",
        lambda customer_id, as_of: {101: 1.0},
    )
    monkeypatch.setattr(
        recommender,
        "_similar_customers",
        lambda customer_id, as_of, region_id=None: [(22, 0.7)],
    )
    monkeypatch.setattr(
        recommender.repo,
        "collaborative_products",
        lambda similar, as_of, customer_id: {202: 0.8},
    )
    monkeypatch.setattr(
        recommender.repo,
        "in_stock_product_ids",
        lambda product_ids: set(product_ids),
    )
    monkeypatch.setattr(recommender.repo, "product_groups", lambda product_ids: {})
    monkeypatch.setattr(
        recommender.live_remap,
        "live_product_map",
        lambda product_ids: {product_id: product_id for product_id in product_ids},
    )
    monkeypatch.setattr(
        recommender.live_remap,
        "remap_recs_to_live",
        lambda recommendations: recommendations,
    )

    result = recommender.recommend(7, as_of_date="2026-07-25", top_n=2)

    assert [
        (item.source, item.source_detail) for item in result.recommendations
    ] == [
        (RecSource.REPURCHASE, RecSourceDetail.REPURCHASE_HISTORY),
        (RecSource.DISCOVERY, RecSourceDetail.SIMILAR_CLIENTS),
    ]
    assert result.source_history_start == "2025-01-01"
    assert result.effective_start == "2025-01-01"
    assert result.history_complete is True


def test_backfill_labels_copurchase_and_global_popular_paths(monkeypatch):
    copurchase_item = ProductRec(
        product_id=20,
        score=0.8,
        rank=1,
        segment="COPURCHASE",
        source=RecSource.DISCOVERY,
        source_detail=RecSourceDetail.COPURCHASE,
    )
    monkeypatch.setattr(
        copurchase,
        "recommend",
        lambda *args, **kwargs: _result(copurchase_item),
    )
    monkeypatch.setattr(
        baselines,
        "global_popular",
        lambda as_of, top_n, exclude: [30],
    )
    monkeypatch.setattr(
        recommender.live_remap,
        "live_product_map",
        lambda product_ids: {product_id: product_id for product_id in product_ids},
    )
    monkeypatch.setattr(
        recommender.repo,
        "in_stock_product_ids",
        lambda product_ids: set(product_ids),
    )

    result = recommender._backfill(
        [],
        customer_id=7,
        as_of="2026-07-25",
        top_n=2,
        segment=Segment.LIGHT,
        excl=frozenset(),
        owned_live=frozenset(),
    )

    assert [item.product_id for item in result] == [20, 30]
    assert [item.rank for item in result] == [1, 2]
    assert [item.source_detail for item in result] == [
        RecSourceDetail.COPURCHASE,
        RecSourceDetail.GLOBAL_POPULAR,
    ]
    assert all(item.source == RecSource.DISCOVERY for item in result)


def test_global_backfill_excludes_any_owned_catalog_generation(monkeypatch):
    monkeypatch.setattr(
        copurchase,
        "recommend",
        lambda *args, **kwargs: _result(),
    )
    monkeypatch.setattr(
        baselines,
        "global_popular",
        lambda as_of, top_n, exclude: [90, 91],
    )
    monkeypatch.setattr(
        recommender.live_remap,
        "live_product_map",
        lambda product_ids: {90: 900, 91: 901},
    )
    monkeypatch.setattr(
        recommender.repo,
        "in_stock_product_ids",
        lambda product_ids: set(product_ids),
    )

    result = recommender._backfill(
        [],
        customer_id=7,
        as_of="2026-07-25",
        top_n=1,
        segment=Segment.LIGHT,
        excl=frozenset(),
        owned_live=frozenset({900}),
    )

    assert [item.product_id for item in result] == [91]
    assert result[0].source_detail == RecSourceDetail.GLOBAL_POPULAR


def test_copurchase_labels_owned_and_new_products(monkeypatch):
    settings = SimpleNamespace(ubiquity_exclude_pct=0.99)
    monkeypatch.setattr(copurchase, "get_settings", lambda: settings)
    monkeypatch.setattr(copurchase.repo, "ubiquitous_product_ids", lambda pct: frozenset())
    monkeypatch.setattr(copurchase.cache, "get_negatives", lambda customer_id: frozenset())
    monkeypatch.setattr(
        copurchase.repo,
        "owned_live_product_ids",
        lambda customer_id, as_of: {1},
    )
    monkeypatch.setattr(
        copurchase,
        "_client_products_with_freq",
        lambda customer_id, as_of: {1: 2.0},
    )
    monkeypatch.setattr(
        copurchase,
        "_cooccurring_products",
        lambda seed_products, as_of: {1: 0.5, 2: 1.0},
    )
    monkeypatch.setattr(
        copurchase.repo,
        "in_stock_product_ids",
        lambda product_ids: set(product_ids),
    )
    monkeypatch.setattr(
        recommender.live_remap,
        "remap_recs_to_live",
        lambda recommendations: recommendations,
    )

    result = copurchase.recommend(
        customer_id=7,
        as_of_date="2026-07-25",
        top_n=2,
        include_owned=True,
    )

    assert [
        (item.product_id, item.source, item.source_detail)
        for item in result.recommendations
    ] == [
        (1, RecSource.REPURCHASE, RecSourceDetail.REPURCHASE_HISTORY),
        (2, RecSource.DISCOVERY, RecSourceDetail.COPURCHASE),
    ]


def test_copurchase_discovery_excludes_owned_product_after_live_remap(monkeypatch):
    settings = SimpleNamespace(ubiquity_exclude_pct=0.99)
    monkeypatch.setattr(copurchase, "get_settings", lambda: settings)
    monkeypatch.setattr(copurchase.repo, "ubiquitous_product_ids", lambda pct: frozenset())
    monkeypatch.setattr(copurchase.cache, "get_negatives", lambda customer_id: frozenset())
    monkeypatch.setattr(
        copurchase.repo,
        "owned_live_product_ids",
        lambda customer_id, as_of: {200},
    )
    monkeypatch.setattr(
        copurchase,
        "_client_products_with_freq",
        lambda customer_id, as_of: {10: 2.0},
    )
    monkeypatch.setattr(
        copurchase,
        "_cooccurring_products",
        lambda seed_products, as_of: {20: 1.0, 21: 0.8},
    )
    monkeypatch.setattr(
        copurchase.repo,
        "in_stock_product_ids",
        lambda product_ids: set(product_ids),
    )

    def _remap(recommendations):
        mapping = {20: 200, 21: 201}
        for item in recommendations:
            item.product_id = mapping[item.product_id]
        return recommendations

    monkeypatch.setattr(recommender.live_remap, "remap_recs_to_live", _remap)

    result = copurchase.recommend(
        customer_id=7,
        as_of_date="2026-07-25",
        top_n=2,
        include_owned=False,
    )

    assert [item.product_id for item in result.recommendations] == [201]
    assert result.recommendations[0].rank == 1
    assert result.recommendations[0].source_detail == RecSourceDetail.COPURCHASE


def test_als_labels_owned_and_collaborative_products(monkeypatch):
    model = SimpleNamespace(
        owned={7: {1}},
        recommend=lambda customer_id, top_n: [(1, 0.9), (2, 0.8)],
    )
    monkeypatch.setattr(als, "get_model", lambda as_of: model)

    result = als.recommend(7, "2026-07-25", top_n=2)

    assert [
        (item.product_id, item.source, item.source_detail)
        for item in result.recommendations
    ] == [
        (1, RecSource.REPURCHASE, RecSourceDetail.REPURCHASE_HISTORY),
        (2, RecSource.DISCOVERY, RecSourceDetail.SIMILAR_CLIENTS),
    ]
