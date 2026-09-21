"""Launch-page scraper: availability first, then StockX profitability, then ranking."""

from .brand import fetch_launch_products
from .market import check_launch_products
from .ranking import build_launch_candidates, rank_candidates
from .config import TARGET_SIZES


def scrape_launch(target_sizes=TARGET_SIZES):
    products = fetch_launch_products(target_sizes=target_sizes)
    return check_launch_products(products)


def scrape_launch_candidates(target_sizes=TARGET_SIZES, profitable_only=True):
    checks = scrape_launch(target_sizes=target_sizes)
    return build_launch_candidates(checks, profitable_only=profitable_only)


__all__ = [
    "build_launch_candidates",
    "check_launch_products",
    "fetch_launch_products",
    "rank_candidates",
    "scrape_launch",
    "scrape_launch_candidates",
]
