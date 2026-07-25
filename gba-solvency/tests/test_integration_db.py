"""DB-backed integration smoke against the dev DB (ConcordDb_V5).

SKIPPED when the DB env is absent so CI stays green without a DB; run via `make integration`
or `pytest -m integration` after exporting DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD/REDIS_DB.

These reproduce what only live smoke caught: a real client with non-EUR (UAH) agreements is
scored end-to-end, and turnover_eur_by_currency is asserted to be the un-converted, already-EUR
magnitude (NOT divided by the ~52 UAH->EUR rate -- the x52 regression). A phantom client must
raise LookupError, and a real client must score within 0..100.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.integration

UAH_CURRENCY_ID = 10038
_DB_ENV = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _db_configured() -> bool:
    if all(os.environ.get(k) for k in _DB_ENV):
        return True
    try:
        from app.core.config import get_settings

        settings = get_settings()
        return bool(
            settings.db_host
            and settings.db_name
            and settings.db_user
            and settings.db_password
        )
    except Exception:
        return False


skip_no_db = pytest.mark.skipif(
    not _db_configured(),
    reason="DB env not set (DB_HOST/DB_NAME/DB_USER/DB_PASSWORD); run via 'make integration'",
)


def _settings_db_configured() -> bool:
    """Also honor the service's local .env, which pydantic-settings loads in dev."""
    try:
        from app.core.config import get_settings

        settings = get_settings()
        return bool(
            settings.db_host
            and settings.db_name
            and settings.db_user
            and settings.db_password
        )
    except Exception:
        return False


skip_no_settings_db = pytest.mark.skipif(
    not _settings_db_configured(),
    reason="DB settings unavailable; configure service .env or DB_* environment variables",
)

_CURRENT_AS_OF = datetime.now(UTC).strftime("%Y-%m-%d")
_AS_OF = os.environ.get("SOLVENCY_TEST_AS_OF", _CURRENT_AS_OF)


@pytest.fixture(scope="module")
def repo():
    from app.data import solvency_repository as _repo

    return _repo


@pytest.fixture(scope="module")
def service():
    from app.services.solvency import service as _service

    return _service


@pytest.fixture(scope="module")
def source_history_start() -> str:
    from app.core.config import get_settings

    return get_settings().source_history_start_date.isoformat()


@pytest.fixture(scope="module")
def uah_client(repo, source_history_start) -> int:
    """A real client whose turnover flows through a UAH agreement in the window."""
    from app.data.db import query

    placeholder, synthetic = repo._synthetic_not_in()
    rows = query(
        f"""
        SELECT TOP 1 ca.ClientID AS client_id
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE a.CurrencyID = :uah
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {placeholder}
              AND s.Created >= :history_start
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, -12, :asof)
        GROUP BY ca.ClientID
        HAVING COUNT(DISTINCT s.ID) > 20
               AND SUM(oi.Qty * oi.PricePerItem) > 10000
        ORDER BY SUM(oi.Qty * oi.PricePerItem) DESC
        """,
        {
            "uah": UAH_CURRENCY_ID,
            "asof": _AS_OF,
            "history_start": source_history_start,
            **synthetic,
        },
    )
    if not rows:
        pytest.skip("no UAH client with material recent turnover in dev DB")
    return int(rows[0]["client_id"])


@skip_no_db
def test_uah_turnover_bucket_is_not_divided_by_fx_rate(
    repo, uah_client, source_history_start
):
    from app.data.db import query

    placeholder, synthetic = repo._synthetic_not_in()
    buckets = repo.turnover_eur_by_currency(uah_client, _AS_OF, 12, _AS_OF)
    uah = [b for b in buckets if b["currency_id"] == UAH_CURRENCY_ID]
    assert uah, f"client {uah_client} has no UAH bucket"
    service_value = float(uah[0]["turnover_eur"])

    rows = query(
        f"""
        SELECT
            ISNULL(SUM(oi.Qty * oi.PricePerItem), 0) AS no_convert,
            ISNULL(SUM(
                dbo.GetExchangedToEuroValue(oi.Qty * oi.PricePerItem, a.CurrencyID, :fx)
            ), 0) AS buggy_convert
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE ca.ClientID = :cid
              AND a.CurrencyID = :uah
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID NOT IN {placeholder}
              AND s.Created >= :history_start
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, -12, :asof)
        """,
        {
            "cid": uah_client,
            "uah": UAH_CURRENCY_ID,
            "asof": _AS_OF,
            "fx": _AS_OF,
            "history_start": source_history_start,
            **synthetic,
        },
    )
    no_convert = float(rows[0]["no_convert"])
    buggy_convert = float(rows[0]["buggy_convert"])

    assert service_value == pytest.approx(no_convert, rel=1e-6)
    assert buggy_convert > 0
    fx_rate = no_convert / buggy_convert
    assert fx_rate > 10.0, (
        f"UAH->EUR rate {fx_rate:.1f} unexpectedly low; cannot demonstrate the x52 gap"
    )
    assert service_value > buggy_convert * (fx_rate / 2.0), (
        f"turnover bucket {service_value:.0f} looks divided by the FX rate "
        f"(buggy x52 value would be {buggy_convert:.0f}) -- the over-conversion regressed"
    )


@skip_no_db
def test_uah_bucket_same_order_of_magnitude_as_eur_engine(repo, uah_client):
    buckets = repo.turnover_eur_by_currency(uah_client, _AS_OF, 12, _AS_OF)
    uah = [b for b in buckets if b["currency_id"] == UAH_CURRENCY_ID]
    assert uah
    bucket_value = float(uah[0]["turnover_eur"])
    engine_value = repo.turnover_eur(uah_client, _AS_OF, 12, _AS_OF)
    assert engine_value > 0
    assert bucket_value == pytest.approx(engine_value, rel=1e-6)


@skip_no_db
def test_nonexistent_client_raises_lookup(service):
    with pytest.raises(LookupError):
        service.score_client(999999999, None, _AS_OF, 12, use_cache=False)


@skip_no_db
def test_real_uah_client_scores_in_band(service, uah_client):
    result = service.score_client(uah_client, None, _AS_OF, 12, use_cache=False)
    features = service.risk_dataset.features_one(uah_client, _AS_OF, 12)
    assert result.client_id == uah_client
    assert 0 <= result.score <= 100
    assert result.rating in {"A", "B", "C", "D"}
    # v3 contract: explainable contributions plus a 6mo signal only inside that model's
    # declared population (positive debt, not already SEV180); sub_factors are deprecated.
    assert result.pd is not None and 0.0 <= result.pd <= 1.0
    assert result.contributions and len(result.contributions) > 0
    if (
        features["total_debt_eur"] <= 0
        or features["overdue_eur_180plus"] >= service.risk_dataset.SEV180_MIN_EUR
    ):
        assert result.forward_risk is None
        assert result.forward_risk_status == "not_applicable"
    else:
        assert result.forward_risk is None
        assert result.forward_risk_status == "model_unavailable"
        assert "3 < 30" in result.forward_risk_reason
    assert result.sub_factors is None
    assert result.model_version == "creditscore-v3"


def _return_anomaly_client(repo, having: str) -> int | None:
    from app.data.db import query

    placeholder, synthetic = repo._synthetic_not_in()
    rows = query(
        f"""
        SELECT TOP 1 sr.ClientID AS client_id
        FROM dbo.SaleReturnItem sri
        JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
        JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
             AND sr.Deleted = 0 AND sr.IsCanceled = 0
        WHERE sri.Deleted = 0
              AND oi.ProductID IS NOT NULL
              AND oi.ProductID NOT IN {placeholder}
              AND sr.FromDate <= :asof
              AND sr.FromDate >= DATEADD(month, -12, :asof)
        GROUP BY sr.ClientID, sri.SaleReturnID, sri.OrderItemID
        HAVING {having}
        ORDER BY sr.ClientID
        """,
        {"asof": _CURRENT_AS_OF, **synthetic},
    )
    return int(rows[0]["client_id"]) if rows else None


def _assert_return_rate_matches_canonical_qty(repo, client_id: int):
    from app.data.db import query

    placeholder, synthetic = repo._synthetic_not_in()
    rows = query(
        f"""
        SELECT
            (
                SELECT ISNULL(SUM(sri.Qty), 0)
                FROM dbo.SaleReturnItem sri
                JOIN dbo.OrderItem oi ON oi.ID = sri.OrderItemID
                JOIN dbo.SaleReturn sr ON sr.ID = sri.SaleReturnID
                     AND sr.Deleted = 0 AND sr.IsCanceled = 0
                WHERE sri.Deleted = 0
                      AND oi.ProductID IS NOT NULL
                      AND oi.ProductID NOT IN {placeholder}
                      AND sr.ClientID = :cid
                      AND sr.FromDate <= :asof
                      AND sr.FromDate >= DATEADD(month, -12, :asof)
            ) AS return_qty,
            (
                SELECT ISNULL(SUM(oi.Qty), 0)
                FROM dbo.Sale s
                JOIN dbo.[Order] o ON o.ID = s.OrderID
                JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
                JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
                WHERE ca.ClientID = :cid
                      AND oi.IsValidForCurrentSale = 1
                      AND oi.ProductID NOT IN {placeholder}
                      AND s.Created <= :asof
                      AND s.Created >= DATEADD(month, -12, :asof)
            ) AS sold_qty
        """,
        {"cid": client_id, "asof": _CURRENT_AS_OF, **synthetic},
    )
    sold_qty = float(rows[0]["sold_qty"] or 0)
    if sold_qty <= 0:
        pytest.skip(f"return anomaly client {client_id} has no valid sold qty in the window")
    expected = float(rows[0]["return_qty"] or 0) / sold_qty
    assert repo.return_qty_rate(client_id, _CURRENT_AS_OF, 12) == pytest.approx(
        expected, rel=0, abs=1e-12
    )


@skip_no_settings_db
def test_partial_return_rate_uses_salereturnitem_qty(repo):
    client_id = _return_anomaly_client(
        repo, "SUM(sri.Qty) > 0 AND SUM(sri.Qty) < MAX(oi.Qty)"
    )
    if client_id is None:
        pytest.skip("no partial active return in the current integration dataset")
    _assert_return_rate_matches_canonical_qty(repo, client_id)


@skip_no_settings_db
def test_multiple_return_rows_are_summed(repo):
    client_id = _return_anomaly_client(
        repo, "COUNT(*) > 1 AND SUM(sri.Qty) <> MAX(sri.Qty)"
    )
    if client_id is None:
        pytest.skip("no multi-row active return in the current integration dataset")
    _assert_return_rate_matches_canonical_qty(repo, client_id)


# --- Buyer-role applicability (solvency applies ONLY to Buyer-role entities) ---


@pytest.fixture(scope="module")
def buyer_client(repo) -> int:
    """A real entity that has a non-deleted Buyer role (ClientType.[Type]=0)."""
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 1 cir.ClientID AS client_id
        FROM dbo.ClientInRole cir
        JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
        WHERE cir.Deleted = 0 AND ct.[Type] = 0
        ORDER BY cir.ClientID
        """,
    )
    if not rows:
        pytest.skip("no Buyer-role client found in dev DB")
    return int(rows[0]["client_id"])


@pytest.fixture(scope="module")
def provider_only_client() -> int:
    """A current entity with at least one role but no active Buyer role."""
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 1 c.ID AS client_id
        FROM dbo.Client c
        WHERE c.Deleted = 0
              AND NOT EXISTS (
                  SELECT 1
                  FROM dbo.ClientInRole cir
                  JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
                  WHERE cir.ClientID = c.ID
                        AND cir.Deleted = 0
                        AND ct.[Type] = 0)
              AND EXISTS (
                  SELECT 1
                  FROM dbo.ClientInRole cir
                  WHERE cir.ClientID = c.ID AND cir.Deleted = 0)
        ORDER BY c.ID
        """,
        {},
    )
    if not rows:
        pytest.skip("no current provider-only/non-buyer entity")
    return int(rows[0]["client_id"])


@skip_no_db
def test_provider_only_has_no_buyer_role(repo, provider_only_client):
    assert repo.has_buyer_role(provider_only_client) is False


@skip_no_db
def test_buyer_client_has_buyer_role(repo, buyer_client):
    assert repo.has_buyer_role(buyer_client) is True


@skip_no_db
def test_provider_only_score_not_applicable(service, provider_only_client):
    result = service.score_client(provider_only_client, None, _AS_OF, 12, use_cache=False)
    assert result.applicable is False
    assert result.score is None
    assert result.rating is None
    assert result.sub_factors is None
    assert result.raw_score is None
    assert result.client_id == provider_only_client


@skip_no_db
def test_buyer_client_score_applicable_in_band(service, buyer_client):
    result = service.score_client(buyer_client, None, _AS_OF, 12, use_cache=False)
    assert result.applicable is True
    assert result.score is not None
    assert 0 <= result.score <= 100
    assert result.rating in {"A", "B", "C", "D"}


# --- v3 scorecard sanity: a current severe/no-limit buyer must land in band D ---


@pytest.fixture(scope="module")
def severe_no_limit_buyer() -> int:
    """Resolve a live D-regression fixture by business signals, never by score/rating."""
    from app.risk import dataset

    clients = dataset.buyer_ids()
    labels = dataset.label_sev180(_AS_OF, clients)
    severe_ids = [client_id for client_id in clients if labels[client_id] == 1]
    features = dataset.features_many(severe_ids, _AS_OF, 12)
    selected = [
        client_id
        for client_id in severe_ids
        if features[client_id]["credit_limit_eur"] == 0.0
    ]
    if not selected:
        pytest.skip("no current SEV180 buyer without a controlled credit limit")
    return selected[0]


@skip_no_db
def test_current_severe_no_limit_buyer_is_band_d(service, severe_no_limit_buyer):
    result = service.score_client(
        severe_no_limit_buyer, None, _AS_OF, 12, use_cache=False
    )
    assert result.applicable is True
    assert result.rating == "D"
    assert result.score is not None and result.score < 65
    assert result.pd is not None and result.pd > 0.15  # band D threshold
    assert result.forward_risk is None
