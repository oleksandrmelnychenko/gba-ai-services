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
    monkeypatch.setattr(main.cache, "get_negative_vendor_codes",
                        lambda cid: frozenset({"VC-11", "VC-12", "VC-13"}))
    monkeypatch.setattr(main.repo, "client_exists", lambda cid: True)
    monkeypatch.setattr(main.repo, "active_product_ids", lambda pids: set(pids))

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


def test_feedback_store_roundtrip(monkeypatch, tmp_path):
    from app.data import feedback_store

    store_file = tmp_path / "negatives.jsonl"
    monkeypatch.setattr(feedback_store, "_store_path", lambda: store_file)

    feedback_store.append("AAAA-1111", ["VC-1", "VC-2"])
    feedback_store.append("aaaa-1111", ["VC-2", "VC-3"])
    feedback_store.append("bbbb-2222", ["VC-9"])
    store_file.write_text(store_file.read_text(encoding="utf-8") + "{broken json\n",
                          encoding="utf-8")

    journal = feedback_store.load()
    assert journal == {"aaaa-1111": {"VC-1", "VC-2", "VC-3"}, "bbbb-2222": {"VC-9"}}


def test_feedback_store_replay_rearms_redis(monkeypatch, tmp_path):
    from app.data import cache, feedback_store

    store_file = tmp_path / "negatives.jsonl"
    monkeypatch.setattr(feedback_store, "_store_path", lambda: store_file)
    feedback_store.append("cccc-3333", ["VC-5", "VC-6"])

    calls = []

    class _FakeRedis:
        def sadd(self, key, *members):
            calls.append(("sadd", key, set(members)))
            return len(members)

        def expire(self, key, ttl):
            calls.append(("expire", key, ttl))
            return True

    monkeypatch.setattr(cache, "_get_client", lambda: _FakeRedis())
    replayed = feedback_store.replay_into_redis()

    assert replayed == 1
    assert ("sadd", "reco:neg:cccc-3333", {"VC-5", "VC-6"}) in calls
    assert any(op == "expire" and key == "reco:neg:cccc-3333" for op, key, _ in calls)
