"""Budget-constrained 0/1 selection — exact MILP (CBC) with a greedy fallback.

Given each candidate's value (profit-at-risk) and cost (EUR line cost), pick the subset
that maximizes total value subject to the spend ceiling. Quantities are already MOQ/pack
order-ready upstream, so this is the optimal 0/1 knapsack over those lines.
"""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from app.core.logging import get_logger

log = get_logger("optimization")


def select_within_budget(values: list[float], costs: list[float], budget: float,
                         time_limit: int = 10) -> tuple[set[int], bool]:
    """Indices to include + whether the exact MILP produced them (False = greedy fallback),
    so the plan's method_used reflects what actually ran instead of hiding solver breakage."""
    n = len(values)
    if n == 0 or budget <= 0:
        return set(), True
    try:
        import pulp

        prob = pulp.LpProblem("budget_knapsack", pulp.LpMaximize)
        x = [pulp.LpVariable(f"x{i}", cat="Binary") for i in range(n)]
        prob += pulp.lpSum(values[i] * x[i] for i in range(n))
        # Prices are normally rounded line cents. Preserve higher-precision
        # callers too: convert costs to their exact common decimal unit, then
        # floor the budget in that unit (never round an insufficient budget up).
        exact_costs = [Decimal(str(c)) for c in costs]
        scale = max(0, max(-c.as_tuple().exponent for c in exact_costs))
        units = 10 ** scale
        cost_units = [int(Fraction(c) * units) for c in exact_costs]
        budget_units = Fraction(Decimal(str(budget))) * units
        budget_units = budget_units.numerator // budget_units.denominator
        # CBC uses doubles. Do not send integers it cannot represent exactly.
        if max([abs(budget_units), *(abs(c) for c in cost_units)]) > 2 ** 53:
            return _greedy(values, costs, budget), False
        prob += pulp.lpSum(cost_units[i] * x[i] for i in range(n)) <= budget_units
        status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit))
        if pulp.LpStatus[status] == "Optimal":
            chosen = {i for i in range(n) if (x[i].value() or 0) > 0.5}
            # A solver tolerance must never authorize spending over the exact budget.
            if sum((Fraction(exact_costs[i]) for i in chosen), Fraction()) <= Fraction(Decimal(str(budget))):
                return chosen, True
            log.warning("milp_budget_validation_failed")
            return _greedy(values, costs, budget), False
        log.warning("milp_non_optimal", status=pulp.LpStatus[status])
    except Exception as exc:  # noqa: BLE001
        log.warning("milp_failed_fallback_greedy", error=str(exc))
    return _greedy(values, costs, budget), False


def _greedy(values: list[float], costs: list[float], budget: float) -> set[int]:
    order = sorted(range(len(values)),
                   key=lambda i: (
                       -((values[i] / costs[i]) if costs[i] > 0 else 0.0),
                       i,
                   ))
    used = Fraction()
    exact_budget = Fraction(Decimal(str(budget)))
    chosen: set[int] = set()
    for i in order:
        exact_cost = Fraction(Decimal(str(costs[i])))
        if exact_cost > 0 and used + exact_cost <= exact_budget:
            chosen.add(i)
            used += exact_cost
    return chosen
