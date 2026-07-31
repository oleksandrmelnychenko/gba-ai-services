"""DB-backed credit-risk SCENARIO suite for the v3 scorecard (creditscore-v3).

Codifies realistic risk scenarios as assertions against the LIVE :8003 service (end-to-end,
via requests) and the in-process scorer (internals). SKIPPED when the DB is unreachable so CI
stays green without a DB; run via `make integration` or `pytest -m integration` against a dev
DB + the running gba-solvency service.

Scenarios:
  COHORT          -- current role-1 buyers with >=EUR250 180+ overdue must all land C/D;
                     debt-free buyers-with-sales must mostly land A/B.
  REGRESSION      -- dynamically selected severe buyers with no controlled limit must remain D.
  GATE            -- current provider-only / non-buyer ids -> applicable=false, score null.
  FORWARD         -- only the model's declared population (debt>0, not already SEV180) is compared
                     exactly with the behavioral scorecard; already-SEV180 has no forward score.
  LEAKAGE         -- changing excluded current-SEV180 magnitude cannot change current score.
  CONTRACT        -- v3 shape complete: all keys, types, sub_factors null, rating in A..D,
                     0<=score<=100, pd in [0,1], contributions nonempty for an applicable buyer,
                     model_version == creditscore-v3.
  EDGE            -- zero-debt buyer -> applicable, high score, no out-of-population forward
                     signal; brand-new buyer -> explicitly insufficient, no fabricated risk;
                     nonexistent client -> 404 live / LookupError in-process; malformed body -> 422.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

pytestmark = pytest.mark.integration

_AS_OF = os.environ.get("SOLVENCY_TEST_AS_OF", datetime.now(UTC).date().isoformat())
SEV180_MIN_EUR = 100.0

NONEXISTENT_CLIENT_ID = 999999999

LIVE_BASE_URL = os.environ.get("SOLVENCY_BASE_URL", "http://127.0.0.1:8003")


def _assert_operational_risk_90d(risk, features: dict) -> None:
    from app.domain.money import round_cent

    payload = risk if isinstance(risk, dict) else risk.model_dump(mode="json")
    overdue_90_plus = float(
        round_cent(
            sum(
                float(features.get(name, 0.0) or 0.0)
                for name in ("overdue_eur_91_180", "overdue_eur_180plus")
            )
        )
    )
    overdue_1_90 = float(
        round_cent(
            sum(
                float(features.get(name, 0.0) or 0.0)
                for name in (
                    "overdue_eur_1_30",
                    "overdue_eur_31_60",
                    "overdue_eur_61_90",
                )
            )
        )
    )
    total_debt = float(
        round_cent(
            float(features.get("total_debt_eur", 0.0) or 0.0)
        )
    )

    if overdue_90_plus >= 100.0:
        expected = ("critical", "already_90_plus", overdue_90_plus)
    elif overdue_1_90 >= 100.0:
        expected = ("high", "will_cross_90_days", overdue_1_90)
    elif total_debt > 0.0:
        expected = ("medium", "current_debt", total_debt)
    else:
        expected = ("low", "no_debt", 0.0)

    assert payload["horizon_days"] == 90
    assert payload["threshold_days"] == 90
    assert payload["band"] == expected[0]
    assert payload["reason_code"] == expected[1]
    assert payload["exposure_eur"] == pytest.approx(expected[2], abs=0.005)


# --------------------------------------------------------------------------------------------
# Skip plumbing: gate on the DB actually being reachable (settings come from .env or env vars),
# not on os.environ alone -- this suite must run wherever the app's own DB config resolves.
# --------------------------------------------------------------------------------------------
def _db_reachable() -> bool:
    try:
        from app.data.db import query

        query("SELECT 1 AS hit", {})
        return True
    except Exception:
        return False


skip_no_db = pytest.mark.skipif(
    not _db_reachable(),
    reason="DB unreachable (configure DB_* via .env or env); run via 'make integration'",
)


_LIVE_PROBE: dict[str, bool] = {}


def _live_reachable() -> bool:
    """Probe the live service lazily (cached). Evaluated at CALL time, not import/collection
    time, so a sandboxed collection phase doesn't wrongly mark the service unreachable."""
    if "ok" not in _LIVE_PROBE:
        try:
            import httpx as requests

            r = requests.get(f"{LIVE_BASE_URL}/health", headers=_api_headers(), timeout=5)
            _LIVE_PROBE["ok"] = r.status_code == 200
        except Exception:
            _LIVE_PROBE["ok"] = False
    return _LIVE_PROBE["ok"]


def _require_live() -> None:
    if not _live_reachable():
        pytest.skip(f"live solvency service not reachable at {LIVE_BASE_URL}")


def _api_headers() -> dict[str, str]:
    from app.core.config import get_settings

    key = get_settings().internal_api_key
    return {"X-Internal-Api-Key": key} if key else {}


# --------------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def service():
    from app.services.solvency import service as _service

    return _service


def _score(service, client_id: int):
    return service.score_client(client_id, None, _AS_OF, 12, use_cache=False)


@pytest.fixture(scope="module")
def overdue_cohort() -> list[dict]:
    """Current role-1 buyers whose 180+ overdue EUR exposure is >= 250, biggest first.

    Mirrors app.risk.dataset._sev180_eur_by_client (the SEV180 label engine) but restricted to
    Buyer-role entities and the >=250 severity floor. Uses EXISTS rather than joining roles so
    multiple role rows cannot multiply monetary exposure.
    """
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 30 client_id, SUM(eur) AS sev_eur
        FROM (
            SELECT cid.ClientID AS client_id,
                   dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof) AS eur
            FROM dbo.ClientInDebt cid
            JOIN dbo.Debt d ON d.ID = cid.DebtID
            JOIN dbo.Agreement a ON a.ID = cid.AgreementID
            WHERE cid.Deleted = 0 AND d.Deleted = 0 AND d.Created <= :asof
                  AND DATEDIFF(day, d.Created, :asof) > a.NumberDaysDebt + 180
                  AND EXISTS (
                      SELECT 1
                      FROM dbo.ClientInRole cir
                      JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
                      WHERE cir.ClientID = cid.ClientID
                            AND cir.Deleted = 0
                            AND ct.[Type] = 0)
        ) t
        GROUP BY client_id
        HAVING SUM(eur) >= 250
        ORDER BY SUM(eur) DESC
        """,
        {"asof": _AS_OF},
    )
    if len(rows) < 10:
        pytest.skip(f"only {len(rows)} >=EUR250 overdue buyers in dev DB; need >=10")
    return [{"client_id": int(r["client_id"]), "sev_eur": float(r["sev_eur"])} for r in rows]


@pytest.fixture(scope="module")
def severe_d_regression_cohort(overdue_cohort) -> list[dict]:
    """At least two current severe buyers with no controlled credit limit.

    This is the live-data replacement for pre-rebuild numeric IDs. The current scorecard's
    independent risk signals (open debt plus no controlled limit) must keep this cohort in D;
    the fixture never selects by score or rating.
    """
    from app.data.db import query
    from app.risk import dataset

    ids = [row["client_id"] for row in overdue_cohort]
    features = dataset.features_many(ids, _AS_OF, 12)
    selected = [
        row
        for row in overdue_cohort
        if features[row["client_id"]]["credit_limit_eur"] == 0.0
    ][:5]
    if len(selected) < 2:
        pytest.skip("fewer than two current severe buyers have no controlled credit limit")
    placeholders = ", ".join(f":cid_{index}" for index in range(len(selected)))
    params = {f"cid_{index}": row["client_id"] for index, row in enumerate(selected)}
    names = {
        int(row["ID"]): str(row["Name"])
        for row in query(
            f"SELECT ID, Name FROM dbo.Client WHERE ID IN ({placeholders})",
            params,
        )
    }
    return [
        {**row, "name": names.get(row["client_id"], str(row["client_id"]))}
        for row in selected
    ]


@pytest.fixture(scope="module")
def clean_cohort() -> list[int]:
    """Up to 12 role-1 buyers with material sales and ZERO open debt rows (Total>0).

    'No overdue' in practice means 'no open debt at all': a buyer can have no 180+ overdue yet
    still carry large 0-180d debt, which the scorecard (correctly) treats as risk. The clean
    cohort is therefore the genuinely debt-free, actively-trading buyer.
    """
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 12 ca.ClientID AS client_id
        FROM dbo.Sale s
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.ClientInRole cir ON cir.ClientID = ca.ClientID
        JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
        WHERE cir.Deleted = 0 AND ct.[Type] = 0
              AND s.Created <= :asof AND s.Created >= DATEADD(month, -12, :asof)
              AND ca.ClientID NOT IN (
                  SELECT cid.ClientID FROM dbo.ClientInDebt cid
                  JOIN dbo.Debt d ON d.ID = cid.DebtID
                  WHERE cid.Deleted = 0 AND d.Deleted = 0 AND d.Total > 0)
        GROUP BY ca.ClientID
        HAVING COUNT(DISTINCT s.ID) >= 10
        ORDER BY COUNT(DISTINCT s.ID) DESC
        """,
        {"asof": _AS_OF},
    )
    if len(rows) < 5:
        pytest.skip(f"only {len(rows)} zero-debt active buyers in dev DB; need >=5")
    return [int(r["client_id"]) for r in rows]


@pytest.fixture(scope="module")
def nonbuyer_ids() -> list[int]:
    """Up to 10 entities with a non-deleted role but NO non-deleted Buyer (Type=0) role."""
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 10 c.ID AS client_id
        FROM dbo.Client c
        WHERE NOT EXISTS (
                  SELECT 1 FROM dbo.ClientInRole cir
                  JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
                  WHERE cir.ClientID = c.ID AND cir.Deleted = 0 AND ct.[Type] = 0)
              AND EXISTS (
                  SELECT 1 FROM dbo.ClientInRole cir2
                  WHERE cir2.ClientID = c.ID AND cir2.Deleted = 0)
        ORDER BY c.ID
        """,
        {},
    )
    if len(rows) < 3:
        pytest.skip("not enough provider-only/non-buyer entities in dev DB")
    return [int(r["client_id"]) for r in rows]


@pytest.fixture(scope="module")
def forward_at_risk_cohort() -> list[dict]:
    """Current material-debt buyers used to prove exact operational 90-day control."""
    from app.risk import dataset

    clients = dataset.buyer_ids()
    labels = dataset.label_sev180(_AS_OF, clients)
    aging = dataset.feat_debt_aging(_AS_OF)
    candidates = sorted(
        [
            (int(row.client_id), float(row.total_debt_eur))
            for row in aging.itertuples()
            if float(row.total_debt_eur) >= 1.0 and labels[int(row.client_id)] == 0
        ],
        key=lambda item: item[1],
        reverse=True,
    )
    selected_ids = [client_id for client_id, _ in candidates[:5]]
    if not selected_ids:
        pytest.skip("no current material-debt, not-yet-SEV180 buyers")
    features = dataset.features_many(selected_ids, _AS_OF, 12)
    return [
        {"client_id": client_id, "features": features[client_id]}
        for client_id in selected_ids
        if client_id in features
    ]


@pytest.fixture(scope="module")
def brand_new_buyer() -> int:
    """A buyer-role entity with NO sales at all (cold-start)."""
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 1 cir.ClientID AS client_id
        FROM dbo.ClientInRole cir
        JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
        WHERE cir.Deleted = 0 AND ct.[Type] = 0
              AND NOT EXISTS (
                  SELECT 1 FROM dbo.ClientAgreement ca
                  JOIN dbo.Sale s ON s.ClientAgreementID = ca.ID
                  WHERE ca.ClientID = cir.ClientID)
        ORDER BY cir.ClientID DESC
        """,
        {},
    )
    if not rows:
        pytest.skip("no brand-new (no-sales) buyer in dev DB")
    return int(rows[0]["client_id"])


# --------------------------------------------------------------------------------------------
# COHORT
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_cohort_overdue_buyers_all_band_c_or_d(service, overdue_cohort):
    """Every >=EUR250 180+-overdue buyer must score C or D (i.e. NOT investment-grade A/B)."""
    misranked = []
    for c in overdue_cohort:
        res = _score(service, c["client_id"])
        assert res.applicable is True, f"{c['client_id']} should be an applicable buyer"
        if res.rating not in {"C", "D"}:
            misranked.append((c["client_id"], res.rating, res.score, round(c["sev_eur"])))
    assert not misranked, (
        f"overdue buyers ranked A/B (should be C/D): {misranked}"
    )


@skip_no_db
def test_material_debt_cohort_has_exact_operational_90_day_control(
    service, forward_at_risk_cohort
):
    for row in forward_at_risk_cohort:
        assert row["features"]["total_debt_eur"] > 0
        assert row["features"]["overdue_eur_180plus"] < SEV180_MIN_EUR
        result = _score(service, row["client_id"])
        assert result.score is not None
        assert result.pd is not None
        assert result.current_model_run_id
        assert result.risk_90d is not None
        _assert_operational_risk_90d(result.risk_90d, row["features"])
        assert result.forward_risk is None
        assert result.forward_risk_status == "not_applicable"
        assert result.forward_risk_reason == "replaced_by_operational_90d"


@skip_no_db
def test_cohort_clean_buyers_mostly_a_or_b(service, clean_cohort):
    """Debt-free, actively-trading buyers should be mostly A/B (the model's 'safe' grade)."""
    ab = 0
    grades = []
    for cid in clean_cohort:
        res = _score(service, cid)
        assert res.applicable is True
        grades.append((cid, res.rating, res.score))
        if res.rating in {"A", "B"}:
            ab += 1
    frac = ab / len(clean_cohort)
    assert frac >= 0.80, (
        f"only {ab}/{len(clean_cohort)} ({frac:.0%}) clean buyers graded A/B; grades={grades}"
    )


# --------------------------------------------------------------------------------------------
# D REGRESSION  (dynamic, survives client-ID re-mints)
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_dynamic_severe_no_limit_regression_cohort_is_band_d(
    service, severe_d_regression_cohort
):
    failures = []
    for row in severe_d_regression_cohort:
        result = _score(service, row["client_id"])
        if (
            result.applicable is not True
            or result.rating != "D"
            or result.score is None
            or result.score >= 65
            or result.pd is None
            or result.pd <= 0.15
        ):
            failures.append(
                (
                    row["client_id"],
                    row["name"],
                    result.rating,
                    result.score,
                    result.pd,
                )
            )
        # A current SEV180 is outside the forward model's declared population.
        assert result.forward_risk is None
    assert not failures, f"dynamic severe/no-limit D regression failures: {failures}"


# --------------------------------------------------------------------------------------------
# GATE  (solvency applies only to buyers)
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_gate_current_provider_only_not_applicable(service, nonbuyer_ids):
    client_id = nonbuyer_ids[0]
    res = _score(service, client_id)
    assert res.applicable is False
    assert res.score is None and res.rating is None and res.pd is None
    assert res.contributions is None and res.forward_risk is None
    assert res.sub_factors is None and res.raw_score is None


@skip_no_db
def test_gate_nonbuyer_cohort_all_not_applicable(service, nonbuyer_ids):
    leaks = []
    for cid in nonbuyer_ids:
        res = _score(service, cid)
        if res.applicable or res.score is not None or res.contributions is not None:
            leaks.append((cid, res.applicable, res.score))
    assert not leaks, f"non-buyer entities produced a score (gate leak): {leaks}"


# --------------------------------------------------------------------------------------------
# TARGET LEAKAGE / MONOTONIC SANITY
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_current_score_is_invariant_to_excluded_sev180_magnitude(overdue_cohort):
    """Live features prove the target itself cannot improve (or alter) the current score.

    ``overdue_eur_180plus`` and its derived share are intentionally excluded from the supervised
    current-state scorecard to prevent target leakage. The old matched-client test varied many
    other features and compared two unrelated buyers; this counterfactual holds every consumed
    feature constant and therefore tests the actual monotonic/leakage contract exactly.
    """
    from app.risk import dataset
    from app.risk.score_current import _card, score_current

    consumed_features = set(_card()["features"])
    assert "overdue_eur_180plus" not in consumed_features
    assert "pct_debt_180plus" not in consumed_features

    ids = [row["client_id"] for row in overdue_cohort[:10]]
    features = dataset.features_many(ids, _AS_OF, 12)
    for client_id in ids:
        baseline_features = features[client_id]
        amplified_features = dict(baseline_features)
        amplified_features["overdue_eur_180plus"] *= 10
        amplified_features["pct_debt_180plus"] = 1.0
        baseline = score_current(baseline_features)
        amplified = score_current(amplified_features)
        assert amplified["score"] == baseline["score"], client_id
        assert amplified["pd"] == baseline["pd"], client_id
        assert amplified["band"] == baseline["band"], client_id


# --------------------------------------------------------------------------------------------
# CONTRACT  (v3 response shape, both in-process and live)
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_contract_inprocess_shape_for_applicable_buyer(service, forward_at_risk_cohort):
    cohort_row = forward_at_risk_cohort[0]
    res = _score(service, cohort_row["client_id"])
    assert res.applicable is True
    assert isinstance(res.score, int) and 0 <= res.score <= 100
    assert res.rating in {"A", "B", "C", "D"}
    assert isinstance(res.pd, float) and 0.0 <= res.pd <= 1.0
    assert res.contributions is not None and len(res.contributions) > 0
    for c in res.contributions:
        assert isinstance(c.feature, str) and c.feature
        assert isinstance(c.points, float)
    assert res.risk_90d is not None
    _assert_operational_risk_90d(res.risk_90d, cohort_row["features"])
    assert res.forward_risk is None
    assert res.forward_risk_status == "not_applicable"
    assert res.forward_risk_reason == "replaced_by_operational_90d"
    assert res.current_model_run_id
    assert res.sub_factors is None  # deprecated, always null in v3
    assert res.model_version == "creditscore-v3"


@skip_no_db
def test_contract_live_response_shape_applicable(forward_at_risk_cohort):
    _require_live()
    import httpx as requests

    cohort_row = forward_at_risk_cohort[0]
    client_id = cohort_row["client_id"]
    r = requests.post(
        f"{LIVE_BASE_URL}/score",
        json={"client_id": client_id, "as_of_date": _AS_OF, "use_cache": False},
        headers=_api_headers(),
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    required = {
        "client_id", "applicable", "score", "rating", "pd", "contributions",
        "risk_90d", "forward_risk", "forward_risk_status", "forward_risk_reason",
        "current_model_run_id", "sub_factors", "model_version",
    }
    assert required.issubset(body.keys()), f"missing keys: {required - set(body.keys())}"
    assert body["client_id"] == client_id
    assert body["applicable"] is True
    assert body["model_version"] == "creditscore-v3"
    assert body["sub_factors"] is None
    assert 0 <= body["score"] <= 100
    assert body["rating"] in {"A", "B", "C", "D"}
    assert 0.0 <= body["pd"] <= 1.0
    assert isinstance(body["contributions"], list) and len(body["contributions"]) > 0
    _assert_operational_risk_90d(body["risk_90d"], cohort_row["features"])
    assert body["forward_risk"] is None
    assert body["forward_risk_status"] == "not_applicable"
    assert body["forward_risk_reason"] == "replaced_by_operational_90d"
    assert body["current_model_run_id"]


@skip_no_db
def test_contract_live_batch_scores_cohort(
    severe_d_regression_cohort, nonbuyer_ids
):
    _require_live()
    import httpx as requests

    severe_ids = [row["client_id"] for row in severe_d_regression_cohort[:2]]
    provider_id = nonbuyer_ids[0]
    ids = [*severe_ids, provider_id]
    r = requests.post(
        f"{LIVE_BASE_URL}/score/batch",
        json={"client_ids": ids, "as_of_date": _AS_OF, "use_cache": False},
        headers=_api_headers(),
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {row["client_id"]: row for row in body["results"]}
    assert set(by_id) == set(ids)
    assert all(by_id[client_id]["rating"] == "D" for client_id in severe_ids)
    assert by_id[provider_id]["applicable"] is False


# --------------------------------------------------------------------------------------------
# EDGE
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_edge_zero_debt_buyer_high_score_without_forward_risk(service, clean_cohort):
    res = _score(service, clean_cohort[0])
    assert res.applicable is True
    assert res.score is not None and res.score >= 90, f"clean buyer scored low: {res.score}"
    assert res.rating in {"A", "B"}
    assert res.forward_risk is None


@skip_no_db
def test_edge_brand_new_buyer_no_crash_sane_defaults(service, brand_new_buyer):
    res = _score(service, brand_new_buyer)
    assert res.applicable is True
    assert res.score is None
    assert res.rating is None
    assert res.pd is None
    assert res.contributions is None
    assert res.data_sufficiency == "insufficient"
    assert res.data_sufficiency_reason
    assert res.forward_risk is None
    assert res.model_version == "creditscore-v3"


@skip_no_db
def test_edge_nonexistent_client_raises_lookup(service):
    with pytest.raises(LookupError):
        service.score_client(NONEXISTENT_CLIENT_ID, None, _AS_OF, 12, use_cache=False)


@skip_no_db
def test_edge_live_nonexistent_client_404():
    _require_live()
    import httpx as requests

    r = requests.post(
        f"{LIVE_BASE_URL}/score",
        json={"client_id": NONEXISTENT_CLIENT_ID},
        headers=_api_headers(),
        timeout=15,
    )
    assert r.status_code == 404, r.text


def test_edge_live_malformed_body_422():
    _require_live()
    import httpx as requests

    r = requests.post(
        f"{LIVE_BASE_URL}/score", json={}, headers=_api_headers(), timeout=10
    )
    assert r.status_code == 422, r.text
