"""FastAPI app — GBA Product Intelligence Service (assortment / inventory-health)."""
from __future__ import annotations

import hmac
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import METRICS
from app.data import cache
from app.data import signals_repository as sig
from app.data.db import dispose, get_engine
from app.services import margin_returns, portfolio, stock_health, substitution

log = get_logger("api")
settings = get_settings()

_OPEN_PATHS = {"/health"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()
    if not settings.internal_api_key:
        log.warning("internal_api_key_not_set", note="gba-products running OPEN — set INTERNAL_API_KEY")
    log.info("service_starting", service="gba-products")
    yield
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
    return datetime.now(UTC).strftime("%Y-%m-%d")


@app.get("/health")
def health() -> dict:
    db_ok = True
    try:
        with get_engine().connect() as c:
            c.exec_driver_sql("SELECT 1")
    except Exception:
        db_ok = False
    return {"status": "healthy" if db_ok else "degraded", "db_connected": db_ok,
            "cache_connected": cache.health(), "version": "0.1.0",
            "model_version": settings.model_version}


@app.get("/metrics")
def metrics() -> dict:
    return METRICS.snapshot()


@app.get("/assortment/stock")
def assortment_stock(as_of_date: str | None = None, limit: int = 100) -> dict:
    """Portfolio inventory-health snapshot: on-hand stock bucketed into days-of-cover bands,
    with EUR value per band and the top SKUs by frozen capital. Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    try:
        key = cache.make_key("assortment", "stock", as_of)
        snap = cache.get(key)
        if snap is None:
            snap = stock_health.snapshot(as_of)
            cache.set(key, snap)
        METRICS.record_request((time.time() - started) * 1000)
        out = dict(snap)
        out["rows"] = out.get("rows", [])[:max(0, limit)]
        return out
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_stock_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_stock_failed") from exc


def _portfolio(as_of: str) -> dict:
    key = cache.make_key("assortment", "portfolio", as_of)
    build = cache.get(key)
    if build is None or not _portfolio_cache_compatible(build):
        build = portfolio.build_portfolio(as_of)
        cache.set(key, build)
    return build


def _attach_meta(rows: list[dict]) -> list[dict]:
    meta = sig.product_meta([r["product_id"] for r in rows])
    return [{**r, **{k: meta.get(r["product_id"], {}).get(k)
                     for k in ("name", "vendor_code", "has_analogue", "is_for_sale")}} for r in rows]


def _region_window(window_days: int | None) -> int:
    return max(1, int(window_days or settings.dead_window_days))


def _regional_sales(as_of: str, window_days: int, region_id: int) -> list[dict]:
    key = cache.make_key("assortment", f"region-sales:{region_id}:{window_days}", as_of)
    cached = cache.get(key)
    if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
        return cached["rows"]
    rows = sig.regional_product_sales(as_of, window_days, region_id=region_id)
    cache.set(key, {"rows": rows})
    return rows


def _attach_regional_sales(rows: list[dict], regional_rows: list[dict]) -> list[dict]:
    by_pid = {int(r["product_id"]): r for r in regional_rows}
    out: list[dict] = []
    for row in rows:
        regional = by_pid.get(int(row["product_id"]))
        if regional is None:
            continue
        out.append({
            **row,
            "regional_units": round(float(regional["regional_units"] or 0), 2),
            "regional_revenue_eur": round(float(regional["regional_revenue_eur"] or 0), 2),
            "regional_order_count": int(regional["regional_order_count"] or 0),
            "regional_client_count": int(regional["regional_client_count"] or 0),
            "region_id": int(regional["region_id"]),
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


def _portfolio_cache_compatible(build: dict) -> bool:
    if not isinstance(build, dict):
        return False
    if build.get("model_version") != settings.model_version:
        return False
    rows = build.get("rows")
    if not isinstance(rows, list):
        return False
    if rows and not _PORTFOLIO_ROW_FIELDS.issubset(rows[0]):
        return False
    overview = build.get("overview")
    return isinstance(overview, dict) and "by_action" in overview


@app.get("/assortment/overview")
def assortment_overview(as_of_date: str | None = None) -> dict:
    """Portfolio summary: counts by band / lifecycle / ABC / XYZ + totals + avg health. Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    try:
        build = _portfolio(as_of)
        METRICS.record_request((time.time() - started) * 1000)
        return {"as_of": as_of, "model_version": build["model_version"],
                "count": build["count"], "overview": build["overview"]}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_overview_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_overview_failed") from exc


@app.get("/assortment/health")
def assortment_health(as_of_date: str | None = None, band: str | None = None, abc: str | None = None,
                      xyz: str | None = None, lifecycle: str | None = None,
                      sort: str = "health_asc", limit: int = 100, stocked_only: bool = True,
                      region_id: int | None = None, region_window_days: int | None = None) -> dict:
    """Ranked, filterable assortment action list (the purchasing dashboard). Defaults to the
    on-hand-stocked subset (the actual inventory-health decisions); stocked_only=false includes the
    order-to-demand active catalog. Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    resolved_sort = _SORT_ALIASES.get(sort, sort)
    if resolved_sort not in _SORTS:
        allowed = sorted([*_SORTS, *_SORT_ALIASES])
        raise HTTPException(status_code=400, detail={"error": "unknown_sort", "allowed": allowed})
    if resolved_sort in _REGIONAL_SORTS and region_id is None:
        raise HTTPException(status_code=400, detail={"error": "regional_sort_requires_region_id"})
    try:
        rows = _portfolio(as_of)["rows"]
        if stocked_only:
            rows = [r for r in rows if r["band"] != "order_to_demand"]
        for field, val in (("band", band), ("abc", abc), ("xyz", xyz), ("lifecycle", lifecycle)):
            if val:
                rows = [r for r in rows if r[field] == val]
        win = None
        if region_id is not None:
            win = _region_window(region_window_days)
            rows = _attach_regional_sales(rows, _regional_sales(as_of, win, region_id))
        keyfn, rev = _SORTS[resolved_sort]
        rows = sorted(rows, key=keyfn, reverse=rev)[:max(0, limit)]
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
            "sort": resolved_sort,
            "region_id": region_id,
            "region_window_days": win,
            "count": len(rows),
            "tasks": _attach_meta(rows),
        }
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_health_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_health_failed") from exc


@app.get("/assortment/regions")
def assortment_regions(as_of_date: str | None = None, window_days: int | None = None,
                       limit: int = 50) -> dict:
    """Regional portfolio demand summary by Client.RegionID. Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    win = _region_window(window_days)
    try:
        key = cache.make_key("assortment", f"regions:{win}", as_of)
        cached = cache.get(key)
        rows = cached.get("regions") if isinstance(cached, dict) else None
        if rows is None:
            rows = sig.regional_demand_summary(as_of, win)
            cache.set(key, {"regions": rows})
        rows = rows[:max(0, limit)]
        METRICS.record_request((time.time() - started) * 1000)
        return {"as_of": as_of, "window_days": win, "count": len(rows), "regions": rows}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_regions_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_regions_failed") from exc


@app.get("/product/{product_id}")
def product_profile(product_id: int, as_of_date: str | None = None) -> dict:
    """Full per-SKU 360 profile (the product card). Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    try:
        row = next((r for r in _portfolio(as_of)["rows"] if r["product_id"] == product_id), None)
        meta = sig.product_meta([product_id]).get(product_id, {})
        METRICS.record_request((time.time() - started) * 1000)
        if row is None:
            return {"product_id": product_id, "as_of": as_of, "found": False, **meta}
        return {"as_of": as_of, "found": True, **{**row, **meta}}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("product_profile_failed", product_id=product_id, error=str(exc))
        raise HTTPException(status_code=500, detail="product_profile_failed") from exc


@app.get("/product/{product_id}/regions")
def product_regions(product_id: int, as_of_date: str | None = None, window_days: int | None = None,
                    limit: int = 20) -> dict:
    """Per-product demand split by Client.RegionID. Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    win = _region_window(window_days)
    try:
        rows = sig.regional_product_sales(as_of, win, product_ids=[product_id])
        rows = sorted(rows, key=lambda r: float(r["regional_revenue_eur"] or 0), reverse=True)
        METRICS.record_request((time.time() - started) * 1000)
        return {
            "as_of": as_of,
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
def product_substitutes(product_id: int, as_of_date: str | None = None, limit: int = 20) -> dict:
    """Ranked interchangeable replacements (ProductAnalogue + OE fallback), in-stock + healthy first."""
    started = time.time()
    as_of = as_of_date or _today()
    try:
        lookup = {r["product_id"]: r for r in _portfolio(as_of)["rows"]}
        result = substitution.substitutes(product_id, lookup, limit)
        METRICS.record_request((time.time() - started) * 1000)
        return {"as_of": as_of, **result}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("substitutes_failed", product_id=product_id, error=str(exc))
        raise HTTPException(status_code=500, detail="substitutes_failed") from exc


@app.get("/assortment/margin")
def assortment_margin(as_of_date: str | None = None, limit: int = 20) -> dict:
    """Margin leaders / laggards / below-cost alerts + portfolio margin summary. Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    try:
        rows = _portfolio(as_of)["rows"]
        METRICS.record_request((time.time() - started) * 1000)
        return {"as_of": as_of,
                "leaders": margin_returns.margin_leaders(rows, limit),
                "laggards": margin_returns.margin_laggards(rows, limit),
                "negative": margin_returns.negative_margin(rows),
                "summary": margin_returns.margin_returns_summary(rows)}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_margin_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_margin_failed") from exc


@app.get("/assortment/returns")
def assortment_returns(as_of_date: str | None = None, min_rate: float | None = None,
                       limit: int = 20) -> dict:
    """High-return SKUs + returns summary. Internal-key gated."""
    started = time.time()
    as_of = as_of_date or _today()
    rate = settings.returns_high_min_rate if min_rate is None else min_rate
    try:
        rows = _portfolio(as_of)["rows"]
        METRICS.record_request((time.time() - started) * 1000)
        return {"as_of": as_of, "min_rate": rate,
                "high_returns": margin_returns.high_returns(rows, rate, limit),
                "summary": margin_returns.margin_returns_summary(rows)}
    except Exception as exc:  # noqa: BLE001
        METRICS.record_request((time.time() - started) * 1000, error=True)
        log.error("assortment_returns_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="assortment_returns_failed") from exc
