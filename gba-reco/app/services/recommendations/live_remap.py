"""Remap recommendation product ids to their live catalog rows.

Catalog re-syncs mint NEW Product rows and soft-delete the old generations, while
order history keeps pointing at the old ids (on dev the entire 1.9M-line history
references deleted product rows). A recommendation carrying a dead id is useless:
the gba-server hydration filters Deleted=0, so the client sees an empty list.

The natural key across generations is VendorCode — map every recommended id to the
newest live row with the same VendorCode (identity for ids that are already live),
drop the ones with no live counterpart, and dedupe generations that collapse onto
the same live row (first/highest-ranked wins).
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.data.db import in_clause, query
from app.domain.models import ProductRec

log = get_logger("live_remap")


def live_product_map(product_ids: list[int]) -> dict[int, int]:
    """old_id -> live_id via the VendorCode natural key. Already-live ids map to
    themselves; ids whose VendorCode has no live row are absent from the result."""
    if not product_ids:
        return {}
    ph, params = in_clause("m", list(dict.fromkeys(product_ids)))
    rows = query(
        f"""
        SELECT d.ID AS old_id,
               CASE WHEN d.Deleted = 0 THEN d.ID ELSE MAX(l.ID) END AS live_id
        FROM dbo.Product d
        JOIN dbo.Product l ON l.VendorCode = d.VendorCode AND l.Deleted = 0
        WHERE d.ID IN {ph} AND d.VendorCode IS NOT NULL AND d.VendorCode <> ''
        GROUP BY d.ID, d.Deleted
        """,
        params,
    )
    return {int(r["old_id"]): int(r["live_id"]) for r in rows}


def remap_recs_to_live(recs: list[ProductRec]) -> list[ProductRec]:
    """Translate a ranked rec list onto live product ids, dropping unmappable ids and
    deduping generations that collapse onto the same live row. Ranks are reassigned."""
    if not recs:
        return recs
    mapping = live_product_map([r.product_id for r in recs])
    seen: set[int] = set()
    kept: list[ProductRec] = []
    for rec in recs:
        live_id = mapping.get(rec.product_id)
        if live_id is None or live_id in seen:
            continue
        seen.add(live_id)
        rec.product_id = live_id
        rec.rank = len(kept) + 1
        kept.append(rec)
    if len(kept) < len(recs):
        log.info("recs_remapped_to_live", requested=len(recs), kept=len(kept))
    return kept
