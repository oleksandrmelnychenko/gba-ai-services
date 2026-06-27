"""Feedback endpoint + copurchase cache key — pure (cache monkeypatched, no Redis/DB)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import main


def _headers() -> dict[str, str]:
    if not main.settings.internal_api_key:
        return {}
    return {"X-Internal-Api-Key": main.settings.internal_api_key}


def test_copurchase_key_stable_and_versioned():
    from app.data.cache import make_copurchase_key
    k1 = make_copurchase_key(123, "2026-06-01", 25)
    k2 = make_copurchase_key(123, "2026-06-01", 25)
    assert k1 == k2
    assert k1.startswith("copurchase:")
    assert ":123:" in k1


def test_feedback_endpoint_records_negatives(monkeypatch):
    captured = {}

    def _add(cid, pids, ttl):
        captured.update(cid=cid, pids=list(pids), ttl=ttl)
        return len(pids)
    monkeypatch.setattr(main.cache, "add_negatives", _add)
    monkeypatch.setattr(main.cache, "invalidate_copurchase",
                        lambda cid: captured.update(invalidated=cid) or 1)
    monkeypatch.setattr(main.cache, "get_negatives", lambda cid: frozenset({11, 12, 13}))

    client = TestClient(main.app)
    resp = client.post(
        "/feedback",
        json={"customer_id": 5, "product_ids": [11, 12, 13]},
        headers=_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"customer_id": 5, "added": 3, "total_negatives": 3}
    assert captured["cid"] == 5 and captured["pids"] == [11, 12, 13]
    assert captured["invalidated"] == 5         # cache invalidated so exclusion takes effect next call


def test_feedback_endpoint_rejects_empty_products():
    client = TestClient(main.app)
    resp = client.post(
        "/feedback",
        json={"customer_id": 5, "product_ids": []},
        headers=_headers(),
    )
    assert resp.status_code == 422        # min_length=1 enforced by the request model
