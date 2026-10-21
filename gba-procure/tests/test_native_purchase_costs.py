"""Offline trust-boundary tests; no native SQL, HTTP, cache or model access."""

from __future__ import annotations

import copy
import io
import json
import socket
from datetime import date
from decimal import Decimal, localcontext
from email.message import Message
from urllib.error import HTTPError

import pytest

from app.core.config import Settings
from app.data import native_purchase_costs as costs
from app.data.native_purchase_costs import (
    NativePurchaseCostClient,
    NativePurchaseCostError,
    PurchaseCostRequest,
    aggregate_verified_costs,
    parse_purchase_cost_response,
)

RECEIPT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FAILED_RECEIPT = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
KEY = "test-only-dedicated-key-for-native-costs-0001"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refused(*_args, **_kwargs):
        raise AssertionError("Network is forbidden in this suite")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket.socket, "connect", refused)


def request(products=(101,), supplier=7, as_of=date(2026, 9, 8), history=30):
    return PurchaseCostRequest(products, supplier, as_of, history)


def row(identity=1, product=101, cost="10.0000", supplier=7, agreement=7001, unit=1):
    known = cost is not None
    return {
        "observation_id": identity,
        "product_id": product,
        "supplier_id": supplier,
        "supplier_client_agreement_id": agreement,
        "document_id": 2000 + identity,
        "document_date_utc": "2026-09-03T12:00:00Z" if known else None,
        "unit_id": unit,
        "unit_net_cost_eur": cost,
        "status": "known" if known else "unknown",
        "reason_codes": [] if known else ["financial_multiset_mismatch"],
        "evidence_receipt_id": RECEIPT if known else FAILED_RECEIPT,
        "evidence_line_ordinals": [identity - 1] if known else [],
        "financial_fingerprint": "c" * 64,
    }


def payload(req=None, rows=None):
    req = req or request()
    return {
        **req.payload(),
        "scope": "CurrentPostedReceipts",
        "resolver_version": "current-posted-receipt-cost-v1",
        "currency": "EUR",
        "observation_started_at_utc": "2026-09-08T02:10:00.0000001+00:00",
        "observation_completed_at_utc": "2026-09-08T02:10:01.0000002+00:00",
        "fingerprint": "A" * 64,
        "publications": [
            {
                "for_amg": False,
                "receipt_id": RECEIPT,
                "committed_at_utc": "2026-09-05T11:00:00Z",
                "contract_version": "GBA-ONEC-INBOUND-V7",
                "canonical_chain_sha256": "b" * 64,
                "validated_at_utc": "2026-09-08T02:10:01.0000002+00:00",
            }
        ],
        "observations": rows if rows is not None else [row()],
    }


def parse(data, req=None):
    return parse_purchase_cost_response(json.dumps(data).encode(), req or request())


def test_partial_observation_median_keeps_zero_and_exact_agreements_with_coverage():
    req = request((101, 102), supplier=None)
    data = payload(
        req,
        [
            row(cost="0"),
            row(2, cost="0.0001", agreement=7002),
            row(3, cost="0.12345678"),
            row(4, cost=None),
            row(5, cost="99", supplier=8, agreement=8001),
        ],
    )
    summary = aggregate_verified_costs((parse(data, req),))
    supplier = next(group for group in summary.by_product_supplier if group.supplier_id == 7)
    assert supplier.median_unit_net_cost_eur == Decimal("0.0001")
    assert supplier.median_float == 0.0001
    assert (supplier.observation_count, supplier.known_count, supplier.unknown_count) == (4, 3, 1)
    assert supplier.supplier_client_agreement_ids == (7001, 7002)
    assert supplier.coverage == "partial"
    assert supplier.reason_counts == (("financial_multiset_mismatch", 1),)
    empty = next(group for group in summary.by_product if group.product_id == 102)
    assert empty.coverage == "no_observations" and empty.median_float is None
    assert summary.coherent_snapshot


def test_even_median_uses_exact_fraction_and_half_up_despite_decimal_context():
    data = payload(rows=[row(cost="0"), row(2, cost="0.0001")])
    with localcontext() as context:
        context.prec = 2
        group = aggregate_verified_costs((parse(data),)).by_product[0]
    assert group.median_unit_net_cost_eur == Decimal("0.0001")
    assert group.median_float == 0.0001


def test_large_price_and_tiny_increment_do_not_silently_round_through_float():
    data = payload(rows=[row(cost="900000000000000.0000"), row(2, cost="900000000000000.0001")])
    with localcontext() as context:
        context.prec = 3
        group = aggregate_verified_costs((parse(data),)).by_product[0]
    assert group.median_unit_net_cost_eur == Decimal("900000000000000.0001")
    assert group.median_float is None
    assert group.aggregation_reason == "cost_float_precision_unsupported"


def test_all_unknown_has_no_median_and_known_zero_is_not_missing():
    unknown = aggregate_verified_costs((parse(payload(rows=[row(cost=None)])),)).by_product[0]
    zero = aggregate_verified_costs((parse(payload(rows=[row(cost="0.0000")])),)).by_product[0]
    assert unknown.coverage == "unknown" and unknown.median_float is None
    assert zero.coverage == "complete" and zero.median_float == 0.0


def test_same_product_with_incompatible_current_units_has_no_cost_scalar():
    group = aggregate_verified_costs((parse(payload(rows=[row(), row(2, unit=2)])),)).by_product[0]
    assert group.median_unit_net_cost_eur is None and group.median_float is None
    assert group.unit_ids == (1, 2) and group.aggregation_reason == "mixed_current_units"


@pytest.mark.parametrize(
    "path,value",
    [
        (("version",), True),
        (("version",), 2),
        (("scope",), "HistoricReceipts"),
        (("resolver_version",), "other"),
        (("currency",), "UAH"),
        (("fingerprint",), "xyz"),
        (("product_ids",), [101, 101]),
        (("product_ids",), [102]),
        (("product_ids",), [True]),
        (("supplier_id",), 8),
        (("supplier_id",), True),
        (("history_days",), 29),
        (("as_of_exclusive",), "2026-09-09"),
        (("observations", 0, "observation_id"), True),
        (("observations", 0, "product_id"), 102),
        (("observations", 0, "supplier_id"), 8),
        (("observations", 0, "supplier_client_agreement_id"), None),
        (("observations", 0, "unit_id"), None),
        (("observations", 0, "document_id"), 0),
        (("observations", 0, "unit_net_cost_eur"), 10),
        (("observations", 0, "unit_net_cost_eur"), None),
        (("observations", 0, "unit_net_cost_eur"), "NaN"),
        (("observations", 0, "unit_net_cost_eur"), "1e2"),
        (("observations", 0, "unit_net_cost_eur"), "-1"),
        (("observations", 0, "unit_net_cost_eur"), " 1"),
        (("observations", 0, "unit_net_cost_eur"), "79228162514264337593543950336"),
        (("observations", 0, "unit_net_cost_eur"), "0.00000000000000000000000000001"),
        (("observations", 0, "evidence_receipt_id"), FAILED_RECEIPT),
        (("observations", 0, "evidence_line_ordinals"), []),
        (("observations", 0, "evidence_line_ordinals"), [0, 1]),
        (("observations", 0, "evidence_line_ordinals"), [True]),
        (("observations", 0, "reason_codes"), ["price_invalid"]),
        (("observations", 0, "document_date_utc"), "2026-09-03T15:00:00+03:00"),
        (("observations", 0, "document_date_utc"), "2026-09-07T21:00:00Z"),
        (("publications", 0, "contract_version"), "GBA-ONEC-INBOUND-V6"),
        (("publications", 0, "contract_version"), "GBA-ONEC-INBOUND-V9"),
        (("publications", 0, "contract_version"), "gba-onec-inbound-v8"),
        (("publications", 0, "contract_version"), " GBA-ONEC-INBOUND-V8"),
        (("publications", 0, "contract_version"), ["GBA-ONEC-INBOUND-V8"]),
        (("publications", 0, "contract_version"), None),
        (("publications", 0, "for_amg"), 0),
        (("publications", 0, "validated_at_utc"), "2026-09-08T02:10:01.0000003Z"),
        (("observation_started_at_utc",), "2026-09-08T02:10:01.0000003Z"),
    ],
)
def test_contract_contradictions_reject_entire_response_without_dropping_rows(path, value):
    data = payload()
    target = data
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value
    with pytest.raises(NativePurchaseCostError, match="^invalid_purchase_cost_response$"):
        parse(data)


@pytest.mark.parametrize(
    "change", ["extra", "missing", "duplicate_observation", "duplicate_evidence", "world"]
)
def test_shape_and_identity_collisions_are_rejected(change):
    data = payload()
    if change == "extra":
        data["secret_extra"] = "do not log"
    elif change == "missing":
        del data["observations"][0]["supplier_client_agreement_id"]
    elif change == "duplicate_observation":
        data["observations"].append(row())
    elif change == "duplicate_evidence":
        duplicate = row(2)
        duplicate["evidence_line_ordinals"] = [0]
        data["observations"].append(duplicate)
    else:
        duplicate = copy.deepcopy(data["publications"][0])
        duplicate["receipt_id"] = FAILED_RECEIPT
        data["publications"].append(duplicate)
    with pytest.raises(NativePurchaseCostError):
        parse(data)


@pytest.mark.parametrize("change", ["money", "ordinal", "date", "no_reason", "unknown_reason"])
def test_unknown_rows_cannot_smuggle_price_proof_or_false_zero(change):
    unknown = row(cost=None)
    if change == "money":
        unknown["unit_net_cost_eur"] = "0"
    elif change == "ordinal":
        unknown["evidence_line_ordinals"] = [1]
    elif change == "date":
        unknown["document_date_utc"] = "2026-09-03T12:00:00Z"
    else:
        unknown["reason_codes"] = [] if change == "no_reason" else ["new_unreviewed_reason"]
    with pytest.raises(NativePurchaseCostError):
        parse(payload(rows=[unknown]))


@pytest.mark.parametrize("body", [b'{"version":1,"version":1}', b'{"value":NaN}', b"\xff", b"[]"])
def test_duplicate_json_keys_invalid_constants_and_encoding_are_rejected(body):
    with pytest.raises(NativePurchaseCostError):
        parse_purchase_cost_response(body, request())


def test_calendar_bounds_keep_kyiv_dst_and_history_floor_without_utc_date_shortcut():
    req = request(as_of=date(2025, 1, 2), history=540)
    data = payload(req)
    data["observations"][0]["document_date_utc"] = "2024-12-31T22:00:00Z"
    assert parse(data, req).observations[0].unit_net_cost_eur == Decimal("10")
    data["observations"][0]["document_date_utc"] = "2024-12-31T21:59:59.9999999Z"
    with pytest.raises(NativePurchaseCostError):
        parse(data, req)
    dst = request(as_of=date(2026, 3, 30), history=1)
    start, end = dst.utc_bounds()
    assert end - start == 23 * 3600 * 10_000_000


def test_chunks_preserve_separate_snapshot_metadata_and_refuse_overlap_or_duplicate_votes():
    first = parse(payload())
    req = request((102,))
    data = payload(req, [row(2, product=102)])
    data["observation_completed_at_utc"] = "2026-09-08T02:10:02Z"
    second = parse(data, req)
    summary = aggregate_verified_costs((first, second))
    assert not summary.coherent_snapshot and len(summary.snapshots) == 2
    assert [group.product_id for group in summary.by_product] == [101, 102]
    with pytest.raises(NativePurchaseCostError):
        aggregate_verified_costs((first, first))
    data["observations"][0]["observation_id"] = 1
    with pytest.raises(NativePurchaseCostError, match="duplicate_purchase_cost_observation"):
        aggregate_verified_costs((first, parse(data, req)))
    data["observations"][0]["observation_id"] = 2
    data["observations"][0]["evidence_line_ordinals"] = [0]
    with pytest.raises(NativePurchaseCostError, match="duplicate_purchase_cost_evidence"):
        aggregate_verified_costs((first, parse(data, req)))


def test_current_typed_document_receipt_contract_is_accepted_per_world():
    data = payload()
    data["publications"][0]["contract_version"] = "GBA-ONEC-INBOUND-V8"
    data["publications"].append(
        {
            "for_amg": True,
            "receipt_id": FAILED_RECEIPT,
            "committed_at_utc": "2026-09-05T10:00:00Z",
            "contract_version": "GBA-ONEC-INBOUND-V7",
            "canonical_chain_sha256": "d" * 64,
            "validated_at_utc": "2026-09-08T02:10:01Z",
        }
    )
    snapshot = parse(data)
    assert [(item.for_amg, item.contract_version) for item in snapshot.publications] == [
        (False, "GBA-ONEC-INBOUND-V8"),
        (True, "GBA-ONEC-INBOUND-V7"),
    ]
    assert snapshot.observations[0].status == "known"
    assert snapshot.observations[0].unit_net_cost_eur == Decimal("10.0000")
    assert snapshot.observations[0].evidence_receipt_id == RECEIPT


def test_receipt_cannot_switch_contract_version_between_chunks():
    first = parse(payload())
    req = request((102,))
    data = payload(req, [row(2, product=102)])
    data["publications"][0]["contract_version"] = "GBA-ONEC-INBOUND-V8"
    with pytest.raises(NativePurchaseCostError, match="purchase_cost_publication_changed"):
        aggregate_verified_costs((first, parse(data, req)))


def test_same_immutable_receipt_cannot_change_its_proof_between_chunks():
    first = parse(payload())
    req = request((102,))
    data = payload(req, [row(2, product=102)])
    data["publications"][0]["canonical_chain_sha256"] = "f" * 64
    with pytest.raises(NativePurchaseCostError, match="purchase_cost_publication_changed"):
        aggregate_verified_costs((first, parse(data, req)))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"products": ()},
        {"products": (101, 101)},
        {"products": (True,)},
        {"products": tuple(range(1, 1002))},
        {"products": (2**63,)},
        {"supplier": 0},
        {"history": 541},
        {"history": True},
        {"as_of": date(2025, 1, 1)},
    ],
)
def test_request_bounds_fail_before_http(kwargs):
    with pytest.raises(NativePurchaseCostError, match="invalid_purchase_cost_request"):
        request(**kwargs)


class Response(io.BytesIO):
    def __init__(self, body, *, status=200, length=None, content_type="application/json"):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if length is not None:
            self.headers["Content-Length"] = str(length)


class Opener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def open(self, message, timeout):
        self.requests.append((message, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_client_posts_exact_dedicated_header_and_body_without_logging_or_live_io(capsys):
    body = json.dumps(payload()).encode()
    opener = Opener(Response(body, length=len(body)))
    client = NativePurchaseCostClient("http://analytics.internal:80", KEY, _opener=opener)
    result = client.read(request())
    message, timeout = opener.requests[0]
    assert message.method == "POST" and message.full_url.endswith(costs._PATH)
    assert message.get_header("X-procure-cost-key") == KEY
    assert message.get_header("X-internal-api-key") is None
    assert json.loads(message.data) == request().payload() and timeout == 150
    assert result.observations[0].unit_net_cost_eur == Decimal("10")
    assert KEY not in repr(client)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("failure", ["status", "type", "length", "oversize_header", "oversize_body", "http"])
def test_transport_failures_never_fallback_and_never_expose_response_or_key(failure, monkeypatch):
    body = json.dumps(payload()).encode()
    response = Response(body)
    if failure == "status":
        response.status = 503
    elif failure == "type":
        response.headers.replace_header("Content-Type", "text/html")
    elif failure == "length":
        response.headers["Content-Length"] = str(len(body) + 1)
    elif failure == "oversize_header":
        response.headers["Content-Length"] = str(costs._MAX_BYTES + 1)
    elif failure == "oversize_body":
        monkeypatch.setattr(costs, "_MAX_BYTES", len(body) - 1)
    else:
        response = HTTPError("http://private/" + KEY, 500, "private payload " + KEY, {}, None)
    opener = Opener(response)
    with pytest.raises(NativePurchaseCostError) as error:
        NativePurchaseCostClient("http://analytics.internal", KEY, _opener=opener).read(request())
    assert KEY not in str(error.value) and "private" not in str(error.value)
    assert len(opener.requests) == 1


def test_redirect_handler_refuses_before_replaying_key_and_environment_proxies_are_disabled():
    client = NativePurchaseCostClient("http://analytics.internal", KEY)
    redirects = [handler for handler in client._opener.handlers if isinstance(handler, costs._NoRedirects)]
    assert len(redirects) == 1
    with pytest.raises(NativePurchaseCostError, match="redirect_rejected"):
        redirects[0].redirect_request(None, None, 302, None, None, "https://foreign.invalid")
    assert not any(
        isinstance(handler, costs.ProxyHandler) and handler.proxies for handler in client._opener.handlers
    )


def test_elapsed_deadline_refuses_late_body(monkeypatch):
    moments = iter([0, 0, 151])
    monkeypatch.setattr(costs.time, "monotonic", lambda: next(moments))
    opener = Opener(Response(json.dumps(payload()).encode()))
    with pytest.raises(NativePurchaseCostError, match="purchase_cost_timeout"):
        NativePurchaseCostClient("http://analytics.internal", KEY, _opener=opener).read(request())


def test_dedicated_config_does_not_inherit_inbound_key_or_expose_secret(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "wrong-inbound-key")
    monkeypatch.setenv("NATIVE_PURCHASE_COST_BASE_URL", "http://analytics.internal")
    monkeypatch.setenv("NATIVE_PURCHASE_COST_API_KEY", KEY)
    settings = Settings(_env_file=None)
    client = NativePurchaseCostClient.from_settings(settings)
    assert client._key == KEY and client._key != settings.internal_api_key
    assert KEY not in repr(settings) and KEY not in settings.model_dump_json()
    empty = Settings(_env_file=None, native_purchase_cost_api_key="")
    with pytest.raises(NativePurchaseCostError, match="purchase_cost_not_configured"):
        NativePurchaseCostClient.from_settings(empty)


@pytest.mark.parametrize(
    "base,key,timeout",
    [
        ("", KEY, 150),
        ("http://user:pass@analytics.internal", KEY, 150),
        ("http://analytics.internal?secret=yes", KEY, 150),
        ("file:///private", KEY, 150),
        ("http://analytics.internal", "", 150),
        ("http://analytics.internal", "x\r\ny", 150),
        ("http://analytics.internal", KEY, 120),
        ("http://analytics.internal", KEY, float("inf")),
    ],
)
def test_invalid_client_configuration_fails_closed(base, key, timeout):
    with pytest.raises(NativePurchaseCostError, match="purchase_cost_not_configured"):
        NativePurchaseCostClient(base, key, timeout)


@pytest.mark.parametrize("length,accepted", [(31, False), (32, True), (128, True), (129, False)])
def test_key_length_matches_dedicated_server_auth_before_any_request(length, accepted):
    secret = "x" * length
    if accepted:
        settings = Settings(_env_file=None, native_purchase_cost_api_key=secret)
        assert settings.native_purchase_cost_api_key.get_secret_value() == secret
        NativePurchaseCostClient("http://analytics.internal", secret)
    else:
        with pytest.raises(ValueError) as error:
            Settings(_env_file=None, native_purchase_cost_api_key=secret)
        assert secret not in str(error.value)
        with pytest.raises(NativePurchaseCostError, match="purchase_cost_not_configured"):
            NativePurchaseCostClient("http://analytics.internal", secret)
