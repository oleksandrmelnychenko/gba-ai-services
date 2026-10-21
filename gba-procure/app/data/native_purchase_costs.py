"""Strict read-only purchase-cost transport and exact, observation-weighted history.

This module is deliberately not wired into policy/cost_repository. Each response
is one native snapshot. Separate responses (including chunks) are NOT a joint
snapshot. Confirmed supplier history is not a customer-contract price quote.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from http.client import HTTPException
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.core.config import Settings

_PATH = "/internal/procure/purchase-cost-observations/v1"
_SCOPE = "CurrentPostedReceipts"
_RESOLVER = "current-posted-receipt-cost-v1"
_CONTRACT_VERSIONS = frozenset({"GBA-ONEC-INBOUND-V7", "GBA-ONEC-INBOUND-V8"})
_MAX_ROWS = 50_000
_MAX_BYTES = 64 * 1024 * 1024
_MAX_I64 = 2**63 - 1
_MAX_DECIMAL_COEFFICIENT = 2**96 - 1
_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")
_MONEY = re.compile(r"(?:0|[1-9][0-9]{0,28})(?:\.[0-9]{1,28})?\Z")
_UTC = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,7}))?(?:Z|\+00:00)\Z"
)
_REASONS = frozenset(
    {
        "native_provenance_unavailable",
        "receipt_missing",
        "receipt_invalid",
        "receipt_contract_unsupported",
        "receipt_chain_invalid",
        "source_identity_unresolved",
        "source_route_unsupported",
        "financial_multiset_mismatch",
        "price_invalid",
        "arithmetic_precision_unsupported",
    }
)


class NativePurchaseCostError(RuntimeError):
    """Sanitized error code only: never include URL, key or response payload."""


def _require(condition: bool, code: str = "invalid_purchase_cost_response") -> None:
    if not condition:
        raise NativePurchaseCostError(code)


def _identity(value: Any, *, nullable: bool = False, zero: bool = False) -> int | None:
    if nullable and value is None:
        return None
    _require(type(value) is int and (0 if zero else 1) <= value <= _MAX_I64)
    return value


def _object(value: Any, keys: set[str]) -> dict:
    _require(type(value) is dict and set(value) == keys)
    return value


def _array(value: Any, limit: int) -> list:
    _require(type(value) is list and len(value) <= limit)
    return value


def _sha(value: Any) -> str:
    _require(type(value) is str and _HEX.fullmatch(value) is not None)
    return value.lower()


def _uuid(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    _require(type(value) is str and len(value) == 36)
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise NativePurchaseCostError("invalid_purchase_cost_response") from None
    _require(parsed.int != 0 and str(parsed) == value.lower())
    return str(parsed)


def _utc_ticks(value: Any) -> int:
    """Compare UTC at native 100ns resolution; datetime alone truncates digit7."""
    _require(type(value) is str)
    match = _UTC.fullmatch(value)
    _require(match is not None)
    try:
        moment = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise NativePurchaseCostError("invalid_purchase_cost_response") from None
    seconds = moment.toordinal() * 86400 + moment.hour * 3600 + moment.minute * 60 + moment.second
    return seconds * 10_000_000 + int((match[2] or "").ljust(7, "0"))


def _money(value: Any) -> Decimal:
    # Numeric JSON money, exponents, signs, whitespace, infinity and NaN are forbidden.
    _require(type(value) is str and len(value) <= 59 and _MONEY.fullmatch(value) is not None)
    parsed = Decimal(value)
    coefficient = int("".join(str(digit) for digit in parsed.as_tuple().digits))
    _require(coefficient <= _MAX_DECIMAL_COEFFICIENT)
    return parsed


@dataclass(frozen=True)
class PurchaseCostRequest:
    product_ids: tuple[int, ...]
    supplier_id: int | None
    as_of_exclusive: date
    history_days: int = 540

    def __post_init__(self) -> None:
        _require(
            type(self.product_ids) is tuple and 1 <= len(self.product_ids) <= 1000,
            "invalid_purchase_cost_request",
        )
        _require(
            all(type(item) is int and 0 < item <= _MAX_I64 for item in self.product_ids)
            and len(set(self.product_ids)) == len(self.product_ids),
            "invalid_purchase_cost_request",
        )
        _require(
            self.supplier_id is None or type(self.supplier_id) is int and 0 < self.supplier_id <= _MAX_I64,
            "invalid_purchase_cost_request",
        )
        _require(
            type(self.as_of_exclusive) is date
            and date(2025, 1, 1) < self.as_of_exclusive
            and self.as_of_exclusive.year <= 9998,
            "invalid_purchase_cost_request",
        )
        _require(
            type(self.history_days) is int and 1 <= self.history_days <= 540, "invalid_purchase_cost_request"
        )

    def payload(self) -> dict:
        return {
            "version": 1,
            "product_ids": sorted(self.product_ids),
            "supplier_id": self.supplier_id,
            "as_of_exclusive": self.as_of_exclusive.isoformat(),
            "history_days": self.history_days,
        }

    def utc_bounds(self) -> tuple[int, int]:
        start = max(date(2025, 1, 1), self.as_of_exclusive - timedelta(days=self.history_days))
        kyiv = ZoneInfo("Europe/Kyiv")

        def ticks(day: date) -> int:
            moment = datetime.combine(day, datetime.min.time(), kyiv).astimezone(UTC)
            return _utc_ticks(moment.isoformat())

        return ticks(start), ticks(self.as_of_exclusive)


@dataclass(frozen=True)
class PurchaseCostPublication:
    for_amg: bool
    receipt_id: str
    committed_at_utc: str
    contract_version: str
    canonical_chain_sha256: str
    validated_at_utc: str


@dataclass(frozen=True)
class PurchaseCostObservation:
    observation_id: int
    product_id: int
    supplier_id: int | None
    supplier_client_agreement_id: int | None
    document_id: int
    document_date_utc: str | None
    unit_id: int | None
    unit_net_cost_eur: Decimal | None
    status: str
    reason_codes: tuple[str, ...]
    evidence_receipt_id: str | None
    evidence_line_ordinals: tuple[int, ...]
    financial_fingerprint: str


@dataclass(frozen=True)
class PurchaseCostSnapshot:
    request: PurchaseCostRequest
    observation_started_at_utc: str
    observation_completed_at_utc: str
    fingerprint: str
    publications: tuple[PurchaseCostPublication, ...]
    observations: tuple[PurchaseCostObservation, ...]
    scope: str = _SCOPE
    resolver_version: str = _RESOLVER
    currency: str = "EUR"


_ROOT_KEYS = {
    "version",
    "scope",
    "resolver_version",
    "currency",
    "product_ids",
    "supplier_id",
    "as_of_exclusive",
    "history_days",
    "observation_started_at_utc",
    "observation_completed_at_utc",
    "fingerprint",
    "publications",
    "observations",
}
_PUBLICATION_KEYS = set(PurchaseCostPublication.__dataclass_fields__)
_OBSERVATION_KEYS = set(PurchaseCostObservation.__dataclass_fields__)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise NativePurchaseCostError("invalid_purchase_cost_response")


def parse_purchase_cost_response(payload: bytes, request: PurchaseCostRequest) -> PurchaseCostSnapshot:
    """Reject the whole response on shape/proof contradictions; no row is silently dropped."""
    _require(type(payload) is bytes and 0 < len(payload) <= _MAX_BYTES)
    try:
        root = _object(
            json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_constant,
            ),
            _ROOT_KEYS,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise NativePurchaseCostError("invalid_purchase_cost_response") from None
    expected = request.payload()
    _require(
        type(root["version"]) is int
        and root["version"] == 1
        and root["scope"] == _SCOPE
        and root["resolver_version"] == _RESOLVER
        and root["currency"] == "EUR"
    )
    products = _array(root["product_ids"], 1000)
    _require(
        all(type(item) is int and 0 < item <= _MAX_I64 for item in products)
        and sorted(products) == expected["product_ids"]
    )
    _identity(root["supplier_id"], nullable=True)
    _require(
        root["supplier_id"] == request.supplier_id
        and type(root["history_days"]) is int
        and root["history_days"] == request.history_days
        and root["as_of_exclusive"] == expected["as_of_exclusive"]
    )
    started = _utc_ticks(root["observation_started_at_utc"])
    completed = _utc_ticks(root["observation_completed_at_utc"])
    _require(started <= completed)
    fingerprint = _sha(root["fingerprint"])
    publications: dict[str, PurchaseCostPublication] = {}
    worlds: set[bool] = set()
    for item in _array(root["publications"], 2):
        row = _object(item, _PUBLICATION_KEYS)
        receipt_id = _uuid(row["receipt_id"])
        _require(
            type(row["for_amg"]) is bool
            and row["for_amg"] not in worlds
            and receipt_id not in publications
            and type(row["contract_version"]) is str
            and row["contract_version"] in _CONTRACT_VERSIONS
        )
        validated = _utc_ticks(row["validated_at_utc"])
        committed = _utc_ticks(row["committed_at_utc"])
        _require(started <= validated <= completed and committed <= completed + 300 * 10_000_000)
        publication = PurchaseCostPublication(
            row["for_amg"],
            receipt_id,
            row["committed_at_utc"],
            row["contract_version"],
            _sha(row["canonical_chain_sha256"]),
            row["validated_at_utc"],
        )
        publications[receipt_id] = publication
        worlds.add(publication.for_amg)
    observations = []
    identities: set[int] = set()
    evidence_ids: set[tuple[str, int]] = set()
    from_ticks, to_ticks = request.utc_bounds()
    product_set = set(products)
    for item in _array(root["observations"], _MAX_ROWS):
        row = _object(item, _OBSERVATION_KEYS)
        identity = _identity(row["observation_id"])
        product = _identity(row["product_id"])
        supplier = _identity(row["supplier_id"], nullable=True)
        agreement = _identity(row["supplier_client_agreement_id"], nullable=True)
        document = _identity(row["document_id"])
        unit = _identity(row["unit_id"], nullable=True)
        receipt_id = _uuid(row["evidence_receipt_id"], nullable=True)
        _require(
            identity not in identities
            and product in product_set
            and (request.supplier_id is None or supplier == request.supplier_id)
        )
        identities.add(identity)
        reasons = _array(row["reason_codes"], len(_REASONS))
        _require(
            all(type(reason) is str and reason in _REASONS for reason in reasons)
            and len(set(reasons)) == len(reasons)
        )
        ordinals = tuple(_identity(value, zero=True) for value in _array(row["evidence_line_ordinals"], 1))
        known = row["status"] == "known"
        _require(type(row["status"]) is str and row["status"] in {"known", "unknown"})
        cost = None
        if known:
            _require(
                not reasons
                and supplier is not None
                and agreement is not None
                and unit is not None
                and receipt_id in publications
                and len(ordinals) == 1
            )
            _require(from_ticks <= _utc_ticks(row["document_date_utc"]) < to_ticks)
            key = (receipt_id, ordinals[0])
            _require(key not in evidence_ids)
            evidence_ids.add(key)
            cost = _money(row["unit_net_cost_eur"])
        else:
            _require(
                bool(reasons)
                and row["unit_net_cost_eur"] is None
                and not ordinals
                and row["document_date_utc"] is None
            )
        observations.append(
            PurchaseCostObservation(
                identity,
                product,
                supplier,
                agreement,
                document,
                row["document_date_utc"],
                unit,
                cost,
                row["status"],
                tuple(reasons),
                receipt_id,
                ordinals,
                _sha(row["financial_fingerprint"]),
            )
        )
    return PurchaseCostSnapshot(
        request,
        root["observation_started_at_utc"],
        root["observation_completed_at_utc"],
        fingerprint,
        tuple(publications.values()),
        tuple(observations),
    )


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NativePurchaseCostError("purchase_cost_redirect_rejected")


class NativePurchaseCostClient:
    """Sync data-layer client. No retries, cache, proxy inheritance, redirects or SQL fallback.

    Socket operations time out after timeout_seconds (> server's120s). A response
    finishing after that elapsed deadline is rejected. The deadline is checked
    between bounded reads; an in-flight socket operation can finish at its socket
    timeout. No claim of one snapshot across multiple calls is made.
    """

    def __init__(self, base_url: str, api_key: str, timeout_seconds: float = 150.0, *, _opener=None):
        _require(type(base_url) is str and type(api_key) is str, "purchase_cost_not_configured")
        try:
            url = urlsplit(base_url)
            valid_url = (
                url.scheme in {"http", "https"}
                and url.hostname
                and url.username is None
                and url.password is None
                and not url.query
                and not url.fragment
                and url.port != 0
            )
        except ValueError:
            valid_url = False
        _require(
            bool(valid_url) and not any(char.isspace() for char in base_url), "purchase_cost_not_configured"
        )
        _require(
            32 <= len(api_key) <= 128 and all(33 <= ord(char) <= 126 for char in api_key),
            "purchase_cost_not_configured",
        )
        _require(
            type(timeout_seconds) in {int, float}
            and math.isfinite(timeout_seconds)
            and 120 < timeout_seconds <= 600,
            "purchase_cost_not_configured",
        )
        self._url = base_url.rstrip("/") + _PATH
        self._key = api_key
        self._timeout = float(timeout_seconds)
        self._opener = _opener or build_opener(ProxyHandler({}), _NoRedirects())

    @classmethod
    def from_settings(cls, settings: Settings) -> NativePurchaseCostClient:
        return cls(
            settings.native_purchase_cost_base_url,
            settings.native_purchase_cost_api_key.get_secret_value(),
            settings.native_purchase_cost_timeout_seconds,
        )

    def read(self, request: PurchaseCostRequest) -> PurchaseCostSnapshot:
        body = json.dumps(request.payload(), separators=(",", ":")).encode("ascii")
        _require(len(body) <= 65536, "invalid_purchase_cost_request")
        message = Request(
            self._url,
            body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Procure-Cost-Key": self._key,
            },
        )
        started = time.monotonic()
        try:
            with self._opener.open(message, timeout=self._timeout) as response:
                _require(response.status == 200, "purchase_cost_transport_unavailable")
                _require(
                    response.headers.get_content_type() == "application/json"
                    and response.headers.get("Content-Encoding", "identity").lower() == "identity"
                )
                length = response.headers.get("Content-Length")
                if length is not None:
                    _require(
                        length.isascii()
                        and length.isdigit()
                        and len(length) <= 10
                        and int(length) <= _MAX_BYTES
                    )
                chunks = []
                size = 0
                while True:
                    _require(time.monotonic() - started <= self._timeout, "purchase_cost_timeout")
                    chunk = response.read1(min(65536, _MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    _require(size <= _MAX_BYTES, "purchase_cost_response_too_large")
                    chunks.append(chunk)
                _require(time.monotonic() - started <= self._timeout, "purchase_cost_timeout")
                if length is not None:
                    _require(size == int(length))
        except (TimeoutError, HTTPError, URLError, OSError, HTTPException, UnicodeError):
            raise NativePurchaseCostError("purchase_cost_transport_unavailable") from None
        return parse_purchase_cost_response(b"".join(chunks), request)


@dataclass(frozen=True)
class VerifiedCostAggregate:
    product_id: int
    supplier_id: int | None
    observation_count: int
    known_count: int
    unknown_count: int
    reason_counts: tuple[tuple[str, int], ...]
    unit_ids: tuple[int, ...]
    supplier_client_agreement_ids: tuple[int, ...]
    median_unit_net_cost_eur: Decimal | None
    median_float: float | None
    aggregation_reason: str | None

    @property
    def coverage(self) -> str:
        if self.observation_count == 0:
            return "no_observations"
        return "complete" if self.unknown_count == 0 else "partial" if self.known_count else "unknown"


@dataclass(frozen=True)
class VerifiedCostSummary:
    by_product: tuple[VerifiedCostAggregate, ...]
    by_product_supplier: tuple[VerifiedCostAggregate, ...]
    snapshots: tuple[PurchaseCostSnapshot, ...] = field(repr=False)

    @property
    def coherent_snapshot(self) -> bool:
        return len(self.snapshots) == 1


def _rounded_median(values: list[Decimal]) -> tuple[Decimal, float | None]:
    """Exact even/odd median and four-place HALF_UP without Decimal context rounding."""
    ordered = sorted(Fraction(value) for value in values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    scaled = median * 10000
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    rounded = whole + (2 * remainder >= scaled.denominator)
    exact = Decimal(f"{rounded // 10000}.{rounded % 10000:04d}")
    value = float(exact)
    # Preserve the canonical four-place value through Python JSON's shortest float spelling.
    return exact, value if math.isfinite(value) and Decimal(str(value)) == exact else None


def _aggregate(
    product: int, supplier: int | None, rows: list[PurchaseCostObservation]
) -> VerifiedCostAggregate:
    known = [row for row in rows if row.status == "known"]
    units = tuple(sorted({row.unit_id for row in known}))
    agreements = tuple(sorted({row.supplier_client_agreement_id for row in known}))
    reasons = Counter(reason for row in rows for reason in row.reason_codes)
    median = output = None
    reason = None
    if known:
        if len(units) != 1:
            reason = "mixed_current_units"
        else:
            median, output = _rounded_median([row.unit_net_cost_eur for row in known])
            if output is None:
                reason = "cost_float_precision_unsupported"
    return VerifiedCostAggregate(
        product,
        supplier,
        len(rows),
        len(known),
        len(rows) - len(known),
        tuple(sorted(reasons.items())),
        units,
        agreements,
        median,
        output,
        reason,
    )


def aggregate_verified_costs(snapshots: tuple[PurchaseCostSnapshot, ...]) -> VerifiedCostSummary:
    """Aggregate only validated snapshots with disjoint product requests.

    One PII observation is one vote, including factual zero; no quantity weighting.
    Partial known history is retained with explicit counts. There is no supplier
    ranking or claim that a historical median is currently offered by a contract.
    """
    _require(type(snapshots) is tuple and 1 <= len(snapshots) <= 100, "invalid_purchase_cost_aggregation")
    first = snapshots[0].request
    products: set[int] = set()
    identities: set[int] = set()
    evidence: set[tuple[str, int]] = set()
    publication_proofs: dict[str, tuple] = {}
    by_product: dict[int, list[PurchaseCostObservation]] = defaultdict(list)
    by_pair: dict[tuple[int, int | None], list[PurchaseCostObservation]] = defaultdict(list)
    total = 0
    for snapshot in snapshots:
        request = snapshot.request
        _require(
            snapshot.scope == _SCOPE
            and snapshot.resolver_version == _RESOLVER
            and snapshot.currency == "EUR"
            and request.supplier_id == first.supplier_id
            and request.as_of_exclusive == first.as_of_exclusive
            and request.history_days == first.history_days
            and not products.intersection(request.product_ids),
            "invalid_purchase_cost_aggregation",
        )
        products.update(request.product_ids)
        for publication in snapshot.publications:
            proof = (
                publication.for_amg,
                publication.contract_version,
                publication.canonical_chain_sha256,
                _utc_ticks(publication.committed_at_utc),
            )
            _require(
                publication.receipt_id not in publication_proofs
                or publication_proofs[publication.receipt_id] == proof,
                "purchase_cost_publication_changed",
            )
            publication_proofs[publication.receipt_id] = proof
        total += len(snapshot.observations)
        _require(total <= _MAX_ROWS, "purchase_cost_aggregation_too_large")
        for row in snapshot.observations:
            _require(row.observation_id not in identities, "duplicate_purchase_cost_observation")
            identities.add(row.observation_id)
            for ordinal in row.evidence_line_ordinals:
                key = (row.evidence_receipt_id, ordinal)
                _require(key not in evidence, "duplicate_purchase_cost_evidence")
                evidence.add(key)
            by_product[row.product_id].append(row)
            by_pair[row.product_id, row.supplier_id].append(row)
    # Empty requested products remain represented; absence of observations is not a zero cost.
    if first.supplier_id is not None:
        for product in products:
            by_pair.setdefault((product, first.supplier_id), [])
    return VerifiedCostSummary(
        tuple(_aggregate(product, first.supplier_id, by_product[product]) for product in sorted(products)),
        tuple(
            _aggregate(product, supplier, rows)
            for (product, supplier), rows in sorted(
                by_pair.items(), key=lambda pair: (pair[0][0], pair[0][1] or 0)
            )
        ),
        snapshots,
    )
