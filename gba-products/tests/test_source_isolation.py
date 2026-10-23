from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.api import main
from app.core.config import Settings
from app.core import source
from app.data import cache


def qa_settings(**overrides):
    values = dict(source_environment="qa", db_name="ConcordDb_V5_QA", redis_db=12,
                  cache_namespace="products-qa", warmers_enabled=False, internal_api_key="qa-test-key")
    return Settings(_env_file=None, **(values | overrides))


@pytest.mark.parametrize("change", [
    {"db_name": "ConcordDb_V5"}, {"redis_db": 2}, {"cache_namespace": "products"},
    {"warmers_enabled": True}, {"internal_api_key": ""}, {"db_user": "sa"},
])
def test_qa_config_refuses_dev_fallback_or_background_jobs(change):
    with pytest.raises(ValueError):
        qa_settings(**change)


def test_dev_defaults_preserve_source_and_cache_namespace():
    settings = Settings(_env_file=None)
    assert (settings.source_environment, settings.db_name, settings.redis_db, settings.cache_namespace) == (
        "dev", "ConcordDb_V5", 2, "products")
    assert settings.warmers_enabled is True


def test_cache_keys_cannot_overlap_between_dev_and_qa(monkeypatch):
    dev = Settings(_env_file=None)
    monkeypatch.setattr(cache, "get_settings", lambda: dev)
    dev_key = cache.make_key("assortment", "portfolio", "2026-10-23")
    monkeypatch.setattr(cache, "get_settings", qa_settings)
    qa_key = cache.make_key("assortment", "portfolio", "2026-10-23")
    assert dev_key.startswith("products:")
    assert qa_key.startswith("products-qa:")
    assert dev_key != qa_key


def test_qa_fence_runs_before_endpoint_validation(monkeypatch):
    monkeypatch.setattr(main, "settings", qa_settings())
    client = TestClient(main.app)
    for expected in (None, "dev"):
        headers = {"X-Internal-Api-Key": "qa-test-key"}
        if expected:
            headers["X-GBA-Source-Environment"] = expected
        response = client.get("/product/not-a-number", headers=headers)
        assert response.status_code == 409
        assert response.json() == {"detail": "source_environment_mismatch"}
    assert client.get("/health", headers={"X-GBA-Source-Environment": "dev"}).status_code == 409
    response = client.get("/source", headers={"X-GBA-Source-Environment": "qa"})
    assert response.status_code == 401


def test_source_identity_uses_checked_database_and_response_header(monkeypatch):
    monkeypatch.setattr(main, "settings", qa_settings())
    validator = Mock(return_value="ConcordDb_V5_QA")
    monkeypatch.setattr(main, "validate_source_database", validator)
    response = TestClient(main.app).get("/source", headers={
        "X-Internal-Api-Key": "qa-test-key", "X-GBA-Source-Environment": "qa"})
    assert response.status_code == 200
    assert response.headers["X-GBA-Source-Environment"] == "qa"
    assert response.json()["database"] == "ConcordDb_V5_QA"
    assert response.json()["warmersEnabled"] is False
    validator.assert_called_once_with(main.settings)


def test_qa_lifespan_does_not_start_refresh_tasks(monkeypatch):
    monkeypatch.setattr(main, "settings", qa_settings())
    monkeypatch.setattr(main, "validate_source_database", lambda settings: settings.db_name)
    monkeypatch.setattr(main.sig, "synthetic_product_id", lambda: 123)
    monkeypatch.setattr(main, "dispose", Mock())
    loop = Mock(side_effect=AssertionError("QA must not create a background refresh task"))
    monkeypatch.setattr(main, "_portfolio_refresh_loop", loop)
    with TestClient(main.app):
        pass
    loop.assert_not_called()
    main.dispose.assert_called_once()


@pytest.mark.parametrize("change", [
    {"database_name": "ConcordDb_V5"}, {"is_reader": 0}, {"is_owner": 1},
    {"is_sysadmin": 1}, {"can_insert": 1}, {"can_update": 1}, {"can_delete": 1},
])
def test_actual_connection_is_checked_and_write_permissions_rejected(monkeypatch, change):
    row = dict(database_name="ConcordDb_V5_QA", is_reader=1, is_owner=0, is_sysadmin=0,
               can_insert=0, can_update=0, can_delete=0) | change
    engine = Mock()
    connection = Mock()
    from unittest.mock import MagicMock
    engine.connect.return_value = MagicMock()
    engine.connect.return_value.__enter__.return_value = connection
    connection.exec_driver_sql.return_value.mappings.return_value.one.return_value = row
    monkeypatch.setattr(source, "get_engine", lambda: engine)
    with pytest.raises(RuntimeError):
        source.validate_source_database(qa_settings())
