"""FastAPI app — GBA Sales Forecast Service (client / product monthly sales projection)."""

from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.history import resolve_history_window
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import signals_repository as sig
from app.data.db import dispose, get_engine
from app.services import forecast as fc

log = get_logger("api")
settings = get_settings()
_EXPECTED_SOURCE_HISTORY_START = "2025-01-01"
KYIV = ZoneInfo("Europe/Kyiv")

_OPEN_PATHS = {"/health"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_runtime_configuration()
    get_engine()
    log.info("synthetic_product_resolved", **sig.synthetic_product_status())
    if not settings.internal_api_key:
        log.warning(
            "internal_api_key_not_set",
            note="gba-forecast running OPEN because ALLOW_OPEN_INTERNAL_API=true",
        )
    log.info("service_starting", service="gba-forecast")
    yield
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Sales Forecast Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_internal_key(request: Request, call_next):
    if settings.internal_api_key and request.url.path not in _OPEN_PATHS:
        provided = request.headers.get("X-Internal-Api-Key", "")
        if not hmac.compare_digest(provided, settings.internal_api_key):
            return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


@app.middleware("http")
async def timing(request: Request, call_next):
    t = time.time()
    resp = await call_next(request)
    resp.headers["X-Process-Time-Ms"] = str(round((time.time() - t) * 1000, 2))
    return resp


def _today(now: datetime | None = None) -> str:
    current = now or datetime.now(KYIV)
    if current.tzinfo is None:
        raise ValueError("business-date clock must be timezone-aware")
    return current.astimezone(KYIV).date().isoformat()


def _history_metadata(as_of: date | str) -> dict[str, str | bool]:
    """Stable public metadata for the effective factual history window."""
    as_of_date = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    try:
        window = resolve_history_window(
            as_of_date,
            settings.history_months,
            settings.source_history_start_date,
        )
    except ValueError:
        return {
            "source_history_start": settings.source_history_start_date.isoformat(),
            "effective_start": settings.source_history_start_date.isoformat(),
            "history_complete": False,
        }
    return {
        "source_history_start": window.source_history_start.isoformat(),
        "effective_start": window.effective_start.isoformat(),
        "history_complete": window.history_complete,
    }


@app.get("/health")
def health() -> dict:
    return _health_snapshot()


@app.get("/ready")
def ready() -> JSONResponse:
    snapshot = _health_snapshot()
    ready_ok = snapshot["status"] == "healthy"
    body = {**snapshot, "status": "ready" if ready_ok else "not_ready"}
    return JSONResponse(status_code=200 if ready_ok else 503, content=body)


def _database_health() -> tuple[str, bool]:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    return ("healthy" if db_ok else "degraded", db_ok)


def _health_snapshot(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    _, db_ok = _database_health()
    cache_ok = cache.health()
    data = (
        _sales_data_health(now)
        if db_ok
        else _empty_sales_data_health("database_unavailable", now.date())
    )
    source_history_start = settings.source_history_start_date.isoformat()
    source_history_contract_ready = (
        source_history_start == _EXPECTED_SOURCE_HISTORY_START
    )
    data = {**data, "source_history_start": source_history_start}
    if not source_history_contract_ready:
        data["source_ready"] = False
        data["reason"] = "source_history_start_mismatch"
    business_ready = bool(data["source_ready"])
    service_healthy = db_ok and cache_ok and business_ready
    return {
        "status": "healthy" if service_healthy else "degraded",
        "business_ready": business_ready,
        "db_connected": db_ok,
        "cache_connected": cache_ok,
        "source_history_start": source_history_start,
        "source_history_contract_ready": source_history_contract_ready,
        "version": "0.1.0",
        "model_version": settings.model_version,
        "data": data,
    }


def _sales_data_health(now: datetime) -> dict[str, Any]:
    as_of = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    history_metadata = _history_metadata(now.date())
    try:
        snapshot = sig.sales_source_status(as_of, settings.history_months)
    except Exception as exc:  # noqa: BLE001
        log.warning("sales_source_health_failed", error=str(exc))
        return _empty_sales_data_health("source_query_failed", now.date())

    latest_raw = snapshot.get("latest_sale_at")
    latest = _as_utc(latest_raw)
    age_hours = (now - latest).total_seconds() / 3600 if latest is not None else None
    source_exists = bool(snapshot["source_schema_present"] and snapshot["canonical_row_count"] > 0)
    history_present = bool(
        snapshot["history_row_count"] > 0
        and snapshot["history_product_count"] > 0
        and snapshot["history_client_count"] > 0
    )
    source_fresh = bool(age_hours is not None and 0 <= age_hours <= settings.source_max_age_hours)
    values_valid = snapshot["invalid_value_row_count"] == 0
    synthetic = sig.synthetic_product_status()
    synthetic_resolved = bool(synthetic["resolved"])
    source_ready = (
        source_exists
        and history_present
        and source_fresh
        and values_valid
        and synthetic_resolved
    )

    reason = "ready"
    if not snapshot["source_schema_present"]:
        reason = "source_schema_missing"
    elif not source_exists:
        reason = "canonical_source_empty"
    elif not history_present:
        reason = "history_window_empty"
    elif not values_valid:
        reason = "invalid_sales_values"
    elif not synthetic_resolved:
        reason = "synthetic_product_unresolved"
    elif not source_fresh:
        reason = "canonical_source_stale"

    return {
        "source": "dbo.OrderItem.IsValidForCurrentSale + dbo.Order.Created",
        "source_ready": source_ready,
        "reason": reason,
        "source_schema_present": bool(snapshot["source_schema_present"]),
        "source_exists": source_exists,
        "source_fresh": source_fresh,
        "freshness_max_age_hours": settings.source_max_age_hours,
        "latest_sale_at": latest.isoformat() if latest is not None else None,
        "source_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "canonical_row_count": snapshot["canonical_row_count"],
        "history_window_months": settings.history_months,
        **history_metadata,
        "history_row_count": snapshot["history_row_count"],
        "history_product_count": snapshot["history_product_count"],
        "history_client_count": snapshot["history_client_count"],
        "invalid_value_row_count": snapshot["invalid_value_row_count"],
        "synthetic_product_id": synthetic["product_id"],
        "synthetic_product_resolved": synthetic_resolved,
        "synthetic_product_source": synthetic["source"],
    }


def _empty_sales_data_health(reason: str, as_of: date | None = None) -> dict[str, Any]:
    history_metadata = _history_metadata(as_of or datetime.now(UTC).date())
    return {
        "source": "dbo.OrderItem.IsValidForCurrentSale + dbo.Order.Created",
        "source_ready": False,
        "reason": reason,
        "source_schema_present": False,
        "source_exists": False,
        "source_fresh": False,
        "freshness_max_age_hours": settings.source_max_age_hours,
        "latest_sale_at": None,
        "source_age_hours": None,
        "canonical_row_count": 0,
        "history_window_months": settings.history_months,
        **history_metadata,
        "history_row_count": 0,
        "history_product_count": 0,
        "history_client_count": 0,
        "invalid_value_row_count": 0,
        "synthetic_product_id": None,
        "synthetic_product_resolved": False,
        "synthetic_product_source": "unavailable",
    }


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.get("/forecast/sales")
def forecast_sales(
    client_net_id: UUID | None = None,
    product_net_id: UUID | None = None,
    months: int | None = Query(default=None, ge=1),
    use_cache: bool = True,
    as_of_date: date | None = None,
) -> dict:
    """Monthly sales forecast (EUR) for a client, a product, or both.

    Computes only the series whose id is supplied:
      - client_net_id        -> ByClient
      - product_net_id       -> ByProduct
      - both                 -> ByClient, ByProduct, and ByClientAndProduct (that client buying
                                that product)
    Each populated key is an array of {SaleAmount, MonthNameUK} for the next N months
    (`months`, default config). A series with too little history yields [] for its key
    (the console shows «немає даних») — never crashes. Internal-key gated when configured.
    """
    started = time.time()
    horizon = _resolve_horizon(months)
    current_as_of = _today()
    current_as_of_date = date.fromisoformat(current_as_of)
    requested_as_of = as_of_date.isoformat() if as_of_date is not None else None
    if as_of_date is not None and as_of_date < settings.source_history_start_date:
        raise HTTPException(status_code=422, detail="as_of_date_before_source_history_start")
    if current_as_of_date < settings.source_history_start_date:
        raise HTTPException(status_code=422, detail="as_of_date_before_source_history_start")
    if requested_as_of is not None and requested_as_of != current_as_of:
        raise HTTPException(status_code=422, detail="historical_as_of_not_supported")
    as_of_str = current_as_of
    as_of = current_as_of_date
    history_metadata = _history_metadata(as_of)
    client_id: int | None = None
    product_id: int | None = None
    try:
        client_id = sig.client_id_for_netuid(str(client_net_id)) if client_net_id else None
        product_id = sig.product_id_for_netuid(str(product_net_id)) if product_net_id else None
        synthetic = sig.synthetic_product_status()
        if not synthetic["resolved"]:
            raise HTTPException(status_code=503, detail="synthetic_product_unresolved")
        synthetic_id = int(synthetic["product_id"])
        is_synthetic_product = product_id is not None and product_id == synthetic_id
        source_fingerprint = sig.forecast_source_fingerprint(
            client_id,
            None if is_synthetic_product else product_id,
            as_of_str,
            settings.history_months,
        )
        cache_key = _sales_cache_key(
            client_net_id,
            product_net_id,
            client_id,
            product_id,
            synthetic_id,
            source_fingerprint,
            horizon,
            as_of_str,
            requested_as_of,
        )
        cached = cache.get(cache_key) if use_cache else None
        if cached is not None and _is_valid_cached_response(
            cached,
            client_net_id,
            product_net_id,
            client_id,
            product_id,
            is_synthetic_product,
            source_fingerprint,
            horizon,
            as_of_str,
            requested_as_of,
        ):
            METRICS.record_request((time.time() - started) * 1000)
            return cached

        out: dict[str, Any] = {
            "ByClient": [],
            "ByProduct": [],
            "ByClientAndProduct": [],
        }
        history = {
            "ByClient": _unavailable_history_status(
                "not_requested" if client_net_id is None else "unknown_identity"
            ),
            "ByProduct": _unavailable_history_status(
                "not_requested"
                if product_net_id is None
                else "excluded_synthetic"
                if is_synthetic_product
                else "unknown_identity"
            ),
            "ByClientAndProduct": _unavailable_history_status(
                "not_requested"
                if client_net_id is None or product_net_id is None
                else "excluded_synthetic"
                if is_synthetic_product
                else "unknown_identity"
            ),
        }

        if client_id is not None:
            client_rows = sig.monthly_sales_by_client(client_id, as_of_str, settings.history_months)
            out["ByClient"], history["ByClient"] = _forecast_with_history(client_rows, as_of, horizon)

        if product_id is not None and not is_synthetic_product:
            product_rows = sig.monthly_sales_by_product(product_id, as_of_str, settings.history_months)
            out["ByProduct"], history["ByProduct"] = _forecast_with_history(product_rows, as_of, horizon)

        if client_id is not None and product_id is not None and not is_synthetic_product:
            pair_rows = sig.monthly_sales_by_client_and_product(
                client_id, product_id, as_of_str, settings.history_months
            )
            out["ByClientAndProduct"], history["ByClientAndProduct"] = _forecast_with_history(
                pair_rows, as_of, horizon
            )

        client_identity = (
            "not_requested" if client_net_id is None else "resolved" if client_id is not None else "unknown"
        )
        product_identity = (
            "not_requested"
            if product_net_id is None
            else "excluded_synthetic"
            if is_synthetic_product
            else "resolved"
            if product_id is not None
            else "unknown"
        )
        out["meta"] = {
            "status": _response_status(
                client_net_id,
                product_net_id,
                client_identity,
                product_identity,
                history,
            ),
            "as_of": as_of_str,
            "requested_as_of": requested_as_of,
            "horizon_months": horizon,
            "currency": "EUR",
            "model_version": settings.model_version,
            "source_fingerprint": source_fingerprint,
            **history_metadata,
            "requested": {
                "client_net_id": _uuid_text(client_net_id),
                "product_net_id": _uuid_text(product_net_id),
            },
            "resolved": {
                "client_id": client_id,
                "client_net_id": _uuid_text(client_net_id) if client_id is not None else None,
                "product_id": product_id,
                "product_net_id": (_uuid_text(product_net_id) if product_id is not None else None),
            },
            "identity": {
                "client": client_identity,
                "product": product_identity,
            },
            "history_window_months": settings.history_months,
            "minimum_non_zero_months": settings.min_history_months,
            "history": history,
        }

        METRICS.record_request((time.time() - started) * 1000)
        if not _is_valid_cached_response(
            out,
            client_net_id,
            product_net_id,
            client_id,
            product_id,
            is_synthetic_product,
            source_fingerprint,
            horizon,
            as_of_str,
            requested_as_of,
        ):
            raise ValueError("forecast response failed its exact contract")
        source_fingerprint_after = sig.forecast_source_fingerprint(
            client_id,
            None if is_synthetic_product else product_id,
            as_of_str,
            settings.history_months,
        )
        if source_fingerprint_after != source_fingerprint:
            raise HTTPException(status_code=503, detail="sales_source_changed_retry")
        if use_cache:
            cache.set(cache_key, out)
        return out
    except HTTPException:
        METRICS.record_request((time.time() - started) * 1000, error=True)
        raise
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error(
            "forecast_sales_failed",
            client_net_id=client_net_id,
            product_net_id=product_net_id,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="forecast_sales_failed") from exc


def _forecast_with_history(rows: list[dict], as_of: date, horizon: int) -> tuple[list[dict], dict[str, Any]]:
    labels = fc.history_labels(
        as_of,
        settings.history_months,
        settings.source_history_start_date,
    )
    allowed_months = set(labels)
    if any(row.get("ym") and str(row["ym"]) not in allowed_months for row in rows):
        raise ValueError("sales history contains a month outside the effective source window")
    summary = sig.history_summary(rows, max_months=len(labels))
    points = fc.forecast_points(sig.to_series(rows), as_of, settings, horizon)
    sufficient = summary["non_zero_month_count"] >= settings.min_history_months
    status = "sufficient" if sufficient else "insufficient_history"
    return points, {
        "status": status,
        "month_count": summary["month_count"],
        "non_zero_month_count": summary["non_zero_month_count"],
        "total_eur": fc.eur_cents(summary["total_eur"]),
        "sufficient": sufficient,
    }


def _unavailable_history_status(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "month_count": 0,
        "non_zero_month_count": 0,
        "total_eur": 0.0,
        "sufficient": False,
    }


def _response_status(
    client_net_id: UUID | None,
    product_net_id: UUID | None,
    client_identity: str,
    product_identity: str,
    history: dict[str, dict[str, Any]],
) -> str:
    if client_net_id is None and product_net_id is None:
        return "no_scope"
    if "excluded_synthetic" in {client_identity, product_identity}:
        return "excluded_entity"
    if "unknown" in {client_identity, product_identity}:
        return "unknown_identity"

    applicable = [
        item
        for item in history.values()
        if item["status"] not in {"not_requested", "unknown_identity", "excluded_synthetic"}
    ]
    sufficient_count = sum(item["status"] == "sufficient" for item in applicable)
    if sufficient_count == len(applicable):
        return "ready"
    if sufficient_count == 0:
        return "insufficient_history"
    return "partial"


def _uuid_text(value: UUID | None) -> str | None:
    return str(value).lower() if value is not None else None


def _resolve_horizon(months: int | None) -> int:
    horizon = months or settings.forecast_horizon_months
    if horizon > settings.max_forecast_horizon_months:
        raise HTTPException(
            status_code=422,
            detail=f"months must be <= {settings.max_forecast_horizon_months}",
        )
    return horizon


def _sales_cache_key(
    client_net_id: UUID | None,
    product_net_id: UUID | None,
    client_id: int | None,
    product_id: int | None,
    synthetic_id: int,
    source_fingerprint: str,
    horizon: int,
    as_of: str,
    requested_as_of: str | None,
) -> str:
    client_part = str(client_net_id).lower() if client_net_id else "none"
    product_part = str(product_net_id).lower() if product_net_id else "none"
    entity = f"{client_part}:{product_part}"
    history_metadata = _history_metadata(as_of)
    return cache.make_key(
        "sales",
        entity,
        horizon,
        settings.model_version,
        settings.forecast_method,
        settings.history_months,
        settings.min_history_months,
        history_metadata["source_history_start"],
        history_metadata["effective_start"],
        history_metadata["history_complete"],
        client_id or "unknown-client",
        product_id or "unknown-product",
        synthetic_id,
        source_fingerprint,
        as_of,
        requested_as_of or "default-as-of",
    )


def _is_valid_cached_response(
    payload: object,
    client_net_id: UUID | None,
    product_net_id: UUID | None,
    client_id: int | None,
    product_id: int | None,
    is_synthetic_product: bool,
    source_fingerprint: str,
    horizon: int,
    as_of: str,
    requested_as_of: str | None,
) -> bool:
    """Fail closed on stale/mismatched cache entries before identity data reaches callers."""
    if not isinstance(payload, dict):
        return False
    series_keys = ("ByClient", "ByProduct", "ByClientAndProduct")
    if any(not isinstance(payload.get(key), list) for key in series_keys):
        return False

    meta = payload.get("meta")
    if not isinstance(meta, dict):
        return False
    history_metadata = _history_metadata(as_of)
    if (
        meta.get("as_of") != as_of
        or meta.get("requested_as_of") != requested_as_of
        or meta.get("horizon_months") != horizon
        or meta.get("currency") != "EUR"
        or meta.get("model_version") != settings.model_version
        or meta.get("source_fingerprint") != source_fingerprint
        or meta.get("source_history_start") != history_metadata["source_history_start"]
        or meta.get("effective_start") != history_metadata["effective_start"]
        or meta.get("history_complete") is not history_metadata["history_complete"]
    ):
        return False
    if meta.get("status") not in {
        "no_scope",
        "excluded_entity",
        "unknown_identity",
        "insufficient_history",
        "partial",
        "ready",
    }:
        return False

    requested = meta.get("requested")
    resolved = meta.get("resolved")
    identity = meta.get("identity")
    history = meta.get("history")
    if not all(isinstance(item, dict) for item in (requested, resolved, identity, history)):
        return False
    if requested != {
        "client_net_id": _uuid_text(client_net_id),
        "product_net_id": _uuid_text(product_net_id),
    }:
        return False
    if set(identity) != {"client", "product"}:
        return False
    if identity["client"] not in {"not_requested", "resolved", "unknown"}:
        return False
    if identity["product"] not in {
        "not_requested",
        "resolved",
        "unknown",
        "excluded_synthetic",
    }:
        return False
    expected_identity = {
        "client": (
            "not_requested" if client_net_id is None else "resolved" if client_id is not None else "unknown"
        ),
        "product": (
            "not_requested"
            if product_net_id is None
            else "excluded_synthetic"
            if is_synthetic_product
            else "resolved"
            if product_id is not None
            else "unknown"
        ),
    }
    if identity != expected_identity:
        return False
    if not _valid_resolved_identity(resolved, requested, identity):
        return False
    if resolved["client_id"] != client_id or resolved["product_id"] != product_id:
        return False
    if set(history) != set(series_keys):
        return False

    effective_month_count = len(
        fc.history_labels(
            date.fromisoformat(as_of),
            settings.history_months,
            settings.source_history_start_date,
        )
    )
    for key in series_keys:
        history_item = history[key]
        if not isinstance(history_item, dict) or not _valid_history_item(
            history_item,
            max_months=effective_month_count,
        ):
            return False
        points = payload[key]
        if history_item["status"] == "sufficient":
            if len(points) != horizon:
                return False
        elif points:
            return False
        if any(not _valid_forecast_point(point) for point in points):
            return False
    return True


def _valid_resolved_identity(
    resolved: dict[str, Any],
    requested: dict[str, Any],
    identity: dict[str, Any],
) -> bool:
    if set(resolved) != {
        "client_id",
        "client_net_id",
        "product_id",
        "product_net_id",
    }:
        return False
    for entity in ("client", "product"):
        numeric_id = resolved[f"{entity}_id"]
        net_id = resolved[f"{entity}_net_id"]
        status = identity[entity]
        has_numeric_id = isinstance(numeric_id, int) and not isinstance(numeric_id, bool) and numeric_id > 0
        if status in {"resolved", "excluded_synthetic"}:
            if not has_numeric_id or net_id != requested[f"{entity}_net_id"]:
                return False
        elif numeric_id is not None or net_id is not None:
            return False
    return True


def _valid_history_item(item: dict[str, Any], *, max_months: int | None = None) -> bool:
    if set(item) != {
        "status",
        "month_count",
        "non_zero_month_count",
        "total_eur",
        "sufficient",
    }:
        return False
    if item["status"] not in {
        "not_requested",
        "unknown_identity",
        "excluded_synthetic",
        "insufficient_history",
        "sufficient",
    }:
        return False
    month_count = item["month_count"]
    non_zero = item["non_zero_month_count"]
    month_limit = settings.history_months if max_months is None else max_months
    if (
        not isinstance(month_count, int)
        or isinstance(month_count, bool)
        or month_count < 0
        or month_count > month_limit
        or not isinstance(non_zero, int)
        or isinstance(non_zero, bool)
        or not 0 <= non_zero <= month_count
        or not isinstance(item["sufficient"], bool)
        or not _is_eur_cents(item["total_eur"])
    ):
        return False
    total_eur = Decimal(str(item["total_eur"]))
    if (non_zero > 0) != (total_eur > 0):
        return False
    sufficient = non_zero >= settings.min_history_months
    if item["status"] == "sufficient":
        return sufficient and item["sufficient"]
    if item["status"] == "insufficient_history":
        return not sufficient and not item["sufficient"]
    return month_count == 0 and non_zero == 0 and item["total_eur"] == 0 and not item["sufficient"]


def _valid_forecast_point(point: object) -> bool:
    return (
        isinstance(point, dict)
        and set(point) == {"SaleAmount", "MonthNameUK"}
        and _is_eur_cents(point["SaleAmount"])
        and isinstance(point["MonthNameUK"], str)
        and bool(point["MonthNameUK"])
    )


def _is_eur_cents(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not amount.is_finite() or amount < 0:
        return False
    as_float = float(amount)
    return isfinite(as_float) and fc.eur_cents(amount) == as_float
