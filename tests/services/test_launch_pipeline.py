"""End-to-end launch pipeline on in-memory data: market check -> candidates -> ranking."""

from services.launch import market, ranking
from services.launch.models import LaunchProduct


def product(product_id, style_code, real_price, sizes=("10",)):
    return LaunchProduct(
        title=f"Demo {product_id}",
        subtitle=None,
        url=f"https://brand.example.test/ca/launch/t/{product_id}",
        product_id=product_id,
        style_code=style_code,
        price=real_price,
        real_price=real_price,
        currency="CAD",
        image_url=None,
        product_type="FOOTWEAR",
        launch_status="ACTIVE",
        available_sizes=list(sizes),
        available_size_details=[{"size": size} for size in sizes],
    )


def test_market_check_candidates_and_ranking_run_end_to_end(monkeypatch):
    market_by_style = {
        "DEMO-001": {"10": {"avg_sales": 300.0, "highest_bid": 250.0}},  # profitable
        "DEMO-002": {"10": {"avg_sales": 120.0, "highest_bid": 100.0}},  # loses money
        "DEMO-003": {"10": {"avg_sales": 500.0, "highest_bid": 450.0}},  # most profitable
    }
    monkeypatch.setattr(
        market, "fetch_market_data_for_styles", lambda codes, max_workers=3: market_by_style
    )
    products = [
        product("p1", "DEMO-001", real_price=150.0),
        product("p2", "DEMO-002", real_price=150.0),
        product("p3", "DEMO-003", real_price=300.0),
    ]

    checks = market.check_launch_products(products)
    assert [check.worth_buying for check in checks] == [True, False, True]

    candidates = ranking.build_launch_candidates(checks, profitable_only=True)
    assert [candidate.product.product_id for candidate in candidates] == ["p1", "p3"]
    assert all(candidate.roi is not None for candidate in candidates)

    by_roi = ranking.rank_candidates(candidates)
    assert [c.product.product_id for c in by_roi.candidates] == ["p1", "p3"]

    within_budget = ranking.rank_candidates(candidates, budget=320)
    assert [c.product.product_id for c in within_budget.candidates] == ["p3"]
    assert within_budget.total_real_price == 300.0
