from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.refresh import refresh_generation


@pytest.mark.parametrize("day", ["2026-03-29", "2026-09-07", "2026-10-25"])
def test_generation_changes_at_five_even_on_dst_boundary(day):
    from datetime import date, timedelta
    tz = ZoneInfo("Europe/Kyiv")
    before = datetime.fromisoformat(day + "T04:59:59").replace(tzinfo=tz)
    after = datetime.fromisoformat(day + "T05:00:00").replace(tzinfo=tz)
    assert refresh_generation(before) == "daily-" + (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    assert refresh_generation(after) == "daily-" + day
    assert refresh_generation(after.astimezone(ZoneInfo("UTC"))) == refresh_generation(after)


def test_cache_key_switches_with_generation(monkeypatch):
    from app.data import cache
    import inspect
    key_function = cache.make_key
    values = {"product": 1, "agreement": "a", "as_of": "2026-09-07", "target_margin_pct": 12.0,
              "with_vat": True, "culture": "uk", "client_id": 1, "months": 3,
              "kind": "client", "entity_id": 1}
    kwargs = {name: values[name] for name, param in inspect.signature(key_function).parameters.items()
              if param.kind != inspect.Parameter.VAR_POSITIONAL}
    monkeypatch.setattr(cache, "refresh_generation", lambda: "daily-before")
    before = key_function(**kwargs)
    monkeypatch.setattr(cache, "refresh_generation", lambda: "daily-after")
    assert key_function(**kwargs) != before
