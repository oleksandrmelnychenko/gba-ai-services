from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.api import main
from app.services import product_analytics

client = TestClient(main.app)
_headers = {"X-Internal-Api-Key": main.settings.internal_api_key} if main.settings.internal_api_key else {}


def _stub_profile(monkeypatch, *, rows: list[dict] | None = None, meta: dict | None = None) -> None:
    monkeypatch.setattr(
        main,
        "_portfolio",
        lambda as_of: {"as_of": as_of, "model_version": "products-test-v1", "rows": rows or []},
    )
    monkeypatch.setattr(main.sig, "product_meta", lambda product_ids: meta or {})


def test_route_returns_dense_sales_series_and_marks_partial_current_month(monkeypatch):
    _stub_profile(
        monkeypatch,
        rows=[{"product_id": 42, "qty_on_hand": 7.0, "health": 81.5, "band": "healthy"}],
        meta={42: {"product_id": 42, "name": "Test product", "vendor_code": "T-42"}},
    )
    captured: dict = {}

    def monthly_sales(product_id: int, window_start: str, as_of: str) -> list[dict]:
        captured.update(product_id=product_id, window_start=window_start, as_of=as_of)
        return [
            {
                "ym": "2026-05",
                "units": Decimal("3.5"),
                "order_count": 2,
                "revenue_eur": Decimal("35.00"),
                "avg_price_eur": Decimal("10.00"),
            },
            {
                "ym": "2026-07",
                "units": Decimal("2"),
                "order_count": 1,
                "revenue_eur": Decimal("25.00"),
                "avg_price_eur": Decimal("12.50"),
            },
        ]

    monkeypatch.setattr(main.sig, "monthly_product_sales", monthly_sales)

    response = client.get(
        "/product/42/analytics",
        params={"as_of_date": "2026-07-10", "months": 3},
        headers=_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert captured == {"product_id": 42, "window_start": "2026-05-01", "as_of": "2026-07-10"}
    assert body["product_id"] == 42
    assert body["as_of"] == "2026-07-10"
    assert body["model_version"] == "products-test-v1"
    assert body["window"] == {
        "months": 3,
        "start": "2026-05-01",
        "end_exclusive": "2026-07-10",
        "includes_partial_current_month": True,
    }
    assert body["snapshot"]["found"] is True
    assert body["snapshot"]["name"] == "Test product"
    assert body["sales_series"] == [
        {
            "month": "2026-05",
            "period_start": "2026-05-01",
            "period_end_exclusive": "2026-06-01",
            "is_complete": True,
            "units": 3.5,
            "order_count": 2,
            "revenue_eur": 35.0,
            "avg_price_eur": 10.0,
        },
        {
            "month": "2026-06",
            "period_start": "2026-06-01",
            "period_end_exclusive": "2026-07-01",
            "is_complete": True,
            "units": 0.0,
            "order_count": 0,
            "revenue_eur": 0.0,
            "avg_price_eur": None,
        },
        {
            "month": "2026-07",
            "period_start": "2026-07-01",
            "period_end_exclusive": "2026-07-10",
            "is_complete": False,
            "units": 2.0,
            "order_count": 1,
            "revenue_eur": 25.0,
            "avg_price_eur": 12.5,
        },
    ]
    assert body["data_quality"]["stock_is_current"] is True
    assert body["data_quality"]["stock_history_available"] is False
    assert body["data_quality"]["sales_date_field"] == "Order.Created"


def test_route_returns_zero_months_for_product_without_sales(monkeypatch):
    _stub_profile(
        monkeypatch,
        meta={77: {"product_id": 77, "name": "Catalog only product"}},
    )
    monkeypatch.setattr(main.sig, "monthly_product_sales", lambda *args: [])

    response = client.get(
        "/product/77/analytics",
        params={"as_of_date": "2026-01-01", "months": 2},
        headers=_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["snapshot"]["found"] is False
    assert body["snapshot"]["name"] == "Catalog only product"
    assert [point["month"] for point in body["sales_series"]] == ["2025-12", "2026-01"]
    assert all(point["units"] == 0 and point["order_count"] == 0 for point in body["sales_series"])
    assert all(point["revenue_eur"] == 0 and point["avg_price_eur"] is None
               for point in body["sales_series"])
    assert body["sales_series"][-1]["period_start"] == body["sales_series"][-1]["period_end_exclusive"]
    assert body["sales_series"][-1]["is_complete"] is False


def test_route_validates_date_product_id_and_month_bounds_before_query(monkeypatch):
    def should_not_run(*args, **kwargs):
        raise AssertionError("validated route must not access analytics dependencies")

    monkeypatch.setattr(main, "_portfolio", should_not_run)
    monkeypatch.setattr(main.sig, "monthly_product_sales", should_not_run)

    requests = [
        ("/product/42/analytics", {"months": 0}),
        ("/product/42/analytics", {"months": 25}),
        ("/product/42/analytics", {"months": "twelve"}),
        ("/product/not-an-id/analytics", {}),
        ("/product/42/analytics", {"as_of_date": "not-a-date"}),
    ]
    for path, params in requests:
        assert client.get(path, params=params, headers=_headers).status_code == 422


def test_malformed_repository_bucket_is_reported_as_generic_endpoint_failure(monkeypatch):
    _stub_profile(monkeypatch)
    monkeypatch.setattr(
        main.sig,
        "monthly_product_sales",
        lambda *args: [{"ym": "bad-month", "units": 1, "order_count": 1, "revenue_eur": 10}],
    )

    response = client.get(
        "/product/42/analytics",
        params={"as_of_date": "2026-07-10", "months": 3},
        headers=_headers,
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "product_analytics_failed"}


def test_builder_rejects_duplicate_months_and_non_finite_numbers():
    base = {
        "product_id": 1,
        "as_of": "2026-07-10",
        "months": 1,
        "model_version": "test",
        "snapshot": {"product_id": 1, "found": True},
    }

    duplicate = [
        {"ym": "2026-07", "units": 1, "order_count": 1, "revenue_eur": 10},
        {"ym": "2026-07", "units": 2, "order_count": 1, "revenue_eur": 20},
    ]
    invalid = [{"ym": "2026-07", "units": float("inf"), "order_count": 1, "revenue_eur": 10}]

    for rows in (duplicate, invalid):
        try:
            product_analytics.build_product_analytics(**base, monthly_rows=rows)
        except ValueError:
            pass
        else:
            raise AssertionError("malformed repository aggregates must be rejected")
