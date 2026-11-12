import pytest


@pytest.fixture(autouse=True)
def _no_shared_product_facts(monkeypatch):
    """Unit tests never read or build the Redis-backed product facts artefacts."""
    from app.api import main

    async def unavailable(key, product_id, as_of, build):
        return None

    monkeypatch.setattr(main, "_product_fact_rows", unavailable)
    monkeypatch.setattr(main.memory, "release_free_memory", lambda: False)
