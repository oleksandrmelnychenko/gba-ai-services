"""Seller evidence and financial reports use the same Kyiv day for UTC returns."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.data import sale_attribution_repository as source

SALE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.mark.parametrize(("stamp", "expected"), [
    (datetime(2026, 10, 24, 21, 30), "2026-10-25"),
    (datetime(2026, 10, 25, 21, 30), "2026-10-25"),
    (datetime(2026, 10, 25, 22, 30), "2026-10-26"),
    (datetime(2026, 3, 28, 22, 30), "2026-03-29"),
    (datetime(2026, 3, 29, 21, 30), "2026-03-30"),
    (datetime(2026, 12, 31, 22, 30, tzinfo=UTC), "2027-01-01"),
    (datetime(2026, 10, 25, 0, 30, tzinfo=ZoneInfo("Europe/Kyiv")), "2026-10-25"),
    (None, None),
])
def test_facts_return_day_uses_kyiv_without_shifting_stored_payment_day(monkeypatch, stamp, expected):
    def query(sql, params):
        if "IncomePaymentOrderSale" in sql:
            return [dict(sale_id=1, allocation_id=2, payment_id=3,
                         document_at=datetime(2026, 10, 24, 23, 30), source_currency="UAH",
                         amount_eur=12.34, canceled=False)]
        if "SaleReturnItem" in sql:
            return [dict(sale_id=1, return_id=4, return_item_id=5, document_at=stamp,
                         quantity=2, credit_processed=True, recorded_return_credit_eur=5.25,
                         canceled=False)]
        return [dict(sale_id=1, sale_net_uid=SALE, client_id=7, number="INV-1",
                     is_invoice=True, document_at=datetime(2026, 10, 23),
                     shipped_at=None, order_id=8, offer_net_uid=None, document_currency="EUR",
                     canceled=False, merged=False, retained_lines=1, date_basis="native_invoice_kyiv")]

    monkeypatch.setattr(source, "query", query)
    fact = source.facts([SALE])[SALE]
    assert fact["returns"] == [dict(return_id=4, return_item_id=5, document_date=expected,
                                    quantity=2.0, credit_processed=True,
                                    recorded_return_credit_eur=5.25, canceled=False)]
    assert fact["payments"][0]["document_date"] == "2026-10-24"
    assert fact["document_date"] == "2026-10-23"
