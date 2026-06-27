"""Read-only catalog / interchangeability queries over ConcordDb_V5. All parameterized.

INTERCHANGEABILITY SIGNALS (discovered live on ConcordDb_V5 — see substitution.py header for numbers):
  - CANONICAL: dbo.ProductAnalogue (curated cross-reference). 1.82M live (Deleted=0) pairs across
    191,096 base products; ~99.6% bidirectional, so we read BOTH directions (Base->Analogue and
    Analogue->Base) and de-dupe. dbo.Product.HasAnalogue=1 exactly mirrors "has live ProductAnalogue rows".
  - FALLBACK: dbo.Product.MainOriginalNumber (OE number). Only ~27% of analogue pairs share an OE,
    so OE catches SAME-OE products not yet linked in ProductAnalogue. Junk OE values are rampant
    ('', '-', '*', '0', '#', '--', '0*') — guarded by a LEN >= 3 check plus the _OE_JUNK NOT-IN list.
  - REJECTED: dbo.ProductProductGroup (avg 3,700 products/group, max 35,766 — a coarse category, not a
    substitute set) and dbo.ProductCarBrand (0 live rows — entirely Deleted=1, unusable).

All queries respect Deleted=0. `source` on each candidate marks which signal surfaced it.
"""
from __future__ import annotations

from typing import Any

from app.data.db import in_clause, query

# OE values that are present but meaningless — never group products by these. The LEN >= 3 guard in
# analogues_for already drops 1-2 char junk ('', '-', '*', '0', '#', '--', '0*'); this list covers the
# 3+ char junk that survives it.
_OE_JUNK = ("0**", "***", "n/a", "na", "---", "...", "xxx", "000")


def product_card(product_id: int) -> dict[str, Any] | None:
    """Target product's catalog identity (or None if missing / deleted)."""
    rows = query(
        """
        SELECT ID AS product_id, Name AS name, VendorCode AS vendor_code,
               MainOriginalNumber AS oe_number, HasAnalogue AS has_analogue,
               IsForSale AS is_for_sale
        FROM dbo.Product
        WHERE Deleted = 0 AND ID = :pid
        """,
        {"pid": int(product_id)},
    )
    return rows[0] if rows else None


def analogues_for(product_id: int) -> list[dict[str, Any]]:
    """Interchangeable, non-deleted candidates for a product, with catalog meta + an OE-fallback union.

    Returns one row per distinct candidate product (excluding the target). Columns:
      product_id, name, vendor_code, oe_number, is_for_sale, source
    `source` is 'analogue' for curated ProductAnalogue links and 'oe' for same-OE-number fallbacks
    (analogue wins when a product is reachable by both — the de-dupe keeps the curated row).
    The candidate set is small (largest observed live = 149), so a single set-based query is fine;
    sellability/health ranking is applied downstream in substitution.py.
    """
    pid = int(product_id)
    junk_ph, junk_params = in_clause("junk", _OE_JUNK)
    params: dict[str, Any] = {"pid": pid, **junk_params}
    return query(
        f"""
        WITH analogue_ids AS (
            SELECT a.AnalogueProductID AS pid
            FROM dbo.ProductAnalogue a
            WHERE a.Deleted = 0 AND a.BaseProductID = :pid
            UNION
            SELECT a.BaseProductID AS pid
            FROM dbo.ProductAnalogue a
            WHERE a.Deleted = 0 AND a.AnalogueProductID = :pid
        ),
        oe_ids AS (
            SELECT p2.ID AS pid
            FROM dbo.Product p0
            JOIN dbo.Product p2
              ON p2.Deleted = 0
             AND p2.ID <> p0.ID
             AND LTRIM(RTRIM(p2.MainOriginalNumber)) = LTRIM(RTRIM(p0.MainOriginalNumber))
            WHERE p0.ID = :pid AND p0.Deleted = 0
              AND p0.MainOriginalNumber IS NOT NULL
              AND LEN(LTRIM(RTRIM(p0.MainOriginalNumber))) >= 3
              AND LTRIM(RTRIM(LOWER(p0.MainOriginalNumber))) NOT IN {junk_ph}
        ),
        candidates AS (
            SELECT pid, CASE WHEN src_analogue = 1 THEN 'analogue' ELSE 'oe' END AS source
            FROM (
                SELECT pid, MAX(src_analogue) AS src_analogue
                FROM (
                    SELECT pid, 1 AS src_analogue FROM analogue_ids
                    UNION ALL
                    SELECT pid, 0 AS src_analogue FROM oe_ids
                ) u
                WHERE pid <> :pid
                GROUP BY pid
            ) ranked
        )
        SELECT p.ID AS product_id, p.Name AS name, p.VendorCode AS vendor_code,
               p.MainOriginalNumber AS oe_number, p.IsForSale AS is_for_sale,
               c.source AS source
        FROM candidates c
        JOIN dbo.Product p ON p.ID = c.pid AND p.Deleted = 0
        """,
        params,
    )
