"""Read-only serving provenance. Sale activity is not a sync-completion watermark."""
from datetime import UTC, datetime

from app.core.config import get_settings
from app.data import mongo, signals_repository


def source_provenance() -> dict:
    settings = get_settings()
    source = {"source_ready": False, "latest_sale_at": None}
    generation = {"generation_ready": False, "last_generation_at": None}
    source_available = task_store_available = False
    try:
        source = signals_repository.source_readiness(settings.max_source_lag_days)
        source_available = True
    except Exception:
        pass
    try:
        generation = mongo.generation_readiness(settings.max_generation_lag_hours)
        task_store_available = True
    except Exception:
        pass
    activity = source.get("latest_sale_at")
    if isinstance(activity, str):
        timestamp = datetime.fromisoformat(activity)
        activity = (timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp).isoformat()
    return {
        "sourceEnvironment": settings.source_environment,
        "asOf": datetime.now(UTC).isoformat(),
        "watermark": activity,
        "watermarkKind": "latest_valid_sale_activity",
        "syncFreshnessKnown": False,
        "historyStartDate": settings.source_history_start_date.isoformat(),
        "sourceAvailable": source_available,
        "sourceReady": bool(source.get("source_ready")),
        "taskStoreAvailable": task_store_available,
        "generationEnabled": settings.generation_enabled,
        "generationReady": bool(generation.get("generation_ready")),
        "lastGenerationAt": generation.get("last_generation_at"),
    }
