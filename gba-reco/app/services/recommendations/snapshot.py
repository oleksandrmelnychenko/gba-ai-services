"""Set-based refresh engine: one in-memory snapshot of the sales spine serves every context."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import numpy as np

from app.core.config import get_settings
from app.core.history import require_supported_as_of, source_history_start_iso
from app.core.logging import get_logger
from app.data import cache
from app.data import sales_repository as repo
from app.data import source_rules as rules
from app.data.db import query
from app.domain.models import (
    ProductRec,
    RecommendationResult,
    RecSource,
    RecSourceDetail,
    Segment,
)
from app.services.eval import measured
from app.services.recommendations.copurchase import _COOC_ROW_CAP
from app.services.recommendations.recommender import (
    _MAX_SIMILAR,
    _MIN_SIMILARITY,
    _RECENCY_HALFLIFE_DAYS,
    _STOCK_POOL_CAP,
    _WEIGHTS,
)

log = get_logger("snapshot")

_COPURCHASE_MODEL_VERSION = "copurchase-v5-confirmed-sales-history-floor-20250101"
_SEED_LIMIT = 50
_TARGET_ROW_LIMIT = 500
_CANDIDATE_LIMIT = 400
_SYNTHETIC_NAME = "Ввід боргів"


@dataclass(frozen=True)
class Params:
    repurchase_count: int
    max_per_group: int
    halflife_days: float = _RECENCY_HALFLIFE_DAYS
    discovery_source: str = "similar_clients"
    popular_scope: str = "global"
    seasonal_boost: float = 0.0


def default_params() -> Params:
    settings = get_settings()
    return Params(
        repurchase_count=settings.repurchase_count,
        max_per_group=settings.max_per_group,
        discovery_source=settings.discovery_source,
    )


@dataclass
class SnapshotData:
    as_of: str
    floor: str
    rows: list[tuple]
    synthetic: frozenset[int]
    product_rows: list[tuple]
    availability: list[tuple]
    storages: list[dict]
    agreements: list[tuple]
    groups: list[tuple]
    negatives: dict[int, frozenset[int]] = field(default_factory=dict)
    siblings: dict[int, int] = field(default_factory=dict)
    services: frozenset[int] = frozenset()
    open_lines: list[tuple] = field(default_factory=list)


def _load_negatives() -> dict[int, frozenset[int]]:
    uids = cache.negative_client_uids()
    if not uids:
        return {}
    rows = query(
        """
        SELECT ID AS cid, LOWER(CONVERT(varchar(36), NetUID)) AS uid
        FROM dbo.Client
        WHERE NetUID IS NOT NULL
        """
    )
    out: dict[int, frozenset[int]] = {}
    for row in rows:
        if row["uid"] in uids:
            negatives = cache.get_negatives(int(row["cid"]))
            if negatives:
                out[int(row["cid"])] = negatives
    return out


def load_data(as_of: str) -> SnapshotData:
    require_supported_as_of(as_of)
    floor = source_history_start_iso()
    history_cte = f"""
        WITH H AS (
            SELECT DISTINCT oi.ProductID AS pid
            FROM {rules.SPINE}
            WHERE {rules.CONFIRMED} AND {rules.SALE_INSTANT} >= :sold_from
        )
    """
    catalog_window = {"sold_from": rules.window(as_of)["sold_from"]}
    rows = query(
        f"""
        SELECT ca.ClientID AS cid, LOWER(CONVERT(varchar(36), ca.NetUID)) AS agr,
               o.ID AS oid, {rules.SOLD_AT} AS created, oi.ProductID AS pid
        FROM {rules.SPINE}
        WHERE {rules.PURCHASED}
        """,
        rules.window(as_of),
    )
    synthetic = get_settings().synthetic_product_ids or frozenset(
        int(r["pid"])
        for r in query(
            "SELECT TOP 1 ID AS pid FROM dbo.Product WHERE Name = :name AND Deleted = 0 ORDER BY ID DESC",
            {"name": _SYNTHETIC_NAME},
        )
    )
    product_rows = query(
        history_cte
        + """
        SELECT d.ID AS old_id, d.Deleted AS deleted,
               CASE WHEN d.VendorCode IS NOT NULL AND d.VendorCode <> '' THEN 1 ELSE 0 END AS has_code,
               (SELECT MAX(l.ID) FROM dbo.Product l
                WHERE l.VendorCode = d.VendorCode AND l.Deleted = 0) AS max_live
        FROM H JOIN dbo.Product d ON d.ID = H.pid
        """,
        catalog_window,
    )
    availability = query(
        "SELECT ProductID AS pid, StorageID AS sid, Amount AS amt "
        "FROM dbo.ProductAvailability WHERE Deleted = 0"
    )
    storages = query(
        "SELECT ID AS id, Deleted AS deleted, ForDefective AS defective, LOWER(Locale) AS locale, "
        "AvailableForReSale AS for_resale, IsResale AS is_resale FROM dbo.Storage"
    )
    agreements = query(
        """
        SELECT ca.ClientID AS cid, LOWER(CONVERT(varchar(36), ca.NetUID)) AS uid,
               org.StorageID AS org_storage, a.WithVATAccounting AS vat
        FROM dbo.ClientAgreement ca
        JOIN dbo.Client c ON c.ID = ca.ClientID AND c.Deleted = 0
        JOIN dbo.Agreement a ON a.ID = ca.AgreementID
        JOIN dbo.Organization org ON org.ID = a.OrganizationID AND org.Deleted = 0
        WHERE ca.Deleted = 0 AND ca.NetUID IS NOT NULL AND a.Deleted = 0 AND a.IsActive = 1
        ORDER BY ca.ClientID, ca.ID
        """
    )
    groups = query(
        history_cte
        + """
        SELECT g.ProductID AS pid, g.ProductGroupID AS gid
        FROM H JOIN dbo.ProductProductGroup g ON g.ProductID = H.pid
        WHERE g.Deleted = 0
        ORDER BY g.ProductID, g.ProductGroupID
        """,
        catalog_window,
    )
    return SnapshotData(
        as_of=as_of,
        floor=floor,
        rows=[
            (
                int(r["cid"]) if r["cid"] is not None else None,
                r["agr"],
                int(r["oid"]),
                r["created"],
                int(r["pid"]),
            )
            for r in rows
        ],
        synthetic=frozenset(synthetic),
        product_rows=[
            (
                int(r["old_id"]),
                bool(r["deleted"]),
                bool(r["has_code"]),
                int(r["max_live"]) if r["max_live"] is not None else None,
            )
            for r in product_rows
        ],
        availability=[(int(r["pid"]), int(r["sid"]), float(r["amt"])) for r in availability],
        storages=[
            {
                "id": int(r["id"]),
                "deleted": bool(r["deleted"]),
                "defective": bool(r["defective"]),
                "locale": r["locale"] or "",
                "for_resale": bool(r["for_resale"]),
                "is_resale": bool(r["is_resale"]),
            }
            for r in storages
        ],
        agreements=[
            (
                int(r["cid"]),
                r["uid"],
                int(r["org_storage"]) if r["org_storage"] is not None else None,
                bool(r["vat"]),
            )
            for r in agreements
        ],
        groups=[(int(r["pid"]), int(r["gid"])) for r in groups],
        negatives=_load_negatives(),
        siblings=repo.group_siblings(*repo.sibling_card_rows()),
        services=repo.service_product_ids(),
        open_lines=repo.open_order_rows(as_of),
    )


class Snapshot:
    def __init__(self, data: SnapshotData, params: Params | None = None):
        settings = get_settings()
        self.settings = settings
        self.params = params or default_params()
        self.as_of = data.as_of
        self.floor = data.floor
        self.as_of_dt = datetime.fromisoformat(data.as_of)
        self.synthetic = data.synthetic
        self.negatives = data.negatives

        ubiquity_clients: dict[int, set[int]] = defaultdict(set)
        ubiquity_population: set[int] = set()
        popularity: dict[int, int] = defaultdict(int)
        history: dict[int, list[tuple]] = defaultdict(list)
        ubiquity_start = rules.trailing_year(self.as_of_dt)
        for cid, agreement, order_id, created, pid in data.rows:
            if created >= self.as_of_dt:
                continue
            if cid is not None and created >= ubiquity_start:
                ubiquity_clients[pid].add(cid)
                ubiquity_population.add(cid)
            popularity[pid] += 1
            if cid is not None:
                history[cid].append((order_id, created, pid, agreement))
        self.history = history
        population = len(ubiquity_population)
        self.ubiquitous = self.synthetic | data.services | frozenset(
            pid
            for pid, clients in ubiquity_clients.items()
            if population and len(clients) / population > settings.ubiquity_exclude_pct
        )
        self.popular = [pid for pid, _ in sorted(popularity.items(), key=lambda kv: (-kv[1], kv[0]))]

        self.clients = sorted(history)
        self.client_index = {cid: i for i, cid in enumerate(self.clients)}
        self.products = sorted(popularity)
        self.product_index = {pid: i for i, pid in enumerate(self.products)}
        self.product_ids = np.array(self.products, dtype=np.int64)
        self.client_ids = np.array(self.clients, dtype=np.int64)
        matrix = np.zeros((len(self.clients), len(self.products)), dtype=np.float32)
        self.client_products: dict[int, set[int]] = {}
        for cid, lines in history.items():
            owned = {line[2] for line in lines}
            self.client_products[cid] = owned
            matrix[self.client_index[cid], [self.product_index[p] for p in owned]] = 1.0
        self.matrix = matrix
        self.matrix_t = np.ascontiguousarray(matrix.T)
        self.degree = np.maximum(matrix.sum(axis=0).astype(np.int64), 1)
        self.client_product_count = matrix.sum(axis=1).astype(np.int64)

        self.live: dict[int, int] = {}
        self.owned_live: dict[int, int] = {}
        for old_id, deleted, has_code, max_live in data.product_rows:
            if not deleted:
                self.owned_live[old_id] = old_id
            elif max_live is not None:
                self.owned_live[old_id] = max_live
            if has_code and max_live is not None:
                self.live[old_id] = max_live if deleted else old_id
        for live_id in list(self.live.values()):
            self.live.setdefault(live_id, live_id)

        self.availability = data.availability
        self.storages = data.storages
        resale = {
            st["id"] for st in data.storages
            if not st["deleted"] and not st["defective"] and (st["for_resale"] or st["is_resale"])
        }
        self.stock_global = frozenset(
            pid for pid, sid, amount in data.availability if amount > 0 and sid in resale
        )
        self._stock_profiles: dict[tuple, frozenset[int]] = {}

        self.agreements: dict[int, list[str]] = defaultdict(list)
        self.agreement_profile: dict[str, tuple] = {}
        for cid, uid, org_storage, vat in data.agreements:
            self.agreements[cid].append(uid)
            self.agreement_profile[uid] = (org_storage, vat)
        self.group = dict(data.groups)

        self.article = dict(data.siblings)
        cards: dict[int, list[int]] = defaultdict(list)
        for pid, article in self.article.items():
            cards[article].append(pid)
        self.cards = {article: tuple(sorted(members)) for article, members in cards.items()}
        open_products: dict[int, set[int]] = defaultdict(set)
        for cid, pid in data.open_lines:
            open_products[cid].add(pid)
        self.open_products = {
            cid: frozenset(repo.with_siblings(pids, self.article))
            for cid, pids in open_products.items()
        }

    def stock(self, agreement: str | None) -> frozenset[int]:
        if agreement is None:
            return self.stock_global
        profile = self.agreement_profile.get(agreement)
        if profile is None:
            return frozenset()
        if profile not in self._stock_profiles:
            org_storage, vat = profile
            allowed = {
                st["id"] for st in self.storages
                if not st["deleted"] and not st["defective"] and st["locale"] != "pl"
                and (st["id"] == org_storage or (not vat and st["for_resale"]))
            }
            totals: dict[int, float] = defaultdict(float)
            for pid, sid, amount in self.availability:
                if sid in allowed:
                    totals[pid] += amount
            self._stock_profiles[profile] = frozenset(p for p, total in totals.items() if total > 0)
        return self._stock_profiles[profile]

    def targets(self, clients: Sequence[int]) -> list[tuple[int, str | None]]:
        return [(cid, uid) for cid in clients for uid in [None, *self.agreements.get(cid, [])]]

    def context(self, customer_id: int, agreement: str | None) -> Context:
        return Context(self, customer_id, agreement)


def _normalize(scores: dict[int, float]) -> dict[int, float]:
    if not scores:
        return {}
    top = max(scores.values()) or 1.0
    return {k: v / top for k, v in scores.items()}


class Context:
    def __init__(self, snap: Snapshot, customer_id: int, agreement: str | None):
        self.snap = snap
        self.customer_id = customer_id
        self.agreement = agreement
        client_lines = snap.history.get(customer_id, [])
        lines = client_lines
        if agreement is not None:
            lines = [line for line in lines if line[3] == agreement]
        self.lines = lines
        orders: dict[int, set[int]] = defaultdict(set)
        last: dict[int, datetime] = {}
        for order_id, created, pid, _ in lines:
            orders[pid].add(order_id)
            if pid not in last or created > last[pid]:
                last[pid] = created
        self.frequency = {pid: len(ids) for pid, ids in orders.items()}
        self.last = last
        self.order_count = len({line[0] for line in lines})
        self.owned = set(self.frequency)
        self.owned_live = frozenset(repo.with_siblings(
            {snap.owned_live[pid] for pid in self.owned if pid in snap.owned_live}, snap.article,
        ))
        self.client_owned_live = self.owned_live if agreement is None else frozenset(
            repo.with_siblings(
                {snap.owned_live[line[2]] for line in client_lines if line[2] in snap.owned_live},
                snap.article,
            )
        )
        self.excluded = (
            snap.ubiquitous
            | snap.negatives.get(customer_id, frozenset())
            | snap.open_products.get(customer_id, frozenset())
        )
        self.stock = snap.stock(agreement)
        self._cards: dict[int, int | None] = {}
        self._cross_sell: list[tuple[int, float]] | None = None
        self._neighbour_scores: dict[int, float] | None = None

    def card(self, pid: int) -> int | None:
        if pid not in self._cards:
            snap = self.snap
            live = snap.live.get(pid, pid if pid in snap.article else None)
            found = None
            if live is not None:
                members = snap.cards.get(snap.article.get(live, live), ())
                found = next(
                    (c for c in (live, *members) if c in self.stock and c not in self.excluded),
                    None,
                )
            self._cards[pid] = found
        return self._cards[pid]

    def in_stock(self, pid: int) -> bool:
        return self.card(pid) is not None

    def segment(self) -> Segment:
        if self.order_count >= 500:
            return Segment.HEAVY
        if self.order_count >= 100:
            counts = [c for pid, c in self.frequency.items() if pid not in self.snap.synthetic]
            rate = sum(1 for c in counts if c >= 2) / len(counts) if counts else 0.0
            return Segment.REGULAR_CONSISTENT if rate >= 0.40 else Segment.REGULAR_EXPLORATORY
        return Segment.LIGHT

    def seed_products(self) -> list[int]:
        return sorted(
            self.frequency,
            key=lambda pid: (-self.frequency[pid], -self.last[pid].timestamp(), pid),
        )

    def _similar(self) -> list[tuple[int, float]]:
        snap = self.snap
        ordered = sorted(self.lines, key=lambda line: line[2])
        ordered.sort(key=lambda line: line[0], reverse=True)
        ordered.sort(key=lambda line: line[1], reverse=True)
        target = {line[2] for line in ordered[:_TARGET_ROW_LIMIT]}
        if not target:
            return []
        overlap = snap.matrix_t[[snap.product_index[p] for p in target]].sum(axis=0)
        own = snap.client_index.get(self.customer_id)
        if own is not None:
            overlap[own] = 0
        idx = np.nonzero(overlap > 0)[0]
        ranked = idx[np.lexsort((snap.client_ids[idx], -overlap[idx]))][:_CANDIDATE_LIMIT]
        sims: list[tuple[int, float]] = []
        for i in ranked:
            shared = int(overlap[i])
            jaccard = shared / (len(target) + int(snap.client_product_count[i]) - shared)
            if jaccard >= _MIN_SIMILARITY:
                sims.append((int(snap.client_ids[i]), jaccard))
        sims.sort(key=lambda item: (-item[1], item[0]))
        return sims[:_MAX_SIMILAR]

    def _collaborative(self) -> dict[int, float]:
        if self._neighbour_scores is not None:
            return self._neighbour_scores
        sims = self._similar()
        total: dict[int, Decimal] = defaultdict(Decimal)
        count: dict[int, int] = defaultdict(int)
        for neighbour, similarity in sims:
            weight = Decimal(repr(similarity))
            for pid in self.snap.client_products[neighbour]:
                if pid not in self.owned:
                    total[pid] += weight
                    count[pid] += 1
        self._neighbour_scores = {
            pid: float(total[pid] / count[pid]) for pid in total if count[pid] >= 2
        }
        return self._neighbour_scores

    def cross_sell_ranking(self, seed_order: Sequence[int] | None = None) -> list[tuple[int, float]]:
        if self._cross_sell is not None:
            return self._cross_sell
        snap = self.snap
        order = seed_order if seed_order is not None else self.seed_products()
        own = {pid: float(self.frequency[pid]) for pid in order if pid not in self.excluded}
        seeds = list(own)[:_SEED_LIMIT]
        co: dict[int, float] = {}
        if seeds:
            seed_idx = np.array([snap.product_index[p] for p in seeds])
            seed_rows = snap.matrix_t[seed_idx]
            buyers = np.nonzero(seed_rows.any(axis=0))[0]
            counts = seed_rows[:, buyers] @ snap.matrix[buyers]
            si, ci = np.nonzero(counts >= 2)
            values = counts[si, ci].astype(np.float64)
            top = np.lexsort((snap.product_ids[ci], snap.product_ids[seed_idx][si], -values))[:_COOC_ROW_CAP]
            contribution = values[top] / np.sqrt(
                (snap.degree[seed_idx[si[top]]] * snap.degree[ci[top]]).astype(np.float64)
            )
            scores = np.zeros(len(snap.products), dtype=np.float64)
            np.add.at(scores, ci[top], contribution)
            co = {
                int(snap.product_ids[j]): float(scores[j])
                for j in np.unique(ci[top])
                if int(snap.product_ids[j]) not in self.excluded
            }
        own_n, co_n = _normalize(own), _normalize(co)
        combined = {pid: (0.6 * score, False) for pid, score in own_n.items()}
        for pid, score in co_n.items():
            previous = combined.get(pid, (0.0, True))
            combined[pid] = (previous[0] + 0.4 * score, pid not in own_n)
        ranked = sorted(combined.items(), key=lambda item: (-item[1][0], item[0]))
        self._cross_sell = [(pid, score) for pid, (score, discovery) in ranked if discovery]
        return self._cross_sell

    def cross_sell(self, top_n: int, seed_order: Sequence[int] | None = None) -> list[tuple[int, float]]:
        snap = self.snap
        window = self.cross_sell_ranking(seed_order)[: max(top_n * 4, 100)]
        ranked = [item for item in window if self.in_stock(item[0])][:top_n]
        seen: set[int] = set()
        out: list[tuple[int, float]] = []
        for pid, score in ranked:
            card = self.card(pid)
            article = snap.article.get(card, card)
            if card is None or article in seen:
                continue
            seen.add(article)
            if card in self.client_owned_live:
                continue
            out.append((card, round(score, 4)))
        return out

    def copurchase_result(
        self, top_n: int, seed_order: Sequence[int] | None = None,
    ) -> RecommendationResult:
        items = self.cross_sell(top_n, seed_order)
        return RecommendationResult(
            customer_id=self.customer_id,
            client_agreement_net_id=self.agreement,
            recommendations=[
                ProductRec(
                    product_id=pid, score=score, rank=i + 1, segment="COPURCHASE",
                    source=RecSource.DISCOVERY, source_detail=RecSourceDetail.COPURCHASE,
                )
                for i, (pid, score) in enumerate(items)
            ],
            count=len(items),
            discovery_count=len(items),
            segment="COPURCHASE",
            precision_estimate=measured.precision("copurchase", top_n),
            latency_ms=0.0,
            as_of_date=self.snap.as_of,
            source_history_start=self.snap.floor,
            effective_start=self.snap.floor,
            history_complete=True,
            model_version=_COPURCHASE_MODEL_VERSION,
        )

    def _diversify(self, items: list[tuple[int, float]]) -> list[tuple[int, float]]:
        limit = self.snap.params.max_per_group
        counts: dict[int, int] = {}
        kept: list[tuple[int, float]] = []
        for item in items:
            gid = self.snap.group.get(item[0])
            if gid is None or counts.get(gid, 0) < limit:
                kept.append(item)
                if gid is not None:
                    counts[gid] = counts.get(gid, 0) + 1
        return kept

    def _evidence(
        self, card: int, source: RecSource, detail: RecSourceDetail,
    ) -> tuple[RecSource, RecSourceDetail]:
        if source == RecSource.DISCOVERY and card in self.client_owned_live:
            return RecSource.REPURCHASE, RecSourceDetail.REPURCHASE_HISTORY
        return source, detail

    def _distinct(
        self, items: list[tuple[int, float]], taken: set[int], limit: int,
    ) -> list[tuple[int, int]]:
        seen: set[int] = set()
        out: list[tuple[int, int]] = []
        for pid, _ in items:
            card = self.card(pid)
            article = self.snap.article.get(card, card)
            if card is None or article in taken or article in seen:
                continue
            seen.add(article)
            out.append((pid, card))
            if len(out) >= limit:
                break
        return out

    def personal_result(
        self,
        top_n: int | None = None,
        include_discovery: bool = True,
        seed_order: Sequence[int] | None = None,
    ) -> RecommendationResult:
        snap, settings, params = self.snap, self.snap.settings, self.snap.params
        top_n = top_n or settings.default_top_n
        repurchase_n = min(params.repurchase_count, top_n)
        discovery_n = max(top_n - repurchase_n, 0) if include_discovery else 0
        segment = self.segment()
        w_freq, w_rec = _WEIGHTS[segment]
        excluded = self.excluded
        owned_live = self.owned_live if include_discovery else frozenset()

        freq = _normalize({p: float(c) for p, c in self.frequency.items() if p not in excluded})
        recency = _normalize({
            p: math.exp(-(snap.as_of_dt - dt).days / params.halflife_days)
            for p, dt in self.last.items()
            if p not in excluded
        })
        scores = {
            p: w_freq * freq.get(p, 0.0) + w_rec * recency.get(p, 0.0)
            for p in set(freq) | set(recency)
        }
        if params.seasonal_boost:
            start = snap.as_of_dt - timedelta(days=380)
            end = snap.as_of_dt - timedelta(days=320)
            for line in self.lines:
                if start <= line[1] < end and line[2] in scores:
                    scores[line[2]] = max(
                        scores[line[2]],
                        w_freq * freq.get(line[2], 0.0) + w_rec * recency.get(line[2], 0.0)
                        + params.seasonal_boost,
                    )
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:_STOCK_POOL_CAP]
        taken: set[int] = set()
        out: list[tuple[int, float, RecSource, RecSourceDetail]] = []

        def take(items: list[tuple[int, float]], reserve: int, limit: int,
                 source: RecSource, detail: RecSourceDetail) -> None:
            pool = self._distinct(items, taken, reserve)
            cards = dict(pool)
            for pid, score in self._diversify([(pid, score) for pid, score in items
                                               if pid in cards][:reserve])[:limit]:
                taken.add(snap.article.get(cards[pid], cards[pid]))
                out.append((cards[pid], float(score), *self._evidence(cards[pid], source, detail)))

        take(ranked, repurchase_n + 10, repurchase_n,
             RecSource.REPURCHASE, RecSourceDetail.REPURCHASE_HISTORY)

        if discovery_n > 0 and params.discovery_source == "similar_clients":
            collab = {p: v for p, v in self._collaborative().items() if p not in excluded}
            candidates = sorted(collab.items(), key=lambda item: (-item[1], item[0]))[:_STOCK_POOL_CAP]
            candidates = [item for item in candidates if self.card(item[0]) not in owned_live]
            take(candidates, discovery_n + 5, discovery_n,
                 RecSource.DISCOVERY, RecSourceDetail.SIMILAR_CLIENTS)

        if include_discovery and len(out) < top_n:
            base = min((item[1] for item in out), default=1.0)
            emitted = 0

            def fill(candidate: int | None, detail: RecSourceDetail) -> None:
                nonlocal emitted
                card = self.card(candidate) if candidate is not None else None
                article = snap.article.get(card, card)
                if card is None or article in taken or card in owned_live:
                    return
                taken.add(article)
                emitted += 1
                out.append((
                    card, round(max(base - 1e-4 * emitted, 0.0), 6),
                    *self._evidence(card, RecSource.DISCOVERY, detail),
                ))

            for pid, _ in self.cross_sell(top_n * 2, seed_order):
                if len(out) >= top_n:
                    break
                fill(pid, RecSourceDetail.COPURCHASE)
            if len(out) < top_n:
                pool_size = min(_STOCK_POOL_CAP, max(top_n * 5, top_n + len(excluded) + len(out)))
                popular = [
                    p for p in snap.popular[: pool_size + len(excluded)] if p not in excluded
                ][:pool_size]
                if params.popular_scope == "client_groups" and self.owned:
                    own_groups = {snap.group.get(p) for p in self.owned} - {None}
                    popular = [p for p in snap.popular if p not in excluded
                               and snap.group.get(p) in own_groups][:pool_size]
                for pid in popular:
                    if len(out) >= top_n:
                        break
                    fill(pid, RecSourceDetail.GLOBAL_POPULAR)

        final = [
            ProductRec(
                product_id=card, score=score, rank=rank, segment=segment.value,
                source=source, source_detail=detail,
            )
            for rank, (card, score, source, detail) in enumerate(out, start=1)
        ]
        return RecommendationResult(
            customer_id=self.customer_id,
            client_agreement_net_id=self.agreement,
            recommendations=final,
            count=len(final),
            discovery_count=sum(1 for r in final if r.source == RecSource.DISCOVERY),
            segment=segment.value,
            precision_estimate=measured.precision("personal", top_n),
            latency_ms=0.0,
            cached=False,
            as_of_date=snap.as_of,
            source_history_start=snap.floor,
            effective_start=snap.floor,
            history_complete=True,
        )


def load(as_of: str) -> Snapshot:
    return Snapshot(load_data(as_of))
