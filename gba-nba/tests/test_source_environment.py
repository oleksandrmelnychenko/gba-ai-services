from datetime import datetime
from types import SimpleNamespace

import mongomock
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.data import mongo
from app.services import source_provenance, worker
from app.api import main


def qa_settings(**extra):
    return Settings(_env_file=None, source_environment="qa", db_name="ConcordDb_V5_QA", mongo_db="gba_nba_qa", generation_enabled=False, **extra)


@pytest.mark.parametrize("field,value", [("db_name", "ConcordDb_V5"), ("mongo_db", "gba_nba"), ("generation_enabled", True)])
def test_qa_rejects_shared_source_or_implicit_generation(field, value):
    values = dict(source_environment="qa", db_name="ConcordDb_V5_QA", mongo_db="gba_nba_qa", generation_enabled=False)
    values[field] = value
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_same_task_key_mutations_are_independent_in_each_mongo_namespace(monkeypatch):
    client = mongomock.MongoClient()
    monkeypatch.setattr(mongo, "get_client", lambda: client)
    for environment, database in [("dev", "gba_nba"), ("qa", "gba_nba_qa")]:
        monkeypatch.setattr(mongo, "get_settings", lambda db=database: SimpleNamespace(mongo_db=db))
        mongo.tasks().insert_one({"task_key": "same-client-task", "status": "open", "environment": environment})
        mongo.seller_contact_events().insert_one({"event_id": "same-event", "environment": environment})
    mongo.tasks().update_one({"task_key": "same-client-task"}, {"$set": {"status": "done"}})
    assert client.gba_nba.tasks.find_one({})["status"] == "open"
    assert client.gba_nba_qa.tasks.find_one({})["status"] == "done"
    assert client.gba_nba.seller_contact_events.find_one({})["environment"] == "dev"
    assert client.gba_nba_qa.seller_contact_events.find_one({})["environment"] == "qa"


def test_uninitialized_qa_reports_no_generation_and_activity_not_sync(monkeypatch):
    monkeypatch.setattr(source_provenance, "get_settings", qa_settings)
    monkeypatch.setattr(source_provenance.signals_repository, "source_readiness", lambda _: {"source_ready": True, "latest_sale_at": "2026-10-22T12:00:00"})
    monkeypatch.setattr(source_provenance.mongo, "generation_readiness", lambda _: {"generation_ready": False, "last_generation_at": None})
    result = source_provenance.source_provenance()
    assert result["sourceEnvironment"] == "qa"
    assert result["watermarkKind"] == "latest_valid_sale_activity"
    assert result["syncFreshnessKnown"] is False
    assert result["generationEnabled"] is False
    assert result["generationReady"] is False
    assert result["lastGenerationAt"] is None
    assert datetime.fromisoformat(result["asOf"]).tzinfo is not None


def test_qa_generation_endpoints_reject_before_resolving_manager_or_reading_source(monkeypatch):
    monkeypatch.setattr(main, "settings", qa_settings())
    monkeypatch.setattr(main, "_resolve_manager", lambda _: pytest.fail("manager source must not be queried"))
    monkeypatch.setattr(main, "_resolved_as_of", lambda _: pytest.fail("source must not be queried"))
    client = TestClient(main.app, raise_server_exceptions=False, headers={"X-GBA-Source-Environment": "qa"})
    assert client.post("/generate/manager/1").status_code == 409
    assert client.post("/cockpit/generate?manager_net_uid=11111111-1111-1111-1111-111111111111").status_code == 409


def test_disabled_worker_and_feedback_fail_before_reading_any_business_data(monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "get_settings", qa_settings)
    monkeypatch.setattr(worker.sig, "all_managers", lambda: pytest.fail("worker must not query SQL"))
    monkeypatch.setattr(worker.lifecycle, "cross_sell_negatives", lambda _: pytest.fail("feedback must not query Mongo"))
    with pytest.raises(RuntimeError, match="generation_disabled"):
        worker.run()
    with pytest.raises(RuntimeError, match="generation_disabled"):
        worker.push_reco_feedback()


def test_wrong_environment_header_blocks_reads_and_writes_before_handlers(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings(_env_file=None, source_environment="dev", internal_api_key=""))
    monkeypatch.setattr(main, "_resolve_manager", lambda _: pytest.fail("must not resolve foreign manager"))
    client = TestClient(main.app, headers={"X-GBA-Source-Environment": "qa"})
    assert client.get("/cockpit/source").status_code == 409
    response = client.post("/cockpit/generate?manager_net_uid=11111111-1111-1111-1111-111111111111")
    assert response.status_code == 409
    assert response.json()["detail"] == "source_environment_mismatch"
    # Malformed JSON would fail body validation if middleware let it reach the route.
    response = client.post("/cockpit/status", content="{malformed", headers={"Content-Type": "application/json"})
    assert response.status_code == 409
    assert response.json()["detail"] == "source_environment_mismatch"
