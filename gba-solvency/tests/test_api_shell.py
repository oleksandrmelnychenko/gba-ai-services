"""API shell tests — no DB/Redis; the scoring service is monkeypatched."""
from __future__ import annotations

import sys
import types

from fastapi.testclient import TestClient

from app.api import main
from app.domain.models import (
    DebtLoadSource,
    Rating,
    SolvencyScore,
    SubFactor,
    SubFactors,
)


def _headers() -> dict[str, str]:
    if not main.settings.internal_api_key:
        return {}
    return {"X-Internal-Api-Key": main.settings.internal_api_key}


def _fake_score(client_id: int) -> SolvencyScore:
    sf = SubFactors(
        discipline=SubFactor(value=0.9, points=31.5, weight=0.35),
        debt_load=SubFactor(value=0.8, points=20.0, weight=0.25),
        activity=SubFactor(value=0.7, points=14.0, weight=0.20),
        tenure=SubFactor(value=1.0, points=10.0, weight=0.10),
        return_quality=SubFactor(value=1.0, points=10.0, weight=0.10),
    )
    return SolvencyScore(
        client_id=client_id, score=85, rating=Rating.A, sub_factors=sf,
        caps_applied=[], debt_load_source=DebtLoadSource.LIVE_PROXY, raw_score=85.5,
    )


def _install_fake_service(monkeypatch):
    mod = types.ModuleType("app.services.solvency.service")

    def score_client(client_id=None, client_net_uid=None, **_):
        return _fake_score(client_id or 1)

    mod.score_client = score_client
    monkeypatch.setitem(sys.modules, "app.services.solvency.service", mod)


def test_metrics_endpoint():
    client = TestClient(main.app)
    resp = client.get("/metrics", headers=_headers())
    assert resp.status_code == 200
    assert "uptime_seconds" in resp.json()


def test_score_requires_identifier():
    client = TestClient(main.app)
    resp = client.post("/score", json={}, headers=_headers())
    assert resp.status_code == 422


def test_score_with_fake_service(monkeypatch):
    _install_fake_service(monkeypatch)
    client = TestClient(main.app)
    resp = client.post("/score", json={"client_id": 7}, headers=_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == 7
    assert body["rating"] == "A"
    assert body["debt_load_source"] == "live_proxy"


def test_score_batch_isolates_errors(monkeypatch):
    mod = types.ModuleType("app.services.solvency.service")

    def score_client(client_id=None, **_):
        if client_id == 99:
            raise ValueError("boom")
        return _fake_score(client_id)

    def score_batch(client_ids, **_):
        results, errors = [], []
        for cid in client_ids:
            try:
                results.append(score_client(client_id=cid))
            except Exception as exc:  # noqa: BLE001
                errors.append({"client_id": cid, "error": str(exc)})
        return results, errors

    mod.score_client = score_client
    mod.score_batch = score_batch
    monkeypatch.setitem(sys.modules, "app.services.solvency.service", mod)

    client = TestClient(main.app)
    resp = client.post(
        "/score/batch",
        json={"client_ids": [1, 99, 2]},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["failed"] == 1
    assert body["errors"][0]["client_id"] == 99
