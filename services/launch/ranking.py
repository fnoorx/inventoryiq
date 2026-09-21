"""Launch-candidate selection, ROI ranking, and budget knapsack logic."""

from services.pricing import calculate_roi

from .models import LaunchCandidate, LaunchRankingResult


def build_launch_candidates(checks, profitable_only=True):
    """Collapse each checked product to its single best size.

    One candidate per product keeps the ranking and knapsack from recommending
    the same shoe several times just because several sizes are available.
    """

    candidates = []
    for check in checks:
        size_check = best_size_check(check.size_checks, profitable_only=profitable_only)
        if size_check is None:
            continue

        candidates.append(
            LaunchCandidate(
                product=check.product,
                size_check=size_check,
                roi=calculate_roi(size_check.profit, check.product.real_price),
            )
        )

    return candidates


def best_size_check(size_checks, profitable_only=True):
    """Return the most profitable size for one product."""

    eligible = [
        size_check
        for size_check in size_checks
        if size_check.profit is not None
        and (not profitable_only or size_check.worth_highlighting)
    ]
    if not eligible:
        return None

    return max(eligible, key=lambda size_check: size_check.profit)


def rank_candidates(candidates, budget=None):
    """Rank by ROI, or choose the best 0/1 knapsack combination within a budget."""

    candidates = list(candidates)
    if budget is None:
        return rank_candidates_by_roi(candidates)
    return rank_candidates_by_budget(candidates, budget)


def rank_candidates_by_roi(candidates):
    """Sort candidates by return on real checkout cost, highest first."""

    valid_candidates = []

    for candidate in candidates:
        roi = calculate_roi(candidate.size_check.profit, candidate.product.real_price)
        if roi is not None:
            candidate.roi = roi
            valid_candidates.append(candidate)

    valid_candidates.sort(key=lambda c: c.roi, reverse=True)

    return build_ranking_result(mode="roi", budget=None, candidates=valid_candidates)


def rank_candidates_by_budget(candidates, budget):
    """0/1 knapsack over one candidate per product, in integer cents."""

    candidates = list(candidates)
    budget_cents = money_to_cents(budget)
    if budget_cents is None or budget_cents <= 0:
        return build_ranking_result(mode="budget", budget=budget, candidates=[])

    items = []
    total_cost_cents = 0
    for candidate in candidates:
        cost_cents = money_to_cents(candidate.product.real_price)
        profit_cents = money_to_cents(candidate.size_check.profit)
        if (
            cost_cents is None
            or profit_cents is None
            or cost_cents <= 0
            or profit_cents <= 0
            or cost_cents > budget_cents
        ):
            continue
        total_cost_cents += cost_cents
        items.append((candidate, cost_cents, profit_cents))

    if not items:
        return build_ranking_result(mode="budget", budget=budget, candidates=[])

    max_capacity = min(budget_cents, total_cost_cents)
    dp = [0] * (max_capacity + 1)
    choices = []

    for _, cost_cents, profit_cents in items:
        choice = bytearray(max_capacity + 1)
        for capacity in range(max_capacity, cost_cents - 1, -1):
            candidate_profit = dp[capacity - cost_cents] + profit_cents
            if candidate_profit > dp[capacity]:
                dp[capacity] = candidate_profit
                choice[capacity] = 1
        choices.append(choice)

    capacity = max_capacity
    selected_candidates = []
    for item_index in range(len(items) - 1, -1, -1):
        if choices[item_index][capacity]:
            candidate, cost_cents, _ = items[item_index]
            selected_candidates.append(candidate)
            capacity -= cost_cents

    selected_candidates.reverse()
    return build_ranking_result(
        mode="budget",
        budget=budget,
        candidates=selected_candidates,
    )


def build_ranking_result(mode, budget, candidates):
    """Package ranked/chosen candidates with useful totals."""

    candidates = list(candidates)
    return LaunchRankingResult(
        mode=mode,
        budget=budget,
        candidates=candidates,
        total_real_price=round(
            sum(candidate.product.real_price or 0 for candidate in candidates),
            2,
        ),
        total_profit=round(
            sum(candidate.size_check.profit or 0 for candidate in candidates),
            2,
        ),
    )


def money_to_cents(value):
    """Convert a float dollar value to integer cents for DP table indexes."""

    if value is None:
        return None
    return int(round(value * 100))
