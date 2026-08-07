"""FastAPI app — GBA Product Intelligence Service (assortment / inventory-health)."""
from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core import exact_numbers as exact
from app.core.config import get_settings
from app.core.history import combined_history_metadata, day_history_window
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import signals_repository as sig
from app.data.db import dispose, get_engine
from app.domain.models import (
    AbcClass,
    InventoryBand,
    LifecycleStage,
    ProductAnalyticsResponse,
    XyzClass,
)
from app.services import (
    history_policy,
    margin_returns,
    portfolio,
    product_analytics,
    stock_health,
    substitution,
)

log = get_logger("api")
settings = get_settings()
_EXPECTED_SOURCE_HISTORY_START = "2025-01-01"

_OPEN_PATHS = {"/health"}

# The day's portfolio snapshot lives ~25h in Redis (see settings.cache_ttl); this in-process
# loop keeps it FRESH by rebuilding it hourly, so no dashboard request ever pays the cold build.
_PORTFOLIO_REFRESH_SECONDS = 3600


async def _portfolio_refresh_loop() -> None:
    while True:
        as_of = _today()
        key = cache.make_key("assortment", "portfolio", as_of)
        try:
            async with _build_locks.setdefault(key, asyncio.Lock()):
                await asyncio.to_thread(_build_and_cache_portfolio, key, as_of)
            await asyncio.to_thread(_build_and_cache_stock, as_of)
        except Exception as exc:  # noqa: BLE001
            log.warning("portfolio_refresh_failed", error=str(exc))
        await asyncio.sleep(_PORTFOLIO_REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-products running OPEN — set INTERNAL_API_KEY")
    log.info("synthetic_product_resolved", product_id=await asyncio.to_thread(sig.synthetic_product_id))
    refresh_task = asyncio.create_task(_portfolio_refresh_loop())
    log.info("service_starting", service="gba-products")
    yield
    refresh_task.cancel()
    dispose()
    log.info("service_stopped")


app = FastAPI(title="GBA Product Intelligence Service", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_allow_origins,
                   allow_methods=["GET", "POST"], allow_headers=["*"])


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


def _today() -> str:
    return datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d")


def _resolve_as_of(as_of_date: date | None) -> str:
    as_of = as_of_date or date.fromisoformat(_today())
    if as_of < settings.source_history_start_date:
        raise HTTPException(status_code=422, detail="as_of_date_before_source_history_start")
    return as_of.isoformat()


_HISTORY_RESPONSE_FIELDS = (
    "source_history_start",
    "requested_start",
    "effective_start",
    "history_complete",
    "history_fingerprint",
    "history_windows",
)


def _history_response_fields(source: dict) -> dict:
    return {field: source[field] for field in _HISTORY_RESPONSE_FIELDS}


def _day_history_metadata(as_of: str, days: int, name: str) -> dict:
    return combined_history_metadata(
        {
            name: day_history_window(
                as_of,
                days,
                settings.source_history_start_date,
            )
        }
    )


@app.get("/health")
def health() -> dict:
    return _service_health()


def _service_health() -> dict:
    as_of = _resolve_as_of(None)
    history_metadata = history_policy.portfolio_metadata(as_of, settings)
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    cache_ok = cache.health()
    source_readiness = None
    business_ready = False
    business_reason = "database_unavailable" if not db_ok else None
    if db_ok:
        try:
            source_readiness = {
                **sig.stock_source_readiness(),
                **history_metadata,
            }
            business_ready = source_readiness.get("ready") is True
            business_reason = source_readiness.get("reason")
        except Exception as exc:  # noqa: BLE001
            business_reason = "stock_readiness_unavailable"
            log.warning("stock_readiness_failed", error=str(exc))
    source_history_start = settings.source_history_start_date.isoformat()
    source_history_contract_ready = (
        source_history_start == _EXPECTED_SOURCE_HISTORY_START
    )
    source_readiness = {
        **(source_readiness or {}),
        "source_history_start": source_history_start,
    }
    if not source_history_contract_ready:
        source_readiness["ready"] = False
        source_readiness["reason"] = "source_history_start_mismatch"
        business_ready = False
        business_reason = "source_history_start_mismatch"
    is_healthy = db_ok and cache_ok and business_ready
    return {
        "status": "healthy" if is_healthy else "degraded",
        "db_connected": db_ok,
        "cache_connected": cache_ok,
        "business_ready": business_ready,
        "business_reason": business_reason,
        "stock_source_readiness": source_readiness,
        "source_history_start": source_history_start,
        "source_history_contract_ready": source_history_contract_ready,
        "version": "0.1.0",
        "model_version": settings.model_version,
        **history_metadata,
    }


@app.get("/ready")
def ready() -> JSONResponse:
    snapshot = _service_health()
    is_ready = snapshot["status"] == "healthy"
    body = {
        **snapshot,
        "status": "ready" if is_ready else "not_ready",
    }
    return JSONResponse(status_code=200 if is_ready else 503, content=body)


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.get("/assortment/stock")
def assortment_stock(as_of_date: date | None = None, limit: int = 100) -> dict:
    """Portfolio inventory-health snapshot: on-hand stock bucketed into days-of-cover bands,
    with EUR value per band and the top SKUs by frozen capital. Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    try:
        snap = cache.get(cache.make_key("assortment", "stock", as_of))
        if snap is None or not _stock_cache_compatible(snap):
            snap = _build_and_cache_stock(as_of)
        METRICS.record_request((time.time() - started) * 1000)
        out = dict(snap)
        out["rows"] = out.get("rows", [])[:max(0, limit)]
        return out
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_stock_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_stock_failed") from exc


# Single-flight: parallel cold requests for the same portfolio key must trigger ONE build
# (the build is heavy SQL; 6 concurrent dashboard tiles otherwise stampede the DB).
_build_locks: dict[str, asyncio.Lock] = {}
# In-process copy of the day's parsed build: the Redis value is ~9MB JSON, so re-fetching and
# re-parsing it per request costs ~300ms. Redis remains the cross-restart warm layer.
_portfolio_memo: tuple[str, dict] | None = None


def _memoize_portfolio(key: str, as_of: str, build: dict) -> dict:
    # Only the current day's build is pinned in memory (historical as_of queries must not evict
    # the hot snapshot the dashboard serves all day).
    if as_of == _today():
        global _portfolio_memo
        _portfolio_memo = (key, build)
    return build


def _memoized_portfolio(key: str) -> dict | None:
    memo = _portfolio_memo
    if memo is not None and memo[0] == key and _portfolio_cache_compatible(memo[1]):
        return memo[1]
    return None


def _build_and_cache_portfolio(key: str, as_of: str) -> dict:
    started = time.time()
    build = portfolio.build_portfolio(as_of)
    cache.set(key, build)
    _memoize_portfolio(key, as_of, build)
    log.info("portfolio_built", as_of=as_of, count=build.get("count"),
             elapsed_ms=round((time.time() - started) * 1000, 1))
    return build


def _build_and_cache_stock(as_of: str) -> dict:
    snap = stock_health.snapshot(as_of)
    cache.set(cache.make_key("assortment", "stock", as_of), snap)
    return snap


def _stock_cache_compatible(snapshot: object) -> bool:
    if not isinstance(snapshot, dict) or snapshot.get("model_version") != settings.model_version:
        return False
    if not _history_cache_compatible(snapshot, stock=True):
        return False
    try:
        stock_health._validate_snapshot(snapshot)
    except (KeyError, TypeError, ValueError):
        return False
    return True


async def _portfolio(as_of: str) -> dict:
    key = cache.make_key("assortment", "portfolio", as_of)
    build = _memoized_portfolio(key)
    if build is not None:
        return build
    build = await asyncio.to_thread(cache.get, key)
    if build is not None and _portfolio_cache_compatible(build):
        return _memoize_portfolio(key, as_of, build)
    async with _build_locks.setdefault(key, asyncio.Lock()):
        build = _memoized_portfolio(key)
        if build is not None:
            return build
        build = await asyncio.to_thread(cache.get, key)
        if build is None or not _portfolio_cache_compatible(build):
            build = await asyncio.to_thread(_build_and_cache_portfolio, key, as_of)
        else:
            _memoize_portfolio(key, as_of, build)
    return build


def _attach_meta(rows: list[dict], as_of: str) -> list[dict]:
    meta = sig.product_meta([r["product_id"] for r in rows], as_of)
    output: list[dict] = []
    for row in rows:
        pid = exact.positive_int(row.get("product_id"), "portfolio product_id")
        product_meta = meta.get(pid, {})
        if product_meta and exact.positive_int(
            product_meta.get("product_id"),
            "product_meta.product_id",
        ) != pid:
            raise ValueError(f"product metadata identity mismatch for {pid}")
        output.append(
            {
                **row,
                **{
                    key: product_meta.get(key)
                    for key in ("name", "vendor_code", "has_analogue", "is_for_sale")
                },
            }
        )
    if len(output) != len(rows):
        raise ValueError("metadata attachment changed row count")
    return output


def _region_window(window_days: int | None) -> int:
    return max(1, int(window_days or settings.dead_window_days))


def _regional_sales(as_of: str, window_days: int, region_id: int) -> list[dict]:
    key = cache.make_key("assortment", f"region-sales:{region_id}:{window_days}", as_of)
    cached = cache.get(key)
    if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
        return _normalize_product_region_rows(cached["rows"])
    rows = _normalize_product_region_rows(
        sig.regional_product_sales(as_of, window_days, region_id=region_id)
    )
    cache.set(key, {"rows": rows})
    return rows


def _normalize_product_region_rows(rows: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    identities: set[tuple[int, int]] = set()
    for row in rows:
        pid = exact.positive_int(row.get("product_id"), "regional product_id")
        rid = exact.positive_int(row.get("region_id"), "regional region_id")
        identity = (pid, rid)
        if identity in identities:
            raise ValueError(f"duplicate regional product identity {identity}")
        identities.add(identity)
        normalized.append(
            {
                **row,
                "product_id": pid,
                "region_id": rid,
                "regional_units": exact.quantity(
                    row.get("regional_units") or 0,
                    "regional_units",
                ),
                "regional_revenue_eur": exact.money(
                    row.get("regional_revenue_eur") or 0,
                    "regional_revenue_eur",
                ),
                "regional_order_count": exact.non_negative_int(
                    row.get("regional_order_count") or 0,
                    "regional_order_count",
                ),
                "regional_client_count": exact.non_negative_int(
                    row.get("regional_client_count") or 0,
                    "regional_client_count",
                ),
            }
        )
    return normalized


def _normalize_region_summary_rows(rows: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    region_ids: set[int] = set()
    for row in rows:
        rid = exact.positive_int(row.get("region_id"), "region_id")
        if rid in region_ids:
            raise ValueError(f"duplicate region_id {rid}")
        region_ids.add(rid)
        normalized.append(
            {
                **row,
                "region_id": rid,
                "client_count": exact.non_negative_int(
                    row.get("client_count") or 0,
                    "client_count",
                ),
                "order_count": exact.non_negative_int(
                    row.get("order_count") or 0,
                    "order_count",
                ),
                "product_count": exact.non_negative_int(
                    row.get("product_count") or 0,
                    "product_count",
                ),
                "units": exact.quantity(row.get("units") or 0, "region units"),
                "revenue_eur": exact.money(
                    row.get("revenue_eur") or 0,
                    "region revenue_eur",
                ),
            }
        )
    return normalized


def _attach_regional_sales(rows: list[dict], regional_rows: list[dict]) -> list[dict]:
    by_pid: dict[int, dict] = {}
    for regional in _normalize_product_region_rows(regional_rows):
        pid = regional["product_id"]
        if pid in by_pid:
            raise ValueError(f"regional sales returned duplicate product_id {pid}")
        by_pid[pid] = regional
    out: list[dict] = []
    for row in rows:
        regional = by_pid.get(int(row["product_id"]))
        if regional is None:
            continue
        out.append({
            **row,
            "regional_units": regional["regional_units"],
            "regional_revenue_eur": regional["regional_revenue_eur"],
            "regional_order_count": regional["regional_order_count"],
            "regional_client_count": regional["regional_client_count"],
            "region_id": regional["region_id"],
            "region_name": regional.get("region_name"),
        })
    return out


_SORTS = {
    "health_asc": (lambda r: r["health"], False),
    "demand": (lambda r: r["demand_score"], True),
    "margin": (lambda r: r["margin_score"], True),
    "frozen_eur": (lambda r: r["eur_value"], True),
    "revenue": (lambda r: r["revenue_eur"], True),
    "regional_revenue": (lambda r: r.get("regional_revenue_eur", 0), True),
    "regional_units": (lambda r: r.get("regional_units", 0), True),
}
_SORT_ALIASES = {"demand_score": "demand", "margin_score": "margin", "region_revenue": "regional_revenue"}
_REGIONAL_SORTS = {"regional_revenue", "regional_units"}
_PORTFOLIO_ROW_FIELDS = {"health", "demand_score", "margin_score", "action_label"}
_FILTER_VALUES = {
    "band": {b.value for b in InventoryBand} | {"unknown"},
    "abc": {a.value for a in AbcClass} | {"unknown"},
    "xyz": {x.value for x in XyzClass} | {"unknown"},
    "lifecycle": {s.value for s in LifecycleStage} | {"unknown"},
}


def _normalize_filter(field: str, value: str) -> str:
    for canon in _FILTER_VALUES[field]:
        if canon.casefold() == value.casefold():
            return canon
    raise HTTPException(status_code=422,
                        detail={"error": f"unknown_{field}", "allowed": sorted(_FILTER_VALUES[field])})


def _portfolio_cache_compatible(build: dict) -> bool:
    if not isinstance(build, dict):
        return False
    if build.get("model_version") != settings.model_version:
        return False
    if not _history_cache_compatible(build):
        return False
    rows = build.get("rows")
    if not isinstance(rows, list):
        return False
    if rows and not _PORTFOLIO_ROW_FIELDS.issubset(rows[0]):
        return False
    overview = build.get("overview")
    if not isinstance(overview, dict) or "by_action" not in overview:
        return False
    try:
        portfolio._validate_portfolio_result(build)
    except (KeyError, TypeError, ValueError):
        return False
    return True


def _history_cache_compatible(payload: dict, *, stock: bool = False) -> bool:
    try:
        if stock:
            expected = history_policy.stock_metadata(str(payload["as_of"]), settings)
        else:
            expected = history_policy.portfolio_metadata(str(payload["as_of"]), settings)
    except (KeyError, TypeError, ValueError):
        return False
    return all(payload.get(field) == expected[field] for field in _HISTORY_RESPONSE_FIELDS)


@app.get("/assortment/overview")
async def assortment_overview(as_of_date: date | None = None) -> dict:
    """Portfolio summary: counts by band / lifecycle / ABC / XYZ + totals + avg health. Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    try:
        build = await _portfolio(as_of)
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
            "model_version": build["model_version"],
            **_history_response_fields(build),
            "count": build["count"],
            "overview": build["overview"],
        }
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_overview_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_overview_failed") from exc


@app.get("/assortment/health")
async def assortment_health(as_of_date: date | None = None, band: str | None = None, abc: str | None = None,
                      xyz: str | None = None, lifecycle: str | None = None,
                      sort: str = "health_asc", limit: int = 100, stocked_only: bool = True,
                      region_id: int | None = None, region_window_days: int | None = None) -> dict:
    """Ranked, filterable assortment action list (the purchasing dashboard). Defaults to the
    on-hand-stocked subset (the actual inventory-health decisions); stocked_only=false includes the
    order-to-demand active catalog. Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    resolved_sort = _SORT_ALIASES.get(sort, sort)
    if resolved_sort not in _SORTS:
        allowed = sorted([*_SORTS, *_SORT_ALIASES])
        raise HTTPException(status_code=400, detail={"error": "unknown_sort", "allowed": allowed})
    if resolved_sort in _REGIONAL_SORTS and region_id is None:
        raise HTTPException(status_code=400, detail={"error": "regional_sort_requires_region_id"})
    filters = {field: _normalize_filter(field, val)
               for field, val in (("band", band), ("abc", abc), ("xyz", xyz), ("lifecycle", lifecycle))
               if val}
    try:
        build = await _portfolio(as_of)
        rows = build["rows"]
        response_history = _history_response_fields(build)
        if stocked_only:
            rows = [r for r in rows if r["band"] != "order_to_demand"]
        for field, val in filters.items():
            rows = [r for r in rows if r[field] == val]
        win = None
        if region_id is not None:
            win = _region_window(region_window_days)
            regional_rows = await asyncio.to_thread(_regional_sales, as_of, win, region_id)
            rows = _attach_regional_sales(rows, regional_rows)
            windows = history_policy.portfolio_windows(as_of, settings)
            windows["regional"] = day_history_window(
                as_of,
                win,
                settings.source_history_start_date,
            )
            response_history = combined_history_metadata(windows)
        keyfn, rev = _SORTS[resolved_sort]
        rows = sorted(rows, key=keyfn, reverse=rev)[:max(0, limit)]
        tasks = await asyncio.to_thread(_attach_meta, rows, as_of)
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
            **response_history,
            "sort": resolved_sort,
            "region_id": region_id,
            "region_window_days": win,
            "count": len(rows),
            "tasks": tasks,
        }
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_health_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_health_failed") from exc


@app.get("/assortment/regions")
def assortment_regions(as_of_date: date | None = None, window_days: int | None = None,
                       limit: int = 50) -> dict:
    """Regional portfolio demand summary by Client.RegionID. Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    win = _region_window(window_days)
    history_metadata = _day_history_metadata(as_of, win, "regional")
    try:
        key = cache.make_key("assortment", f"regions:{win}", as_of)
        cached = cache.get(key)
        rows = cached.get("regions") if isinstance(cached, dict) else None
        if rows is None:
            rows = _normalize_region_summary_rows(
                sig.regional_demand_summary(as_of, win)
            )
            cache.set(key, {"regions": rows})
        else:
            rows = _normalize_region_summary_rows(rows)
        rows = rows[:max(0, limit)]
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
            **history_metadata,
            "window_days": win,
            "count": len(rows),
            "regions": rows,
        }
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_regions_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_regions_failed") from exc


@app.get("/product/{product_id}")
async def product_profile(
    product_id: Annotated[int, Path(gt=0)],
    as_of_date: date | None = None,
) -> dict:
    """Full per-SKU 360 profile (the product card). Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    try:
        build = await _portfolio(as_of)
        snapshot = await asyncio.to_thread(_product_snapshot, build, product_id, as_of)
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
            **_history_response_fields(build),
            **snapshot,
        }
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("product_profile_failed", product_id=product_id, error=str(exc))
        raise HTTPException(status_code=500, detail="product_profile_failed") from exc


def _product_snapshot(build: dict, product_id: int, as_of: str) -> dict:
    """Existing product-profile fields without the top-level as-of wrapper."""
    product_id = exact.positive_int(product_id, "product_id")
    row = next((r for r in build["rows"] if int(r["product_id"]) == product_id), None)
    meta = sig.product_meta([product_id], as_of).get(product_id, {})
    if meta and exact.positive_int(
        meta.get("product_id"),
        "product_meta.product_id",
    ) != product_id:
        raise ValueError(f"product metadata identity mismatch for {product_id}")
    snapshot = {"product_id": product_id, "found": row is not None}
    if row is not None:
        snapshot.update(row)
    snapshot.update(meta)
    snapshot["product_id"] = product_id
    return snapshot


@app.get("/product/{product_id}/analytics", response_model=ProductAnalyticsResponse)
async def product_sales_analytics(
    product_id: Annotated[int, Path(gt=0)],
    as_of_date: date | None = None,
    months: Annotated[int, Query(ge=1, le=24)] = 12,
) -> ProductAnalyticsResponse:
    """Current product snapshot plus dense actual monthly sales. Internal-key gated.

    The sales window ends at ``as_of`` exclusively, matching the other service signals. Stock fields
    come from the current/non-historical portfolio snapshot (which may be cached); the response
    explicitly discloses that no stock history exists.
    """
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    try:
        build = await _portfolio(as_of)
        snapshot = await asyncio.to_thread(_product_snapshot, build, product_id, as_of)
        window = product_analytics.sales_history_window(
            as_of,
            months,
            settings.source_history_start_date,
        )
        monthly_rows = await asyncio.to_thread(
            sig.monthly_product_sales,
            product_id,
            window.requested_start.isoformat(),
            as_of,
        )
        response = product_analytics.build_product_analytics(
            product_id=product_id,
            as_of=as_of,
            months=months,
            model_version=str(build["model_version"]),
            snapshot=snapshot,
            monthly_rows=monthly_rows,
            source_history_start=settings.source_history_start_date,
        )
        METRICS.record_request((time.time() - started) * 1000)
        return response
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("product_analytics_failed", product_id=product_id, error=str(exc))
        raise HTTPException(status_code=500, detail="product_analytics_failed") from exc


@app.get("/product/{product_id}/regions")
def product_regions(
    product_id: Annotated[int, Path(gt=0)],
    as_of_date: date | None = None,
    window_days: int | None = None,
    limit: int = 20,
) -> dict:
    """Per-product demand split by Client.RegionID. Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    win = _region_window(window_days)
    history_metadata = _day_history_metadata(as_of, win, "regional")
    try:
        rows = _normalize_product_region_rows(
            sig.regional_product_sales(as_of, win, product_ids=[product_id])
        )
        if any(row["product_id"] != product_id for row in rows):
            raise ValueError(f"regional response identity mismatch for product {product_id}")
        rows = sorted(
            rows,
            key=lambda row: exact.decimal_value(
                row["regional_revenue_eur"],
                "regional_revenue_eur",
            ),
            reverse=True,
        )
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
            **history_metadata,
            "window_days": win,
            "product_id": product_id,
            "count": len(rows[:max(0, limit)]),
            "regions": rows[:max(0, limit)],
        }
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("product_regions_failed", product_id=product_id, error=str(exc))
        raise HTTPException(status_code=500, detail="product_regions_failed") from exc


@app.get("/product/{product_id}/substitutes")
async def product_substitutes(
    product_id: Annotated[int, Path(gt=0)],
    as_of_date: date | None = None,
    limit: int = 20,
) -> dict:
    """Ranked interchangeable replacements (ProductAnalogue + OE fallback), in-stock + healthy first."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    try:
        build = await _portfolio(as_of)
        lookup = {r["product_id"]: r for r in build["rows"]}
        result = await asyncio.to_thread(substitution.substitutes, product_id, lookup, limit)
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
            **_history_response_fields(build),
            **result,
        }
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("substitutes_failed", product_id=product_id, error=str(exc))
        raise HTTPException(status_code=500, detail="substitutes_failed") from exc


@app.get("/assortment/margin")
async def assortment_margin(as_of_date: date | None = None, limit: int = 20) -> dict:
    """Margin leaders / laggards / below-cost alerts + portfolio margin summary. Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    try:
        build = await _portfolio(as_of)
        rows = build["rows"]
        METRICS.record_request((time.time() - started) * 1000)
        return {"as_of": as_of, **_history_response_fields(build),
                "leaders": margin_returns.margin_leaders(rows, limit),
                "laggards": margin_returns.margin_laggards(rows, limit),
                "negative": margin_returns.negative_margin(rows),
                "summary": margin_returns.margin_returns_summary(rows)}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_margin_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_margin_failed") from exc


@app.get("/assortment/returns")
async def assortment_returns(as_of_date: date | None = None, min_rate: float | None = None,
                             limit: int = 20) -> dict:
    """High-return SKUs + returns summary. Internal-key gated."""
    started = time.time()
    as_of = _resolve_as_of(as_of_date)
    rate = settings.returns_high_min_rate if min_rate is None else min_rate
    try:
        build = await _portfolio(as_of)
        rows = build["rows"]
        METRICS.record_request((time.time() - started) * 1000)
        return {"as_of": as_of, **_history_response_fields(build), "min_rate": rate,
                "high_returns": margin_returns.high_returns(rows, rate, limit),
                "summary": margin_returns.margin_returns_summary(rows)}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_returns_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_returns_failed") from exc
