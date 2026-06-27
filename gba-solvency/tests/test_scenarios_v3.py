"""DB-backed credit-risk SCENARIO suite for the v3 scorecard (creditscore-v3).

Codifies realistic risk scenarios as assertions against the LIVE :8003 service (end-to-end,
via requests) and the in-process scorer (internals). SKIPPED when the DB is unreachable so CI
stays green without a DB; run via `make integration` or `pytest -m integration` against a dev
DB + the running gba-solvency service.

Scenarios:
  COHORT          -- 30 real role-1 buyers with >=EUR250 180+ overdue must all land C/D, most
                     with forward high/very_high; ~12 zero-debt buyers-with-sales mostly A/B.
  NAMED REGRESSION-- 411780 ТРАМП ОЙЛ, 411801 АБРАМЧЕНКО -> D (were 64/65 under the old expert).
  GATE            -- ~10 provider-only / non-buyer ids -> applicable=false, score/contrib null.
  MONOTONIC       -- within a matched tenure/turnover band, higher 180+ overdue -> not-higher
                     score (rank sanity).
  CONTRACT        -- v3 shape complete: all keys, types, sub_factors null, rating in A..D,
                     0<=score<=100, pd in [0,1], contributions nonempty for an applicable buyer,
                     model_version == creditscore-v3.
  EDGE            -- zero-debt buyer -> applicable, high score, forward low; brand-new buyer
                     (no sales) -> no crash, sane defaults; nonexistent client -> 404 live /
                     LookupError in-process; malformed body -> 422.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration

_AS_OF = "2026-06-25"
SEV180_MIN_EUR = 100.0

# Anchors confirmed from data/current_state_scores.parquet (the trained per-client ground truth)
# and live :8003 at as-of 2026-06-25.
TRAMP_OIL_CLIENT_ID = 411780      # ТРАМП ОЙЛ — large 180+ overdue, parquet score 45.7 / band D
ABRAMCHENKO_CLIENT_ID = 411801    # АБРАМЧЕНКО — parquet score 46.6 / band D
PROVIDER_ONLY_CLIENT_ID = 410170  # Універсал банк АТ — provider-only, no Buyer role
NONEXISTENT_CLIENT_ID = 999999999

LIVE_BASE_URL = os.environ.get("SOLVENCY_BASE_URL", "http://127.0.0.1:8003")


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
    """Up to 30 role-1 buyers whose 180+ overdue EUR exposure is >= 250, biggest first.

    Mirrors app.risk.dataset._sev180_eur_by_client (the SEV180 label engine) but restricted to
    Buyer-role entities and the >=250 severity floor.
    """
    from app.data.db import query

    rows = query(
        """
        SELECT TOP 30
               cid.ClientID AS client_id,
               SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)) AS sev_eur
        FROM dbo.ClientInDebt cid
        JOIN dbo.Debt d ON d.ID = cid.DebtID
        JOIN dbo.Agreement a ON a.ID = cid.AgreementID
        JOIN dbo.ClientInRole cir ON cir.ClientID = cid.ClientID
        JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
        WHERE cid.Deleted = 0 AND d.Deleted = 0 AND d.Created <= :asof
              AND DATEDIFF(day, d.Created, :asof) > a.NumberDaysDebt + 180
              AND cir.Deleted = 0 AND ct.[Type] = 0
        GROUP BY cid.ClientID
        HAVING SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)) >= 250
        ORDER BY SUM(dbo.GetExchangedToEuroValue(d.Total, a.CurrencyID, :asof)) DESC
        """,
        {"asof": _AS_OF},
    )
    if len(rows) < 10:
        pytest.skip(f"only {len(rows)} >=EUR250 overdue buyers in dev DB; need >=10")
    return [{"client_id": int(r["client_id"]), "sev_eur": float(r["sev_eur"])} for r in rows]


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
def test_cohort_overdue_buyers_mostly_forward_high(service, overdue_cohort):
    """Most of the overdue cohort should fire the 6mo early-warning (forward high/very_high)."""
    high = 0
    for c in overdue_cohort:
        res = _score(service, c["client_id"])
        assert res.forward_risk is not None
        if res.forward_risk.band in {"high", "very_high"}:
            high += 1
    frac = high / len(overdue_cohort)
    assert frac >= 0.80, (
        f"only {high}/{len(overdue_cohort)} ({frac:.0%}) overdue buyers flagged forward "
        f"high/very_high; expected the majority"
    )


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
# NAMED REGRESSION  (these were 64/65 under the old expert model; v3 must call them D)
# --------------------------------------------------------------------------------------------
@skip_no_db
@pytest.mark.parametrize(
    "client_id, name",
    [(TRAMP_OIL_CLIENT_ID, "ТРАМП ОЙЛ"), (ABRAMCHENKO_CLIENT_ID, "АБРАМЧЕНКО")],
)
def test_named_regression_high_risk_buyer_is_band_d(service, client_id, name):
    res = _score(service, client_id)
    assert res.applicable is True, f"{name} ({client_id}) must be an applicable buyer"
    assert res.rating == "D", f"{name} ({client_id}) regressed: rating={res.rating}"
    assert res.score is not None and res.score < 65, (
        f"{name} ({client_id}) score {res.score} not low (old expert scored it ~64/65)"
    )
    assert res.pd is not None and res.pd > 0.15, (  # band D PD floor
        f"{name} ({client_id}) PD {res.pd} below band-D threshold"
    )
    assert res.forward_risk is not None


# --------------------------------------------------------------------------------------------
# GATE  (solvency applies only to buyers)
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_gate_provider_only_known_id_not_applicable(service):
    res = _score(service, PROVIDER_ONLY_CLIENT_ID)
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
# MONOTONIC  (rank sanity within a matched tenure/turnover band)
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_monotonic_higher_overdue_not_higher_score(service):
    """Within a matched tenure+turnover band, the MORE-overdue buyer must NOT earn a better
    rating (rank sanity). 'Overdue' is measured on the SAME feature the model consumes
    (overdue_eur_180plus from features_one), so the comparison is apples-to-apples and not
    confounded by other debt-aging signals.

    NOTE on resolution: the v3 scorecard saturates -- once any debt-aging signal trips, the
    score floors in band C/D (~45-65) and the integer is then driven by turnover / debt-line
    count, not overdue magnitude. So we assert at the model's real decision granularity (the
    rating band) and allow a small score tolerance for jitter inside the saturated floor.
    """
    from collections import defaultdict

    from app.data.db import query
    from app.risk import dataset

    rows = query(
        """
        SELECT ca.ClientID AS client_id,
               (DATEDIFF(month, MIN(s.Created), :asof) / 12) AS tenure_band,
               (CASE WHEN SUM(oi.Qty * oi.PricePerItem) >= 100000 THEN 3
                     WHEN SUM(oi.Qty * oi.PricePerItem) >= 20000 THEN 2
                     WHEN SUM(oi.Qty * oi.PricePerItem) >= 5000 THEN 1 ELSE 0 END) AS turn_band
        FROM dbo.Sale s
        JOIN dbo.[Order] o ON o.ID = s.OrderID
        JOIN dbo.OrderItem oi ON oi.OrderID = o.ID
        JOIN dbo.ClientAgreement ca ON ca.ID = s.ClientAgreementID
        JOIN dbo.ClientInRole cir ON cir.ClientID = ca.ClientID
        JOIN dbo.ClientType ct ON ct.ID = cir.ClientTypeID
        WHERE oi.IsValidForCurrentSale = 1 AND oi.ProductID <> 25422404
              AND s.Created > '2000-01-01' AND s.Created <= :asof
              AND s.Created >= DATEADD(month, -12, :asof)
              AND cir.Deleted = 0 AND ct.[Type] = 0
              AND ca.ClientID IN (
                  SELECT cid.ClientID FROM dbo.ClientInDebt cid
                  JOIN dbo.Debt d ON d.ID = cid.DebtID
                  JOIN dbo.Agreement a ON a.ID = cid.AgreementID
                  WHERE cid.Deleted = 0 AND d.Deleted = 0 AND d.Created <= :asof
                        AND DATEDIFF(day, d.Created, :asof) > a.NumberDaysDebt + 180)
        GROUP BY ca.ClientID
        HAVING SUM(oi.Qty * oi.PricePerItem) > 0
        """,
        {"asof": _AS_OF},
    )
    if len(rows) < 4:
        pytest.skip("not enough overdue+active buyers to form matched pairs")

    buckets: dict[tuple, list[int]] = defaultdict(list)
    for r in rows:
        buckets[(int(r["tenure_band"]), int(r["turn_band"]))].append(int(r["client_id"]))
    # keep only bands with >=2 members, cap per band to bound feature pulls
    candidate_ids = [cid for ids in buckets.values() if len(ids) >= 2 for cid in ids[:8]]
    if not candidate_ids:
        pytest.skip("no matched tenure/turnover band has >=2 overdue buyers")

    _RANK = {"A": 0, "B": 1, "C": 2, "D": 3}  # worse rating -> higher rank
    info: dict[int, tuple[float, int, int]] = {}  # cid -> (overdue_180plus, score, rank)
    for cid in candidate_ids:
        feats = dataset.features_one(cid, _AS_OF)
        res = _score(service, cid)
        info[cid] = (float(feats.get("overdue_eur_180plus") or 0.0), res.score, _RANK[res.rating])

    _SCORE_TOL = 6
    pairs_checked = 0
    violations = []
    for ids in buckets.values():
        members = [cid for cid in ids[:8] if cid in info]
        if len(members) < 2:
            continue
        members.sort(key=lambda c: info[c][0])  # by overdue_eur_180plus
        lo, hi = members[0], members[-1]
        od_lo, s_lo, r_lo = info[lo]
        od_hi, s_hi, r_hi = info[hi]
        if od_hi <= max(od_lo, 1.0) * 1.5:  # need a materially larger overdue to be meaningful
            continue
        pairs_checked += 1
        better_rating = r_hi < r_lo
        materially_higher_score = s_hi > s_lo + _SCORE_TOL
        if better_rating or materially_higher_score:
            violations.append(
                (lo, round(od_lo), s_lo, ["A", "B", "C", "D"][r_lo],
                 hi, round(od_hi), s_hi, ["A", "B", "C", "D"][r_hi])
            )

    if pairs_checked == 0:
        pytest.skip("no matched tenure/turnover pairs with a material 180+ overdue gap")
    assert not violations, (
        "monotonicity violated (more overdue -> higher score) for "
        f"{len(violations)}/{pairs_checked} pairs: {violations}"
    )


# --------------------------------------------------------------------------------------------
# CONTRACT  (v3 response shape, both in-process and live)
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_contract_inprocess_shape_for_applicable_buyer(service, overdue_cohort):
    res = _score(service, overdue_cohort[0]["client_id"])
    assert res.applicable is True
    assert isinstance(res.score, int) and 0 <= res.score <= 100
    assert res.rating in {"A", "B", "C", "D"}
    assert isinstance(res.pd, float) and 0.0 <= res.pd <= 1.0
    assert res.contributions is not None and len(res.contributions) > 0
    for c in res.contributions:
        assert isinstance(c.feature, str) and c.feature
        assert isinstance(c.points, float)
    assert res.forward_risk is not None
    assert res.forward_risk.band in {"low", "medium", "high", "very_high"}
    assert isinstance(res.forward_risk.pd, float) and 0.0 <= res.forward_risk.pd <= 1.0
    assert res.sub_factors is None  # deprecated, always null in v3
    assert res.model_version == "creditscore-v3"


@skip_no_db
def test_contract_live_response_shape_applicable():
    _require_live()
    import httpx as requests

    r = requests.post(
        f"{LIVE_BASE_URL}/score",
        json={"client_id": TRAMP_OIL_CLIENT_ID, "as_of_date": _AS_OF, "use_cache": False},
        headers=_api_headers(),
        timeout=30,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    required = {
        "client_id", "applicable", "score", "rating", "pd", "contributions",
        "forward_risk", "sub_factors", "model_version",
    }
    assert required.issubset(body.keys()), f"missing keys: {required - set(body.keys())}"
    assert body["applicable"] is True
    assert body["model_version"] == "creditscore-v3"
    assert body["sub_factors"] is None
    assert 0 <= body["score"] <= 100
    assert body["rating"] in {"A", "B", "C", "D"}
    assert 0.0 <= body["pd"] <= 1.0
    assert isinstance(body["contributions"], list) and len(body["contributions"]) > 0
    assert body["forward_risk"]["band"] in {"low", "medium", "high", "very_high"}


@skip_no_db
def test_contract_live_batch_scores_cohort():
    _require_live()
    import httpx as requests

    ids = [TRAMP_OIL_CLIENT_ID, ABRAMCHENKO_CLIENT_ID, PROVIDER_ONLY_CLIENT_ID]
    r = requests.post(
        f"{LIVE_BASE_URL}/score/batch",
        json={"client_ids": ids, "as_of_date": _AS_OF, "use_cache": False},
        headers=_api_headers(),
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {row["client_id"]: row for row in body["results"]}
    assert by_id[TRAMP_OIL_CLIENT_ID]["rating"] == "D"
    assert by_id[ABRAMCHENKO_CLIENT_ID]["rating"] == "D"
    assert by_id[PROVIDER_ONLY_CLIENT_ID]["applicable"] is False


# --------------------------------------------------------------------------------------------
# EDGE
# --------------------------------------------------------------------------------------------
@skip_no_db
def test_edge_zero_debt_buyer_high_score_forward_low(service, clean_cohort):
    res = _score(service, clean_cohort[0])
    assert res.applicable is True
    assert res.score is not None and res.score >= 90, f"clean buyer scored low: {res.score}"
    assert res.rating in {"A", "B"}
    assert res.forward_risk is not None and res.forward_risk.band == "low"


@skip_no_db
def test_edge_brand_new_buyer_no_crash_sane_defaults(service, brand_new_buyer):
    res = _score(service, brand_new_buyer)
    assert res.applicable is True
    assert res.score is not None and 0 <= res.score <= 100
    assert res.rating in {"A", "B", "C", "D"}
    assert res.forward_risk is not None
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
