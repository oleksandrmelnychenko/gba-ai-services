"""HTTP client to gba-reco (cross-sell candidates). Graceful: returns [] on failure."""
from __future__ import annotations

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("reco_client")


def _headers(s) -> dict:
    """Send the reco internal-API key when configured (reco 401s without it when keyed)."""
    return {"X-Internal-Api-Key": s.reco_api_key} if s.reco_api_key else {}


def recommend(customer_id: int, top_n: int = 10, as_of_date: str | None = None,
              path: str = "/recommend", timeout: int | None = None) -> list[dict]:
    """Return reco product list for a client, or [] if reco is unavailable/errors.

    Each item: {product_id, score, rank, segment, source}. We only want DISCOVERY items
    for cross-sell (products the client doesn't already buy) — caller filters by source.
    `path` selects the engine: "/recommend" (v3.2) or "/recommend/copurchase" (item-item, used
    for cross-sell — faster and competitive in eval).
    """
    s = get_settings()
    payload = {"customer_id": customer_id, "top_n": top_n, "include_discovery": True}
    if as_of_date:
        payload["as_of_date"] = as_of_date
    try:
        r = httpx.post(f"{s.reco_url.rstrip('/')}{path}", json=payload,
                       headers=_headers(s), timeout=timeout or s.http_timeout)
        r.raise_for_status()
        return r.json().get("recommendations", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("reco_unavailable", customer_id=customer_id, error=str(exc))
        return []


def send_feedback(customer_id: int, product_ids: list[int], kind: str = "reject") -> bool:
    """Push negative feedback to reco (products the manager dismissed / failed to sell) so the
    recommender stops suggesting them for this client. Best-effort: never raises."""
    if not product_ids:
        return False
    s = get_settings()
    payload = {"customer_id": customer_id, "product_ids": list(product_ids), "kind": kind}
    try:
        r = httpx.post(f"{s.reco_url.rstrip('/')}/feedback", json=payload,
                       headers=_headers(s), timeout=s.http_timeout)
        r.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("reco_feedback_failed", customer_id=customer_id, error=str(exc))
        return False


def is_healthy() -> bool:
    s = get_settings()
    try:
        r = httpx.get(f"{s.reco_url.rstrip('/')}/health", timeout=5)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False
