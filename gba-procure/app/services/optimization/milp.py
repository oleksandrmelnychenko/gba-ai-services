"""Budget-constrained 0/1 selection — exact MILP (CBC) with a greedy fallback.

Given each candidate's value (profit-at-risk) and cost (EUR line cost), pick the subset
that maximizes total value subject to the spend ceiling. Quantities are already MOQ/pack
order-ready upstream, so this is the optimal 0/1 knapsack over those lines.
"""
from __future__ import annotations

from app.core.logging import get_logger

log = get_logger("optimization")


def select_within_budget(values: list[float], costs: list[float], budget: float,
                         time_limit: int = 10) -> set[int]:
    """Indices to include. Exact CBC MILP; falls back to greedy value-density on any solver issue."""
    n = len(values)
    if n == 0 or budget <= 0:
        return set()
    try:
        import pulp

        prob = pulp.LpProblem("budget_knapsack", pulp.LpMaximize)
        x = [pulp.LpVariable(f"x{i}", cat="Binary") for i in range(n)]
        prob += pulp.lpSum(values[i] * x[i] for i in range(n))
        prob += pulp.lpSum(costs[i] * x[i] for i in range(n)) <= budget
        status = prob.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit))
        if pulp.LpStatus[status] in ("Optimal", "Not Solved"):
            chosen = {i for i in range(n) if (x[i].value() or 0) > 0.5}
            if chosen or pulp.LpStatus[status] == "Optimal":
                return chosen
        log.warning("milp_non_optimal", status=pulp.LpStatus[status])
    except Exception as exc:  # noqa: BLE001
        log.warning("milp_failed_fallback_greedy", error=str(exc))
    return _greedy(values, costs, budget)


def _greedy(values: list[float], costs: list[float], budget: float) -> set[int]:
    order = sorted(range(len(values)),
                   key=lambda i: (values[i] / costs[i]) if costs[i] > 0 else 0.0, reverse=True)
    used = 0.0
    chosen: set[int] = set()
    for i in order:
        if costs[i] > 0 and used + costs[i] <= budget:
            chosen.add(i)
            used += costs[i]
    return chosen
