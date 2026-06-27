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

import pytest

pytestmark = pytest.mark.integration

UAH_CURRENCY_ID = 10038
_DB_ENV = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")


def _db_configured() -> bool:
    return all(os.environ.get(k) for k in _DB_ENV)


skip_no_db = pytest.mark.skipif(
    not _db_configured(),
    reason="DB env not set (DB_HOST/DB_NAME/DB_USER/DB_PASSWORD); run via 'make integration'",
)

_AS_OF = "2026-06-15"


@pytest.fixture(scope="module")
def repo():
    from app.data import solvency_repository as _repo

    return _repo


@pytest.fixture(scope="module")
def service():
    from app.services.solvency import service as _service

    return _service


@pytest.fixture(scope="module")
def uah_client(repo) -> int:
    """A real client whose turnover flows through a UAH agreement in the window."""
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 1 ca.ClientID AS client_id
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        WHERE a.CurrencyID = :uah
              AND oi.IsValidForCurrentSale = 1
              AND oi.ProductID <> 25422404
              AND s.Created > '2000-01-01'
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, -12, :asof)
        GROUP BY ca.ClientID
        HAVING COUNT(DISTINCT s.ID) > 20
               AND SUM(oi.Qty * oi.PricePerItem) > 10000
        ORDER BY SUM(oi.Qty * oi.PricePerItem) DESC
        """,
        {"uah": UAH_CURRENCY_ID, "asof": _AS_OF},
    )
    if not rows:
        pytest.skip("no UAH client with material recent turnover in dev DB")
    return int(rows[0]["client_id"])


@skip_no_db
def test_uah_turnover_bucket_is_not_divided_by_fx_rate(repo, uah_client):
    from app.data.db import query

    buckets = repo.turnover_eur_by_currency(uah_client, _AS_OF, 12, _AS_OF)
    uah = [b for b in buckets if b["currency_id"] == UAH_CURRENCY_ID]
    assert uah, f"client {uah_client} has no UAH bucket"
    service_value = float(uah[0]["turnover_eur"])

    rows = query(
        """
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
              AND oi.ProductID <> 25422404
              AND s.Created > '2000-01-01'
              AND s.Created <= :asof
              AND s.Created >= DATEADD(month, -12, :asof)
        """,
        {"cid": uah_client, "uah": UAH_CURRENCY_ID, "asof": _AS_OF, "fx": _AS_OF},
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
    assert result.client_id == uah_client
    assert 0 <= result.score <= 100
    assert result.rating in {"A", "B", "C", "D"}
    # v3 contract: explainable contributions + 6mo forward early-warning; sub_factors deprecated.
    assert result.pd is not None and 0.0 <= result.pd <= 1.0
    assert result.contributions and len(result.contributions) > 0
    assert result.forward_risk is not None
    assert result.forward_risk.band in {"low", "medium", "high", "very_high"}
    assert result.sub_factors is None
    assert result.model_version == "creditscore-v3"


# --- Buyer-role applicability (solvency applies ONLY to Buyer-role entities) ---

PROVIDER_ONLY_CLIENT_ID = 410170  # Універсал банк АТ — provider-only, no Buyer role


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


@skip_no_db
def test_provider_only_has_no_buyer_role(repo):
    assert repo.has_buyer_role(PROVIDER_ONLY_CLIENT_ID) is False


@skip_no_db
def test_buyer_client_has_buyer_role(repo, buyer_client):
    assert repo.has_buyer_role(buyer_client) is True


@skip_no_db
def test_provider_only_score_not_applicable(service):
    result = service.score_client(PROVIDER_ONLY_CLIENT_ID, None, _AS_OF, 12, use_cache=False)
    assert result.applicable is False
    assert result.score is None
    assert result.rating is None
    assert result.sub_factors is None
    assert result.raw_score is None
    assert result.client_id == PROVIDER_ONLY_CLIENT_ID


@skip_no_db
def test_buyer_client_score_applicable_in_band(service, buyer_client):
    result = service.score_client(buyer_client, None, _AS_OF, 12, use_cache=False)
    assert result.applicable is True
    assert result.score is not None
    assert 0 <= result.score <= 100
    assert result.rating in {"A", "B", "C", "D"}


# --- v3 scorecard sanity: a known severely-overdue buyer must land in band D ---

TRAMP_OIL_CLIENT_ID = 411780  # ТРАМП ОЙЛ — large 180+ overdue exposure, SEV180 positive


@skip_no_db
def test_known_overdue_buyer_is_band_d(service):
    """ТРАМП ОЙЛ scores as high-risk (band D, low score) under the v3 current-state scorecard."""
    result = service.score_client(TRAMP_OIL_CLIENT_ID, None, "2026-06-25", 12, use_cache=False)
    assert result.applicable is True
    assert result.rating == "D"
    assert result.score is not None and result.score < 65
    assert result.pd is not None and result.pd > 0.15  # band D threshold
    assert result.forward_risk is not None
