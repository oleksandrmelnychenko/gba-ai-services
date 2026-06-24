"""ALWAYS-ON source guards (no DB, run in normal pytest/CI).

Mocked unit tests stayed GREEN while real correctness bugs shipped to the live engine. These
guards read the repository / service / engine module SOURCE (inspect.getsource) and assert the
fixed SQL/code patterns are present (or the reverted-bug patterns are absent), so reintroducing a
just-fixed bug fails CI immediately — independent of any mock or DB.

Each guard names the specific reintroduction it catches.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

from app.data import pricing_repository as repo
from app.services.pricing import recommend as engine
from app.services.pricing import service


def _code_without_docstring(fn) -> str:
    """The function's source with its docstring stripped, so forbidden-pattern guards match the
    executable body / SQL literals only (prose that explains *why* a bug is avoided is allowed to
    name the bug)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def test_peer_band_uses_priceperitem_as_eur_not_fx_converted():
    """REVERT CAUGHT: wrapping OrderItem.PricePerItem in GetExchangedToEuroValue. PricePerItem is
    ALREADY EUR; dividing a UAH agreement's price by the ~52 UAH/EUR rate collapses realized price
    to ~2% of true and poisons the peer P50."""
    code = _code_without_docstring(repo.peer_band)
    assert "oi.PricePerItem AS eur_price" in code
    assert "GetExchangedToEuroValue" not in code
    assert "CurrencyID = 2 THEN" not in code


def test_active_group_discount_does_not_filter_deleted():
    """REVERT CAUGHT: adding `AND ProductGroupDiscount.Deleted = 0` (or any Deleted predicate) to
    the active-discount lookup. The live SQL price function gates the discount lever by IsActive=1
    and ignores ProductGroupDiscount.Deleted. A Deleted predicate here would silently drop the real
    @DiscountRate the engine consumes."""
    code = _code_without_docstring(repo.active_group_discount)
    assert "IsActive = 1" in code
    assert "Deleted" not in code


def test_segment_cap_population_matches_lever_does_not_filter_pgd_deleted():
    """REVERT CAUGHT: adding `AND pgd.Deleted = 0` to the peer-discount CAP. The cap MUST be
    measured on the same ProductGroupDiscount lever population the live engine consumes: IsActive=1,
    pgd.Deleted ignored. ClientAgreement/Agreement Deleted are separate serving-target flags and
    must stay filtered so stale sync leftovers do not contaminate current tier norms."""
    code = _code_without_docstring(repo.segment_discount_distribution)
    assert "pgd.IsActive = 1" in code
    assert "pgd.Deleted" not in code
    assert "ca.Deleted = 0" in code
    assert "a.Deleted = 0" in code


def test_resolve_client_agreement_rejects_deleted_agreement_chain():
    """REVERT CAUGHT: serving fresh AI recommendations against soft-deleted client agreements.
    The legacy SQL price function can still calculate old NetUIDs, but the API target must be a
    current ClientAgreement + Agreement chain."""
    code = _code_without_docstring(repo.resolve_client_agreement)
    assert "ca.NetUID = :uid" in code
    assert "ca.Deleted = 0" in code
    assert "a.Deleted = 0" in code


def test_segment_cap_uses_actual_pricing_not_base_pricing_family():
    """REVERT CAUGHT: grouping discount caps by dbo.GetBasePricingId(a.PricingID). ЦО2 and
    ЦО1/ЦP share the same base ProductPricing row but have different discount norms; pooling the
    base family lets ЦО1's commercial discount band leak into ЦО2 agreements."""
    code = _code_without_docstring(repo.segment_discount_distribution)
    assert "a.PricingID = :pricing_id" in code
    assert "GetBasePricingId" not in code

    svc_code = _code_without_docstring(service.recommend_price)
    assert "agreement.get('pricing_id')" in svc_code
    assert "base_pricing_id" not in svc_code


def test_is_promotional_present_and_service_derives_marked_up_from_baseline():
    """REVERT CAUGHT: dropping the promotional branch or re-resolving marked_up from the pricing
    tier. On the promo branch the engine FORCES DiscountRate=0, so marked_up must use applied=0;
    the service must derive marked_up from the authoritative baseline (not re-read the tier), and
    must not apply normal ProductGroupDiscount caps because that DiscountRate lever is disabled."""
    assert hasattr(repo, "is_promotional")
    promo_code = _code_without_docstring(repo.is_promotional)
    assert "PromotionalPricingID IS NOT NULL" in promo_code

    svc_code = _code_without_docstring(service.recommend_price)
    assert "is_promotional" in svc_code
    assert "_marked_up_from_baseline" in svc_code
    assert "'p75': 0.0" in svc_code
    assert "'p90': 0.0" in svc_code

    helper_code = _code_without_docstring(service._marked_up_from_baseline)
    assert "baseline / (1.0 - applied_discount_pct / 100.0)" in helper_code


def test_unit_cost_excludes_1c_debt_lots():
    """REVERT CAUGHT: dropping the ProductIncome join / SourceDocumentType exclusion from the cost
    median+fallback. 1С balance-import lots (SourceDocumentType=1) carry inflated AccountingPrice
    (~800-26683 EUR) and would inflate the margin floor (verified: product 26168142 naive median
    587.58 EUR vs debt-excluded 15.00 EUR)."""
    code = _code_without_docstring(repo.unit_cost_eur)
    assert "dbo.ProductIncome" in code
    assert "SourceDocumentType <> :debt_doc_type" in code
    assert "JOIN dbo.Consignment c ON c.ID = ci.ConsignmentID" in code
    assert code.count("SourceDocumentType <> :debt_doc_type") >= 3


def test_service_guards_nonpositive_baseline_with_no_baseline_rationale():
    """REVERT CAUGHT: removing the `baseline is None or baseline <= 0` guard, which would run the
    A+B math on a 0 baseline and recommend 0.0 / a bogus floor. The guard must short-circuit to the
    no-baseline rationale instead."""
    code = _code_without_docstring(service.recommend_price)
    assert "baseline is None or baseline <= 0" in code
    assert "_no_baseline_recommendation" in code

    no_base_code = _code_without_docstring(service._no_baseline_recommendation)
    assert "recommended_price=None" in no_base_code
    assert "engine.R_NO_BASELINE" in no_base_code
    assert engine.R_NO_BASELINE == "no-baseline"


def test_peer_band_rejects_uom_outliers_by_mad_not_fixed_decile():
    """REVERT CAUGHT: reverting the per-product median/MAD outlier reject back to the fixed
    bottom/top-decile trim (PERCENT_RANK 0.10..0.90). The decile trim leaks UoM piece-vs-box
    outliers whenever the contaminated fraction exceeds 10% (product 25104373: decile band
    p75/p25=2.0 vs MAD 1.92; 25104980: 1.53 vs 1.03). The MAD modified-z (k*1.4826*MAD around the
    median) adapts to the contaminated fraction. Guards the CTE chain + the consistency factor +
    the keep-all fallbacks, and that the leaked decile predicate is gone."""
    code = _code_without_docstring(repo.peer_band)
    assert "PERCENT_RANK" not in code
    assert "pr >= 0.10" not in code
    assert "ABS(eur_price - med) <= :mad_k * 1.4826 * mad" in code
    assert "PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY abs_dev) OVER () AS mad" in code
    assert "row_n < :min_rows" in code
    assert "mad IS NULL OR mad <= 0" in code


def test_synthetic_debt_line_excluded_everywhere():
    """REVERT CAUGHT: dropping the synthetic 1С debt-entry product exclusion from cost / peer band
    (it contaminates both cost lots and realized price)."""
    assert ":synthetic" in inspect.getsource(repo.unit_cost_eur)
    assert ":synthetic" in inspect.getsource(repo.peer_band)


def test_elasticity_panel_uses_priceperitem_as_eur_not_fx_converted():
    """REVERT CAUGHT: wrapping PricePerItem in GetExchangedToEuroValue in the elasticity panels.
    PricePerItem is ALREADY EUR for every agreement currency (verified live: a UAH agreement sits
    in the same band as EUR, not ~52x higher); FX-wrapping would collapse non-EUR cells and poison
    the regression. Both the per-SKU and the pooled panel must read PricePerItem directly and
    exclude the synthetic 1С line."""
    for fn in (repo.product_panel, repo.group_panel):
        code = _code_without_docstring(fn)
        assert "oi.PricePerItem" in code
        assert "GetExchangedToEuroValue" not in code
        assert ":synthetic" in code
        assert "IsValidForCurrentSale = 1" in code
        assert "Deleted" not in code or "Deleted = 0" in code  # only ProductProductGroup may filter


def test_elastic_price_is_secondary_never_replaces_recommended_price():
    """REVERT CAUGHT: letting the elasticity feed the PRIMARY recommended_price. The backtest HELD
    this lever (~85% wrong-signed observational fits); the elastic price must stay an additive
    advisory field. Guards that build_recommendation computes recommended_price from the A+B
    clamp (recommended_price()) and only ever assigns the elastic value to elastic_optimal_price."""
    code = _code_without_docstring(engine.build_recommendation)
    assert "recommended_price(floor, p50, baseline)" in code
    assert "elastic_optimal_price=" in code
    assert "recommended_price=_round2(final_price)" in code
    assert "recommended_price=elastic_price" not in code
    assert "recommended_price=_round2(elastic_price)" not in code

    gate = _code_without_docstring(engine.elasticity_outputs)
    assert "is_sane_elasticity" in gate
