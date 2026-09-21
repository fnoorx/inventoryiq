"""Data models shared across the launch scraper pipeline."""

from dataclasses import dataclass


@dataclass
class LaunchProduct:
    """Launch product data after filtering to sizes that are actually available."""

    title: str
    subtitle: str | None
    url: str
    product_id: str
    style_code: str | None
    price: float | None
    real_price: float | None
    currency: str | None
    image_url: str | None
    product_type: str | None
    launch_status: str | None
    available_sizes: list[str]
    available_size_details: list[dict]


@dataclass
class LaunchSizeMarket:
    """StockX market result for one available launch size."""

    size: str
    market_data: dict | None
    avg_sales: float | None
    net_sales: float | None
    highest_bid: float | None
    net_highest_bid: float | None
    avg_sale_profit: float | None
    highest_bid_profit: float | None
    profit: float | None
    profit_source: str | None
    net_sales_above_real_price: bool
    net_highest_bid_above_real_price: bool
    worth_highlighting: bool


@dataclass
class LaunchCheck:
    """Final result after combining launch availability with StockX market data."""

    product: LaunchProduct
    market_data: dict | None
    size_checks: list[LaunchSizeMarket]
    best_size: str | None
    best_sale: float | None
    best_net_sale: float | None
    best_bid: float | None
    best_net_bid: float | None
    best_profit_source: str | None
    estimated_profit: float | None
    worth_buying: bool


@dataclass
class LaunchCandidate:
    """One product with its single most profitable size, ready for ranking."""

    product: LaunchProduct
    size_check: LaunchSizeMarket
    roi: float | None


@dataclass
class LaunchRankingResult:
    """Candidates ordered by ROI, or the knapsack selection for a budget."""

    mode: str
    budget: float | None
    candidates: list[LaunchCandidate]
    total_real_price: float
    total_profit: float
