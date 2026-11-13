"""Kyiv business-month bounds for Mongo-backed task accounting."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from app.domain.models import TaskStatus
from app.services import lifecycle


@pytest.mark.parametrize(
    ("as_of", "expected_start", "expected_end", "duration_hours"),
    [
        (
            "2026-03-29",
            datetime(2026, 2, 28, 22, tzinfo=UTC),
            datetime(2026, 3, 31, 21, tzinfo=UTC),
            743,
        ),
        (
            "2026-10-25",
            datetime(2026, 9, 30, 21, tzinfo=UTC),
            datetime(2026, 10, 31, 22, tzinfo=UTC),
            745,
        ),
    ],
)
def test_business_month_bounds_follow_kyiv_dst(
    as_of: str,
    expected_start: datetime,
    expected_end: datetime,
    duration_hours: int,
) -> None:
    start, end = lifecycle.business_month_bounds_utc(as_of)

    assert start == expected_start
    assert end == expected_end
    assert end - start == timedelta(hours=duration_hours)
    assert start.tzinfo is UTC
    assert end.tzinfo is UTC


def test_first_kyiv_hours_of_new_month_use_new_business_month(monkeypatch) -> None:
    """00:30 Kyiv on Aug 1 is still Jul 31 UTC, but belongs to the August dashboards."""
    mongo_client = mongomock.MongoClient()
    collection = mongo_client["gba_nba_test"]["tasks"]
    monkeypatch.setattr(lifecycle.mongo, "tasks", lambda: collection)

    august_start, september_start = lifecycle.business_month_bounds_utc("2026-08-01")
    assert august_start == datetime(2026, 7, 31, 21, tzinfo=UTC)
    assert september_start == datetime(2026, 8, 31, 21, tzinfo=UTC)

    first_kyiv_hour_aware = datetime(2026, 7, 31, 21, 30, tzinfo=UTC)
    first_kyiv_hour_naive_utc = datetime(2026, 7, 31, 22)
    collection.insert_many(
        [
            {
                "manager_id": 1,
                "status": TaskStatus.DONE.value,
                "generated_at": first_kyiv_hour_aware,
                "resolved_at": first_kyiv_hour_aware,
                "outcome": {"sold": True, "amount": "10.01"},
            },
            {
                # Legacy naive BSON datetime: PyMongo's defined convention is UTC.
                "manager_id": 1,
                "status": TaskStatus.DONE.value,
                "generated_at": first_kyiv_hour_naive_utc,
                "resolved_at": first_kyiv_hour_naive_utc,
                "outcome": {"sold": True, "amount": "2.34"},
            },
            {
                "manager_id": 1,
                "status": TaskStatus.DISMISSED.value,
                "generated_at": august_start,
                "resolved_at": august_start,
            },
            {
                "manager_id": 1,
                "status": TaskStatus.DONE.value,
                "generated_at": august_start - timedelta(microseconds=1),
                "resolved_at": august_start - timedelta(microseconds=1),
                "outcome": {"sold": True, "amount": "999.00"},
            },
            {
                "manager_id": 1,
                "status": TaskStatus.DONE.value,
                "generated_at": september_start,
                "resolved_at": september_start,
                "outcome": {"sold": True, "amount": "999.00"},
            },
        ]
    )

    stats = lifecycle.team_stats(1, "2026-08-01")
    counts = lifecycle.dashboard_counts(1, "2026-08-01")
    completed = {row["status"]: row["count"] for row in counts["completed_vs_open"]}

    assert stats == {
        "active": 0,
        "generated_month": 3,
        "done_month": 2,
        "sold_month": 2,
        "dismissed_month": 1,
        "revenue_month": 12.35,
        "close_rate": 0.667,
        "conversion_rate": 1.0,
    }
    assert completed == {"open": 0, "done": 2, "dismissed": 1}

