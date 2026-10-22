"""An API-only restart must not start generation when explicitly disabled."""
import asyncio
from types import SimpleNamespace

import pytest
from app.api import main
from app.core.config import Settings

@pytest.mark.parametrize("enabled", [True, False])
def test_startup_warm_switch_preserves_other_readonly_lifecycle(monkeypatch, enabled):
    started = []
    disposed = []
    monkeypatch.setattr(main, "settings", SimpleNamespace(warm_on_startup=enabled, internal_api_key="test"))
    monkeypatch.setattr(main, "get_engine", lambda: None)
    monkeypatch.setattr(main, "dispose", lambda: disposed.append(True))
    monkeypatch.setattr(main.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["name"])))
    async def run():
        async with main.lifespan(main.app):
            pass
    asyncio.run(run())
    assert ("cart-warm" in started) is enabled
    assert "mongo-orphan-check" in started
    assert disposed == [True]


def test_environment_can_disable_startup_without_changing_default(monkeypatch):
    monkeypatch.delenv("WARM_ON_STARTUP", raising=False)
    assert Settings(_env_file=None).warm_on_startup
    monkeypatch.setenv("WARM_ON_STARTUP", "false")
    assert not Settings(_env_file=None).warm_on_startup
