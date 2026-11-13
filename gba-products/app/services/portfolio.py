"""Portfolio builder — joins all per-SKU signals into one classified, health-scored table.

One heavy build per as_of (cached); the /assortment/* and /product/{id} endpoints slice it.
ABC is ranked by REVENUE-€ contribution (the standard, universally-computable basis — purchase cost
exists only for on-hand stock, so a margin-ABC would silently drop every non-stocked SKU). Margin% is
still computed per-SKU (where cost is known) and feeds the health-score. Margin compares revenue
and cost on one basis, both excluding recoverable VAT: units sold with VAT are set against the
lot cost without its recorded VAT, units sold without VAT against the cost as booked.
revenue_eur and unit_cost_eur keep their own bases (invoice gross with VAT, lot AccountingPrice)
for turnover and stock value.
"""
from __future__ import annotations

from decimal import Decimal, localcontext

from app.core import exact_numbers as exact
from app.core.config import get_settings
from app.core.history import window_metadata
from app.data import signals_repository as sig
from app.data import canonical_sales as canonical
from app.services import classification as cl
from app.services import health_score, history_policy
from app.services.stock_health import classify_band


def build_portfolio(as_of: str) -> dict:
    cfg = get_settings()
    windows = history_policy.portfolio_windows(as_of, cfg)
    velocity_days = windows["velocity"].effective_days

    stock = _index_by_product(sig.on_hand_stock(), "on_hand_stock")
    vel = _index_by_product(
        sig.sales_velocity(as_of, cfg.velocity_window_days),
        "sales_velocity",
    )
    sold_recently = {
        exact.positive_int(product_id, "sold_product_ids.product_id")
        for product_id in sig.sold_product_ids(as_of, cfg.dead_window_days)
    }
    prices = _index_by_product(
        sig.avg_sale_price_eur(as_of, cfg.dead_window_days),
        "avg_sale_price_eur",
    )
    return_prices = prices if cfg.return_window_days == cfg.dead_window_days else _index_by_product(
        sig.avg_sale_price_eur(as_of, cfg.return_window_days), "return_window_sales")
    rets = _index_by_product(
        sig.returns_for_products(as_of, cfg.return_window_days),
        "returns_for_products",
    )

    net_returns = rets if cfg.return_window_days == cfg.dead_window_days else _index_by_product(
        sig.returns_for_products(as_of, cfg.dead_window_days), "net_sales_returns")

    labels = cl.month_labels(
        as_of,
        cfg.classify_months,
        cfg.source_history_start_date,
    )
    month_days = cl.month_observed_days(labels, as_of, cfg.source_history_start_date)
    history_months = max(cfg.classify_months, cfg.lifecycle_first_sale_months)
    history_labels = cl.month_labels(as_of, history_months, cfg.source_history_start_date)
    history_index = {label: index for index, label in enumerate(history_labels)}
    classification_labels = set(labels)
    monthly: dict[int, dict[str, Decimal]] = {}
    first_sale_index: dict[int, int] = {}
    seen_months: set[tuple[int, str]] = set()
    for r in sig.monthly_units(as_of, history_months):
        pid = exact.positive_int(r.get("product_id"), "monthly_units.product_id")
        label = str(r.get("ym") or "")
        if label not in history_index:
            raise ValueError(f"monthly_units returned unexpected month {label!r}")
        if (pid, label) in seen_months:
            raise ValueError(f"monthly_units returned duplicate ({pid}, {label})")
        seen_months.add((pid, label))
        units = exact.decimal_value(
            r.get("units") or 0,
            "monthly_units.units",
        )
        if units > 0:
            first_sale_index[pid] = min(first_sale_index.get(pid, history_index[label]), history_index[label])
        if label in classification_labels:
            monthly.setdefault(pid, {})[label] = units

    universe = set(stock) | set(monthly) | set(vel) | set(prices) | set(net_returns) | set(rets)
    rows: list[dict] = []
    for pid in universe:
        st = stock.get(pid)
        qty = exact.decimal_value(
            st["qty_on_hand"] if st else 0,
            "qty_on_hand",
            non_negative=True,
        )
        unit_cost_raw = st.get("unit_cost_eur") if st else None
        unit_cost = (
            exact.decimal_value(unit_cost_raw, "unit_cost_eur", non_negative=True)
            if unit_cost_raw is not None
            else None
        )
        unit_cost_ex_vat_raw = st.get("unit_cost_ex_vat_eur") if st else None
        unit_cost_ex_vat = (
            exact.decimal_value(unit_cost_ex_vat_raw, "unit_cost_ex_vat_eur", non_negative=True)
            if unit_cost_ex_vat_raw is not None
            else None
        )
        if (unit_cost is None) != (unit_cost_ex_vat is None):
            raise ValueError(f"stock product {pid} has one cost basis without the other")
        eur_value_raw = st.get("eur_value") if st else None
        if unit_cost is None and eur_value_raw is not None:
            raise ValueError(f"stock product {pid} has value without unit cost")
        if unit_cost is not None and eur_value_raw is None:
            raise ValueError(f"stock product {pid} has unit cost without value")
        eur_value = exact.decimal_value(
            eur_value_raw or 0,
            "eur_value",
            non_negative=True,
        )

        sold_recent_qty = exact.decimal_value(
            vel[pid]["sold_qty"] if pid in vel else 0,
            "sold_qty",
        )
        if velocity_days == 0 and sold_recent_qty > 0:
            raise ValueError("sales velocity has demand outside the effective history window")
        daily_rate = (
            max(Decimal(0), sold_recent_qty) / Decimal(velocity_days)
            if velocity_days > 0
            else Decimal("0")
        )
        band = classify_band(float(qty), float(daily_rate), pid in sold_recently, cfg)
        cover = (qty / daily_rate) if daily_rate > 0 else None

        monthly_values = monthly.get(pid, {})
        series = cl.series_from(
            {label: float(max(Decimal(0), value)) for label, value in monthly_values.items()},
            labels,
        )
        annual_units = sum(
            (monthly_values.get(label, Decimal("0")) for label in labels),
            Decimal("0"),
        )
        xyz = cl.xyz_from_series(series, cfg, month_days)
        first_index = first_sale_index.get(pid)
        days_since_first = (
            (len(history_labels) - 1 - first_index) * 30 + 15
            if first_index is not None
            else None
        )
        lifecycle = cl.lifecycle_from_series(
            series, days_since_first, pid in sold_recently, cfg, month_days=month_days)

        price_row = prices.get(pid)
        revenue_eur = exact.decimal_value(
            price_row.get("revenue_eur") if price_row else 0,
            "revenue_eur",
        )
        priced_qty = exact.decimal_value(
            price_row.get("sold_qty") if price_row else 0,
            "priced sold_qty",
        )
        avg_price = revenue_eur / priced_qty if priced_qty > 0 else Decimal("0")
        if price_row is not None and price_row.get("avg_price_eur") is not None:
            reported_avg = exact.unit_price(price_row["avg_price_eur"], "repository avg_price_eur", non_negative=False)
            if priced_qty != 0 and reported_avg != exact.unit_price(revenue_eur / priced_qty, "derived avg_price_eur", non_negative=False):
                raise ValueError(f"price aggregate mismatch for product {pid}")
        revenue_ex_vat = None
        if (price_row is not None and price_row.get("revenue_ex_vat_eur") is not None
                and not exact.non_negative_int(
                    price_row.get("ex_vat_unknown_lines") or 0, "ex-VAT unknown lines")):
            revenue_ex_vat = exact.decimal_value(
                price_row["revenue_ex_vat_eur"], "revenue_ex_vat_eur")
        avg_price_ex_vat = (
            revenue_ex_vat / priced_qty if revenue_ex_vat is not None and priced_qty > 0 else None
        )
        margin_pct = None
        margin_ex_vat = None
        margin_unit_cost = None
        if unit_cost_ex_vat is not None and avg_price_ex_vat is not None and avg_price_ex_vat > 0:
            vat_qty = exact.decimal_value(price_row.get("vat_sold_qty") or 0, "vat_sold_qty")
            with localcontext() as context:
                context.prec = 50
                cost = unit_cost_ex_vat * vat_qty + unit_cost * (priced_qty - vat_qty)
                margin_ex_vat = revenue_ex_vat - cost
                margin_unit_cost = cost / priced_qty
                margin_pct = margin_ex_vat / revenue_ex_vat
        returned_qty = exact.decimal_value(
            rets[pid].get("returned_qty") if pid in rets else 0,
            "returned_qty",
        )
        return_sold_qty = exact.decimal_value(return_prices.get(pid, {}).get("sold_qty", 0), "return-window sold quantity")
        return_rate = (max(Decimal(0), returned_qty) / return_sold_qty) if return_sold_qty > 0 else Decimal("0")
        net_return = net_returns.get(pid)
        known_returned = exact.decimal_value(
            net_return["known_returned_value_eur"] if net_return else 0, "known returned amount")
        unresolved = exact.non_negative_int(
            net_return["unresolved_amount_count"] if net_return else 0, "unresolved amount count")
        with localcontext() as context:
            context.prec = 50
            known_net = revenue_eur - known_returned
        financial_basis = {"revenue_eur": format(revenue_eur, "f"),
                           "units": format(priced_qty, "f"),
                           "unit_cost_eur": None if unit_cost is None else format(unit_cost, "f"),
                           "revenue_ex_vat_eur": (
                               None if revenue_ex_vat is None else format(revenue_ex_vat, "f")),
                           "margin_ex_vat_eur": (
                               None if margin_ex_vat is None else format(margin_ex_vat, "f")),
                           "known_returned_value_eur": format(known_returned, "f"),
                           "unresolved_amount_count": unresolved}
        rows.append({
            "product_id": pid,
            "qty_on_hand": exact.quantity(qty, "qty_on_hand"),
            "eur_value": exact.money(eur_value, "eur_value"),
            "stock_value_eur": (
                exact.money(eur_value, "stock_value_eur")
                if unit_cost is not None or qty == 0
                else None
            ),
            "unit_cost_eur": (
                exact.unit_price(unit_cost, "unit_cost_eur")
                if unit_cost is not None
                else None
            ),
            "avg_price_eur": (
                exact.unit_price(avg_price, "avg_price_eur")
                if avg_price > 0
                else None
            ),
            "margin_pct": (
                exact.ratio(margin_pct, "margin_pct")
                if margin_pct is not None
                else None
            ),
            "margin_vat_basis": "ex-vat",
            "unit_cost_ex_vat_eur": (
                exact.unit_price(unit_cost_ex_vat, "unit_cost_ex_vat_eur")
                if unit_cost_ex_vat is not None
                else None
            ),
            "margin_unit_cost_eur": (
                exact.unit_price(margin_unit_cost, "margin_unit_cost_eur")
                if margin_unit_cost is not None
                else None
            ),
            "avg_price_ex_vat_eur": (
                exact.unit_price(avg_price_ex_vat, "avg_price_ex_vat_eur", non_negative=False)
                if avg_price_ex_vat is not None and avg_price_ex_vat > 0
                else None
            ),
            "revenue_ex_vat_eur": (
                exact.money(revenue_ex_vat, "revenue_ex_vat_eur", non_negative=False)
                if revenue_ex_vat is not None
                else None
            ),
            "margin_ex_vat_eur": (
                exact.money(margin_ex_vat, "margin_ex_vat_eur", non_negative=False)
                if margin_ex_vat is not None
                else None
            ),
            "annual_units": exact.quantity(priced_qty, "annual_units", non_negative=False),
            "classification_units": exact.quantity(annual_units, "classification units", non_negative=False),
            "return_sold_units": exact.quantity(return_sold_qty, "return-window sold units", non_negative=False),
            "returned_units": exact.quantity(returned_qty, "returned_units", non_negative=False),
            "revenue_eur": exact.money(revenue_eur, "revenue_eur", non_negative=False),
            "gross_revenue_eur": exact.money(revenue_eur, "gross revenue", non_negative=False),
            "returned_value_eur": None if unresolved else exact.money(known_returned, non_negative=False),
            "known_returned_value_eur": exact.money(known_returned, non_negative=False),
            "net_revenue_eur": None if unresolved else exact.money(known_net, non_negative=False),
            "known_net_revenue_eur": exact.money(known_net, non_negative=False),
            "unresolved_amount_count": unresolved,
            "financial_basis": financial_basis,
            "demand_adjustments_present": sold_recent_qty < 0 or any(v < 0 for v in monthly_values.values()),
            "cover_days": exact.cover_days(cover) if cover is not None else None,
            "return_rate": exact.ratio(return_rate, "return_rate", non_negative=True),
            "band": band.value,
            "xyz": xyz.value,
            "lifecycle": lifecycle.value,
            "_band_enum": band,
            "_xyz_enum": xyz,
            "_lifecycle_enum": lifecycle,
            "_margin_pct_raw": float(margin_pct) if margin_pct is not None else None,
            "_return_rate_raw": float(return_rate),
        })

    _assign_abc(rows, cfg)
    for row in rows:
        health, breakdown = health_score.score(
            row["_band_enum"],
            row["_lifecycle_enum"],
            row["_margin_pct_raw"],
            row["_xyz_enum"],
            row["_return_rate_raw"],
            cfg,
            abc=row["abc"],
        )
        demand, demand_breakdown = health_score.demand_score(
            row["_band_enum"],
            row["_lifecycle_enum"],
            row["_xyz_enum"],
            row["abc"],
            cfg,
        )
        margin, margin_breakdown = health_score.margin_score(
            row["_margin_pct_raw"],
            row["_return_rate_raw"],
            cfg,
            abc=row["abc"],
        )
        action, reasons = health_score.action_label(
            row["_band_enum"],
            row["_lifecycle_enum"],
            row["abc"],
            row["_margin_pct_raw"],
            row["_return_rate_raw"],
            demand,
            margin,
            cfg,
        )
        row["health"] = health
        row["health_components"] = breakdown
        row["demand_score"] = demand
        row["demand_components"] = demand_breakdown
        row["margin_score"] = margin
        row["margin_components"] = margin_breakdown
        row["action_label"] = action
        row["action_reasons"] = reasons
        for key in ("_band_enum", "_xyz_enum", "_lifecycle_enum", "_margin_pct_raw", "_return_rate_raw"):
            row.pop(key, None)
    result = {
        "as_of": as_of,
        "model_version": cfg.model_version,
        **history_policy.portfolio_metadata(as_of, cfg),
        "count": len(rows),
        "overview": {**_overview(rows), **history_policy.stock_observation(as_of)},
        "financial_window": window_metadata(windows["dead"]),
        "rows": rows,
    }
    _validate_portfolio_result(result)
    return result


def _index_by_product(rows: list[dict], source: str) -> dict[int, dict]:
    indexed: dict[int, dict] = {}
    for row in rows:
        pid = exact.positive_int(row.get("product_id"), f"{source}.product_id")
        if pid in indexed:
            raise ValueError(f"{source} returned duplicate product_id {pid}")
        indexed[pid] = row
    return indexed


def _assign_abc(rows: list[dict], cfg) -> None:
    # Signed accounting adjustments remain in financial totals. ABC ranks positive
    # commercial contribution only; nonpositive contribution is explicitly unknown.
    def contribution(row):
        return max(Decimal(0), exact.decimal_value(
            row.get("financial_basis", {}).get("revenue_eur", row["revenue_eur"]), "ABC contribution"))
    with localcontext() as context:
        context.prec = 50
        total = sum((contribution(row) for row in rows), Decimal(0))
        cumulative = Decimal(0)
        for row in sorted(rows, key=lambda row: (contribution(row), -int(row["product_id"])), reverse=True):
            amount = contribution(row)
            if amount <= 0 or total <= 0:
                row["abc"] = "unknown"
            else:
                cumulative += amount
                row["abc"] = cl.abc_classify(float(cumulative / total), cfg).value


def _overview(rows: list[dict]) -> dict:
    def tally(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            value = r[key] if r[key] is not None else "unknown"
            out[value] = out.get(value, 0) + 1
        return out

    return {
        "total_skus": len(rows),
        "total_eur_value": exact.money(
            exact.decimal_sum(
                [row["eur_value"] for row in rows],
                "overview eur_value",
                non_negative=True,
            ),
            "total_eur_value",
        ),
        **_stock_value_summary(rows),
        **canonical.money_summary([row["financial_basis"] for row in rows]),
        "by_band": tally("band"),
        "by_lifecycle": tally("lifecycle"),
        "by_action": tally("action_label"),
        "by_abc": tally("abc"),
        "by_xyz": tally("xyz"),
        "avg_health": round(sum(r["health"] for r in rows) / len(rows), 1) if rows else 0.0,
    }


def _stock_value_summary(rows: list[dict]) -> dict:
    stocked = [row for row in rows if exact.decimal_value(row["qty_on_hand"], "qty_on_hand") > 0]
    unvalued = [row for row in stocked if row.get("stock_value_eur") is None]
    valued = len(stocked) - len(unvalued)
    return {
        "stock_skus": len(stocked),
        "stock_valued_skus": valued,
        "stock_unvalued_skus": len(unvalued),
        "stock_unvalued_qty": exact.quantity(
            exact.decimal_sum([row["qty_on_hand"] for row in unvalued], "unvalued stock"),
            "stock_unvalued_qty",
        ),
        "stock_value_eur": (
            None
            if stocked and not valued
            else exact.money(
                exact.decimal_sum(
                    [row["stock_value_eur"] for row in stocked if row["stock_value_eur"] is not None],
                    "stock value",
                    non_negative=True,
                ),
                "stock_value_eur",
            )
        ),
        "stock_value_complete": not unvalued,
    }


def _validate_portfolio_result(result: dict) -> None:
    rows = result["rows"]
    product_ids = [exact.positive_int(row.get("product_id"), "portfolio.product_id") for row in rows]
    if len(product_ids) != len(set(product_ids)):
        raise ValueError("portfolio rows contain duplicate product_id")
    if result["count"] != len(rows):
        raise ValueError("portfolio count does not match rows")

    overview = result["overview"]
    for field in ("by_band", "by_lifecycle", "by_action", "by_abc", "by_xyz"):
        if sum(overview[field].values()) != len(rows):
            raise ValueError(f"overview {field} count does not match rows")

    expected_value = exact.money(
        exact.decimal_sum(
            [row["eur_value"] for row in rows],
            "portfolio eur_value",
            non_negative=True,
        ),
        "portfolio total_eur_value",
    )
    expected_revenue = exact.money(
        exact.decimal_sum(
            [row.get("financial_basis", {}).get("revenue_eur", row["revenue_eur"]) for row in rows],
            "portfolio revenue_eur",
        ),
        "portfolio total_revenue_eur", non_negative=False,
    )
    if overview["total_eur_value"] != expected_value:
        raise ValueError("overview total_eur_value does not match rows")
    if overview["total_revenue_eur"] != expected_revenue:
        raise ValueError("overview total_revenue_eur does not match rows")
    if rows and all("financial_basis" in row for row in rows):
        expected = canonical.money_summary([row["financial_basis"] for row in rows])
        if any(overview.get(key) != value for key, value in expected.items()):
            raise ValueError("overview financial totals do not match canonical additive basis")
