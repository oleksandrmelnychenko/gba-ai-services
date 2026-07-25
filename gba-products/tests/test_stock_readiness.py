from __future__ import annotations

from app.data.signals_repository import _stock_readiness_reason


def _metrics(**overrides):
    metrics = {
        "global_availability_row_count": 100,
        "global_available_qty": 250.0,
        "role_marked_storage_count": 2,
        "sellable_availability_row_count": 80,
        "sellable_available_qty": 200.0,
    }
    metrics.update(overrides)
    return metrics


def test_stock_readiness_accepts_populated_operational_scope():
    assert _stock_readiness_reason(_metrics()) is None


def test_stock_readiness_rejects_missing_product_availability():
    reason = _stock_readiness_reason(
        _metrics(global_availability_row_count=0, global_available_qty=0)
    )
    assert reason == "product_availability_missing"


def test_stock_readiness_rejects_global_stock_without_storage_roles():
    reason = _stock_readiness_reason(_metrics(role_marked_storage_count=0))
    assert reason == "storage_roles_missing"


def test_stock_readiness_rejects_empty_sellable_scope_with_global_stock():
    reason = _stock_readiness_reason(
        _metrics(sellable_availability_row_count=0, sellable_available_qty=0)
    )
    assert reason == "sellable_inventory_missing"


def test_stock_readiness_allows_legitimate_zero_quantity_when_global_is_zero():
    reason = _stock_readiness_reason(
        _metrics(global_available_qty=0, sellable_availability_row_count=80,
                 sellable_available_qty=0)
    )
    assert reason is None


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def exec_driver_sql(self, _sql):
        return None


class _Engine:
    def connect(self):
        return _Connection()


def test_health_degrades_when_business_stock_source_is_not_ready(monkeypatch):
    from app.api import main

    monkeypatch.setattr(main, "get_engine", lambda: _Engine())
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(
        main.sig,
        "stock_source_readiness",
        lambda: {
            "ready": False,
            "reason": "sellable_inventory_missing",
            "global_available_qty": 250.0,
        },
    )

    health = main._service_health()

    assert health["status"] == "degraded"
    assert health["business_ready"] is False
    assert health["business_reason"] == "sellable_inventory_missing"


def test_health_is_green_only_with_db_cache_and_business_inventory(monkeypatch):
    from app.api import main

    monkeypatch.setattr(main, "get_engine", lambda: _Engine())
    monkeypatch.setattr(main.cache, "health", lambda: True)
    monkeypatch.setattr(
        main.sig,
        "stock_source_readiness",
        lambda: {"ready": True, "reason": None, "sellable_available_qty": 200.0},
    )

    health = main._service_health()

    assert health["status"] == "healthy"
    assert health["business_ready"] is True
    assert health["business_reason"] is None
