"""Self-healing cache watchdog tests."""
from __future__ import annotations

from app.domain.models import MODEL_VERSION
from app.services import scheduler
from app.services.replenishment import worker


def _source() -> dict:
    return {
        "ready": True,
        "reason": None,
        "producer_count": 2,
        "product_count": 12,
        "source_fingerprint": "source-fp",
        "as_of": "2026-06-15",
    }


def _canonical_zero_payload(source_fingerprint: str = "source-fp") -> dict:
    return {
        "as_of_date": "2026-06-15",
        "source_history_start": "2025-01-01",
        "effective_start": "2026-02-15",
        "effective_history_days": 120,
        "history_complete": True,
        "history_not_applicable": ["inventory", "reservations"],
        "model_version": MODEL_VERSION,
        "item_count": 0,
        "total_item_count": 0,
        "is_truncated": False,
        "duplicate_supplier_options_removed": 0,
        "total_suggested_qty": 0.0,
        "total_cost_eur": 0.0,
        "priced_cost_eur": 0.0,
        "unpriced_item_count": 0,
        "items": [],
        "_source_fingerprint": source_fingerprint,
    }


def _charts_payload(source_fingerprint: str = "source-fp") -> dict:
    return {
        "producer_id": None,
        "as_of_date": "2026-06-15",
        "source_history_start": "2025-01-01",
        "effective_start": "2026-02-15",
        "effective_history_days": 120,
        "history_complete": True,
        "history_not_applicable": ["inventory", "reservations"],
        "model_version": MODEL_VERSION,
        "top_n": worker.CHARTS_TOP_N,
        "urgency_mix": [],
        "days_of_cover_hist": [],
        "top_items": [],
        "demand_series": [],
        "_source_fingerprint": source_fingerprint,
    }


def test_watchdog_repairs_missing_daily_generation(monkeypatch):
    monkeypatch.setattr(scheduler.cache, "health", lambda: True)
    monkeypatch.setattr(
        worker,
        "require_source_readiness",
        lambda *args, **kwargs: _source(),
    )
    monkeypatch.setattr(scheduler.cache, "get", lambda key: None)
    calls = []
    monkeypatch.setattr(
        worker,
        "warm_cart",
        lambda **kwargs: calls.append(("cart", kwargs)),
    )
    monkeypatch.setattr(
        worker,
        "warm_charts",
        lambda **kwargs: calls.append(("charts", kwargs)),
    )
    monkeypatch.setattr(
        worker,
        "run",
        lambda **kwargs: calls.append(("producers", kwargs))
        or {"ok": 769, "failed": 0, "skipped": 0},
    )

    result = scheduler._cache_watchdog_job(as_of="2026-06-15")

    assert [name for name, _ in calls] == ["cart", "charts", "producers"]
    assert calls[2][1]["skip_existing"] is True
    assert result["as_of"] == "2026-06-15"


def test_watchdog_keeps_valid_generation_and_only_resumes_producers(monkeypatch):
    cart_key = worker.cart_cache_key("2026-06-15")
    charts_key = scheduler.cache.make_key(
        "charts",
        f"all:{worker.CHARTS_TOP_N}",
        "2026-06-15",
    )
    payloads = {
        cart_key: _canonical_zero_payload(),
        charts_key: _charts_payload(),
    }
    monkeypatch.setattr(scheduler.cache, "health", lambda: True)
    monkeypatch.setattr(
        worker,
        "require_source_readiness",
        lambda *args, **kwargs: _source(),
    )
    monkeypatch.setattr(scheduler.cache, "get", payloads.get)
    monkeypatch.setattr(
        worker,
        "warm_cart",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("valid cart must not be rebuilt")
        ),
    )
    monkeypatch.setattr(
        worker,
        "warm_charts",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("valid charts must not be rebuilt")
        ),
    )
    monkeypatch.setattr(
        worker,
        "run",
        lambda **kwargs: {"ok": 0, "failed": 0, "skipped": 769},
    )

    result = scheduler._cache_watchdog_job(as_of="2026-06-15")

    assert result["producers_warmed"] == 0
    assert result["producers_skipped"] == 769


def test_watchdog_rebuilds_old_source_epoch(monkeypatch):
    payloads = {
        worker.cart_cache_key("2026-06-15"): _canonical_zero_payload("old-fp"),
        scheduler.cache.make_key(
            "charts",
            f"all:{worker.CHARTS_TOP_N}",
            "2026-06-15",
        ): _charts_payload("old-fp"),
    }
    monkeypatch.setattr(scheduler.cache, "health", lambda: True)
    monkeypatch.setattr(
        worker,
        "require_source_readiness",
        lambda *args, **kwargs: _source(),
    )
    monkeypatch.setattr(scheduler.cache, "get", payloads.get)
    deleted = []
    monkeypatch.setattr(scheduler.cache, "delete", deleted.append)
    warmed = []
    monkeypatch.setattr(
        worker,
        "warm_cart",
        lambda **kwargs: warmed.append("cart"),
    )
    monkeypatch.setattr(
        worker,
        "warm_charts",
        lambda **kwargs: warmed.append("charts"),
    )
    monkeypatch.setattr(
        worker,
        "run",
        lambda **kwargs: {"ok": 0, "failed": 0, "skipped": 769},
    )

    scheduler._cache_watchdog_job(as_of="2026-06-15")

    assert warmed == ["cart", "charts"]
    assert set(deleted) == set(payloads)
