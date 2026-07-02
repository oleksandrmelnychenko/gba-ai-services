"""Cache-key tests — pure string composition, no Redis/DB."""
from __future__ import annotations

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
