"""Cache-key tests — pure string composition, no Redis/DB."""
from __future__ import annotations

from datetime import date

from app.core.history import model_contract_fingerprint
from app.data import cache


def test_make_key_varies_with_margin():
    a = cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "uk")
    b = cache.make_key(7, "ca-uid", "2026-06-15", 20.0, True, "uk")
    assert a != b


def test_make_key_varies_with_vat_and_culture():
    base = cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "uk")
    assert base != cache.make_key(7, "ca-uid", "2026-06-15", 12.0, False, "uk")
    assert base != cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "ru")


def test_make_key_stable_for_same_params():
    a = cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "uk")
    b = cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "uk")
    assert a == b


def test_make_key_normalizes_agreement_uid_case():
    lower = cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "uk")
    upper = cache.make_key(7, "CA-UID", "2026-06-15", 12.0, True, "uk")
    assert lower == upper


def test_make_key_namespaces_source_history_contract(monkeypatch):
    settings = cache.get_settings()
    first = cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "uk")
    expected = model_contract_fingerprint(
        settings.model_version,
        settings.source_history_start_date,
        settings.trailing_window_months,
    )
    assert f"price:{expected}:" in first

    monkeypatch.setattr(settings, "source_history_start_date", date(2025, 2, 1))
    second = cache.make_key(7, "ca-uid", "2026-06-15", 12.0, True, "uk")
    assert first != second
