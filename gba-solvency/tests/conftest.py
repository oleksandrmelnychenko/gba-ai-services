import pytest


@pytest.fixture(autouse=True)
def _no_unconverted_debt_lookup(monkeypatch):
    from app.data import solvency_repository

    monkeypatch.setattr(solvency_repository, "unconverted_debt", lambda *args, **kwargs: {})
