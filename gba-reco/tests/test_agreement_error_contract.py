"""Public error distinctions without relaxing the authoritative active-agreement guard."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.core.config import get_settings
from app.data import cache
from app.data import sales_repository as repo
from app.domain.models import ProductRec, RecommendationResult, RecSource, RecSourceDetail
from app.services.recommendations import copurchase, recommender

ACTIVE = "8a375ce0-47fe-4804-b1e6-a7c8787dd880"
UNAVAILABLE = "f2d9fa15-0e65-40df-83ff-70bbc5349272"
AS_OF = "2026-10-01"
HEADERS = {"X-Internal-Api-Key": get_settings().internal_api_key}


@pytest.fixture
def local_facts(monkeypatch):
    monkeypatch.setattr(repo, "client_exists", lambda cid: cid == 7)
    monkeypatch.setattr(repo, "active_agreements", lambda cid: [ACTIVE] if cid == 7 else [])
    monkeypatch.setattr(repo, "active_product_ids", lambda ids: set(ids) - {999})
    monkeypatch.setattr(cache, "get", lambda key: pytest.fail("use_cache=false must not read cache"))
    monkeypatch.setattr(cache, "set", lambda *args, **kwargs: pytest.fail("must not write cache"))


def request(endpoint, **overrides):
    body = {"customer_id": 7, "client_agreement_net_id": ACTIVE,
            "as_of_date": AS_OF, "use_cache": False, **overrides}
    return TestClient(app, raise_server_exceptions=False).post(endpoint, json=body, headers=HEADERS)


@pytest.mark.parametrize("endpoint", ["/recommend", "/recommend/copurchase"])
@pytest.mark.parametrize("case", ["inactive", "foreign", "missing"])
def test_unavailable_agreement_is_one_nondisclosing_typed_error(local_facts, monkeypatch, endpoint, case):
    # All three failures have the same authoritative fact: no requested UID in this
    # customer's active agreements. The response must not disclose which reason applies.
    observed = []
    monkeypatch.setattr(repo, "active_agreements", lambda cid: observed.append(cid) or [ACTIVE])
    monkeypatch.setattr(recommender, "recommend", lambda **kw: pytest.fail("must not fall back"))
    monkeypatch.setattr(copurchase, "recommend", lambda *a, **kw: pytest.fail("must not fall back"))
    response = request(endpoint, client_agreement_net_id=UNAVAILABLE)
    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "client_agreement_unavailable"}}
    assert observed == [7]
    assert UNAVAILABLE not in response.text and case not in response.text
    if os.environ.get("GBA_AGREEMENT_WIRE_DIR"):
        target = Path(os.environ["GBA_AGREEMENT_WIRE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "reco-agreement-unavailable.json").write_text(json.dumps(response.json()))


@pytest.mark.parametrize("endpoint", ["/recommend", "/recommend/copurchase"])
def test_missing_customer_is_not_an_agreement_failure(local_facts, endpoint):
    response = request(endpoint, customer_id=8)
    assert response.status_code == 404
    assert "client_agreement_unavailable" not in response.text


def test_missing_seed_product_is_not_an_agreement_failure(local_facts):
    response = request("/recommend/copurchase", product_ids=[999])
    assert response.status_code == 404
    assert response.json() == {"detail": {"message": "seed products were not found", "product_ids": [999]}}


@pytest.mark.parametrize("endpoint", ["/recommend", "/recommend/copurchase"])
@pytest.mark.parametrize("count", [0, 1])
def test_active_agreement_keeps_success_empty_and_nonempty(local_facts, monkeypatch, endpoint, count):
    is_cross_sell = endpoint.endswith("copurchase")
    segment = "COPURCHASE" if is_cross_sell else "LIGHT"
    calls = []

    def result(*args, **kwargs):
        calls.append(kwargs["client_agreement_net_id"])
        items = [ProductRec(product_id=101, score=0.5, rank=1, segment=segment,
                            source=RecSource.DISCOVERY if is_cross_sell else RecSource.REPURCHASE,
                            source_detail=RecSourceDetail.COPURCHASE if is_cross_sell
                            else RecSourceDetail.REPURCHASE_HISTORY)] if count else []
        return RecommendationResult(customer_id=7, client_agreement_net_id=ACTIVE,
                                    recommendations=items, count=count,
                                    discovery_count=count if is_cross_sell else 0, segment=segment,
                                    as_of_date=AS_OF, source_history_start="2025-01-01",
                                    effective_start="2025-01-01", history_complete=True)

    monkeypatch.setattr(copurchase if is_cross_sell else recommender, "recommend", result)
    response = request(endpoint)
    assert response.status_code == 200
    assert response.json()["count"] == count
    assert response.json()["client_agreement_net_id"] == ACTIVE
    assert calls == [ACTIVE]


@pytest.mark.parametrize("endpoint", ["/recommend", "/recommend/copurchase"])
def test_calculation_failure_is_not_an_agreement_failure(local_facts, monkeypatch, endpoint):
    def fail(*args, **kwargs):
        raise RuntimeError("private diagnostic must not appear")

    monkeypatch.setattr(copurchase if endpoint.endswith("copurchase") else recommender, "recommend", fail)
    response = request(endpoint)
    assert response.status_code == 500
    assert "client_agreement_unavailable" not in response.text
    assert "private diagnostic" not in response.text
