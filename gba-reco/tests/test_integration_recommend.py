"""DB-backed integration smoke — runs against the live dev DB (read-only).

Marked `integration`: SKIPPED when DB env is absent so the default CI job (pytest -q)
stays green without a database. Run via `make integration` / `pytest -m integration`
after exporting DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD (+ REDIS_DB).

These assert the just-fixed correctness behaviour against real entities — the failures the
mocked unit tests could never catch:
- a HEAVY client and a LIGHT client both reach top_n real recommendations (count == top_n);
- the synthetic debt-entry line 25422404 never appears in results (explicit exclusion);
- the co-purchase recommender returns sane, real, non-synthetic items.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

if not os.getenv("DB_PASSWORD"):
    pytest.skip("integration: DB env not configured", allow_module_level=True)

from app.core.config import get_settings  # noqa: E402
from app.data.db import query  # noqa: E402
from app.services.recommendations import copurchase, recommender  # noqa: E402

AS_OF = "2026-06-15"
TOP_N = 10
SYNTHETIC_ID = 25422404


def _clients_by_order_volume() -> list[dict]:
    return query(
        """
        SELECT ca.ClientID AS cid, COUNT(DISTINCT o.ID) AS norders,
               COUNT(DISTINCT oi.ProductID) AS nprod
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID IS NOT NULL
              AND o.Created < :asof
        GROUP BY ca.ClientID
        ORDER BY norders DESC
        """,
        {"asof": AS_OF},
    )


@pytest.fixture(scope="module")
def clients() -> dict[str, int]:
    rows = _clients_by_order_volume()
    assert rows, "no clients with valid product history in dev DB"
    heavy = rows[0]["cid"]
    light_rows = [r for r in rows if 1 <= r["norders"] <= 5 and r["nprod"] >= 3]
    assert light_rows, "no LIGHT client with >=3 distinct products in dev DB"
    return {"heavy": int(heavy), "light": int(light_rows[0]["cid"])}


def test_synthetic_id_is_load_bearing_in_dev_db():
    rows = query(
        """
        SELECT COUNT(DISTINCT ca.ClientID) AS clients
        FROM dbo.ClientAgreement ca
        JOIN dbo.[Order] o ON ca.ID = o.ClientAgreementID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID = :pid
        """,
        {"pid": SYNTHETIC_ID},
    )
    assert rows[0]["clients"] > 100, (
        "25422404 should be a widely-bought synthetic line — if it isn't, the exclusion "
        "assertions below are no longer load-bearing and the fixture must be revisited"
    )
    assert SYNTHETIC_ID in get_settings().synthetic_product_ids


def test_heavy_client_reaches_top_n_and_excludes_synthetic(clients):
    res = recommender.recommend(clients["heavy"], as_of_date=AS_OF, top_n=TOP_N)
    assert res.segment == "HEAVY", f"expected HEAVY, got {res.segment}"
    assert res.count == TOP_N, f"HEAVY client must reach top_n; got {res.count}"
    pids = [r.product_id for r in res.recommendations]
    assert SYNTHETIC_ID not in pids, "synthetic line 25422404 leaked into HEAVY recs"
    assert len(set(pids)) == len(pids), "duplicate product ids in recs"
    assert all(r.product_id > 0 for r in res.recommendations)


def test_light_client_reaches_top_n_and_excludes_synthetic(clients):
    res = recommender.recommend(clients["light"], as_of_date=AS_OF, top_n=TOP_N)
    assert res.segment == "LIGHT", f"expected LIGHT, got {res.segment}"
    assert res.count == TOP_N, f"LIGHT client must reach top_n via backfill; got {res.count}"
    pids = [r.product_id for r in res.recommendations]
    assert SYNTHETIC_ID not in pids, "synthetic line 25422404 leaked into LIGHT recs"
    assert len(set(pids)) == len(pids), "duplicate product ids in recs"


def test_copurchase_returns_sane_items(clients):
    res = copurchase.recommend(clients["heavy"], AS_OF, top_n=TOP_N, include_owned=False)
    assert res.count > 0, "copurchase returned nothing for a HEAVY client"
    pids = [r.product_id for r in res.recommendations]
    assert SYNTHETIC_ID not in pids, "synthetic line 25422404 leaked into copurchase items"
    assert len(set(pids)) == len(pids), "duplicate product ids in copurchase items"
    scores = [r.score for r in res.recommendations]
    assert all(s > 0 for s in scores), "copurchase scores must be positive"
    assert scores == sorted(scores, reverse=True), "copurchase items must be score-ranked"
