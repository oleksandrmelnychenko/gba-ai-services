"""Per-product sales and return facts precomputed once per cache generation.

The whole catalog is aggregated in one pass per artefact and stored as a Redis hash with one
field per product, so the product page reads a single small field instead of re-running the
return valuation. Rows are stored exactly as SQL returned them (Decimals as text); validation
and money rounding stay in the serving path, which keeps a per-request result identical to the
live per-product query.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core import exact_numbers as exact
from app.core import memory
from app.core.config import get_settings
from app.data import cache
from app.data import signals_repository as sig

FORMAT_VERSION = 1
META_FIELD = "__meta__"
MONTHLY_MONTHS = 24
_KEY_COLUMN = "product_id"


def monthly_key(as_of: str) -> str:
    return cache.make_key("product-facts", f"monthly:{MONTHLY_MONTHS}", as_of)


def regional_key(as_of: str, window_days: int) -> str:
    return cache.make_key("product-facts", f"regional:{int(window_days)}", as_of)


def encode(rows: list[dict[str, Any]], as_of: str) -> dict[str, str]:
    """Group raw aggregate rows by product into hash fields plus one metadata field."""
    columns: list[str] = []
    decimals: set[str] = set()
    grouped: dict[int, list[list[Any]]] = {}
    if rows:
        columns = [column for column in rows[0] if column != _KEY_COLUMN]
    for row in rows:
        if [column for column in row if column != _KEY_COLUMN] != columns:
            raise ValueError("product facts rows do not share one column layout")
        values: list[Any] = []
        for column in columns:
            value = row[column]
            if isinstance(value, Decimal):
                decimals.add(column)
                value = format(value, "f")
            elif isinstance(value, bool) or not (value is None or isinstance(value, (int, str))):
                raise ValueError(f"product facts column {column} has unsupported type {type(value).__name__}")
            elif column in decimals:
                raise ValueError(f"product facts column {column} mixes numeric types")
            values.append(value)
        grouped.setdefault(exact.positive_int(row.get(_KEY_COLUMN), "product facts product_id"), []).append(values)
    for column in decimals:
        position = columns.index(column)
        for product_rows in grouped.values():
            for values in product_rows:
                if values[position] is not None and not isinstance(values[position], str):
                    raise ValueError(f"product facts column {column} mixes numeric types")
    mapping = {str(pid): json.dumps(product_rows, separators=(",", ":")) for pid, product_rows in grouped.items()}
    mapping[META_FIELD] = json.dumps({
        "format": FORMAT_VERSION,
        "model_version": get_settings().model_version,
        "as_of": as_of,
        "columns": columns,
        "decimals": sorted(decimals),
        "product_count": len(grouped),
        "row_count": len(rows),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return mapping


def decode(meta_raw: str | None, rows_raw: str | None, product_id: int, as_of: str) -> list[dict[str, Any]] | None:
    """Rows of one product, or None when the artefact is missing or not usable."""
    if meta_raw is None:
        return None
    meta = json.loads(meta_raw)
    if (meta.get("format") != FORMAT_VERSION or meta.get("model_version") != get_settings().model_version
            or meta.get("as_of") != as_of):
        return None
    if rows_raw is None:
        return []
    columns = meta["columns"]
    decimals = set(meta["decimals"])
    rows: list[dict[str, Any]] = []
    for values in json.loads(rows_raw):
        if len(values) != len(columns):
            raise ValueError("product facts row does not match its column layout")
        row: dict[str, Any] = {_KEY_COLUMN: product_id}
        for column, value in zip(columns, values, strict=True):
            row[column] = Decimal(value) if column in decimals and value is not None else value
        rows.append(row)
    return rows


def read(key: str, product_id: int, as_of: str) -> list[dict[str, Any]] | None:
    values = cache.hash_fields(key, [META_FIELD, str(int(product_id))])
    if values is None:
        return None
    return decode(values[0], values[1], int(product_id), as_of)


def available(key: str, as_of: str) -> bool:
    values = cache.hash_fields(key, [META_FIELD, META_FIELD])
    return values is not None and decode(values[0], None, 0, as_of) is not None


def age_seconds(key: str, as_of: str) -> float | None:
    """Seconds since the artefact was built, or None when it is missing or not usable."""
    values = cache.hash_fields(key, [META_FIELD])
    if values is None or values[0] is None or decode(values[0], None, 0, as_of) is None:
        return None
    built_at = datetime.fromisoformat(json.loads(values[0])["built_at"])
    return max(0.0, (datetime.now(timezone.utc) - built_at).total_seconds())


def due(as_of: str, window_days: int, max_age_seconds: float) -> bool:
    for key in (monthly_key(as_of), regional_key(as_of, window_days)):
        age = age_seconds(key, as_of)
        if age is None or age >= max_age_seconds:
            return True
    return False


def _publish(key: str, rows: list[dict[str, Any]], as_of: str) -> dict[str, Any]:
    mapping = encode(rows, as_of)
    stored = cache.replace_hash(key, mapping)
    if stored and not available(key, as_of):
        raise RuntimeError(f"product facts readback failed for {key}")
    result = {"key": key, "stored": stored, "products": len(mapping) - 1, "rows": len(rows)}
    del mapping, rows
    memory.release_free_memory()
    return result


def build_monthly(as_of: str) -> dict[str, Any]:
    return _publish(monthly_key(as_of), sig.monthly_sales_all_products(as_of, MONTHLY_MONTHS), as_of)


def build_regional(as_of: str, window_days: int) -> dict[str, Any]:
    return _publish(regional_key(as_of, window_days), sig.regional_sales_all_products(as_of, window_days), as_of)
