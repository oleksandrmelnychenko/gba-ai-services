from copy import deepcopy

from app.api import main


def test_ranked_metadata_is_batched_and_preserves_order_and_financial_values(monkeypatch):
    calls = []

    def metadata(ids, as_of):
        calls.append((ids, as_of))
        return {
            10: {"product_id": 10, "name": "Гальмівний диск", "vendor_code": "BR-10"},
            20: {"product_id": 20, "name": "Фільтр", "vendor_code": "FL-20"},
        }

    monkeypatch.setattr(main.sig, "product_meta", metadata)
    groups = {
        "leaders": [{"product_id": 20, "margin_eur": 42.5}],
        "laggards": [{"product_id": 10, "margin_eur": -15.25}],
        "negative": [{"product_id": 10, "margin_eur": -15.25}],
    }
    before = deepcopy(groups)
    result = main._attach_ranked_meta(groups, "2026-09-07")

    assert calls == [([10, 20], "2026-09-07")]
    assert result["leaders"][0]["name"] == "Фільтр"
    assert result["laggards"][0]["vendor_code"] == "BR-10"
    assert result["negative"][0]["margin_eur"] == -15.25
    assert result["leaders"][0]["margin_eur"] == 42.5
    assert groups == before


def test_stock_metadata_is_added_after_limiting_without_mutating_cached_snapshot(monkeypatch):
    snapshot = {"rows": [{"product_id": 10, "eur_value": 25}, {"product_id": 20, "eur_value": 10}]}
    monkeypatch.setattr(main.cache, "get", lambda _key: snapshot)
    monkeypatch.setattr(main, "_stock_cache_compatible", lambda _value: True)
    monkeypatch.setattr(main.sig, "product_meta", lambda ids, _date: {
        pid: {"product_id": pid, "name": "Диск", "vendor_code": "BR-10"} for pid in ids
    })

    result = main.assortment_stock(limit=1)

    assert len(result["rows"]) == 1
    assert result["rows"][0]["name"] == "Диск"
    assert result["rows"][0]["eur_value"] == 25
    assert len(snapshot["rows"]) == 2
    assert "name" not in snapshot["rows"][0]
