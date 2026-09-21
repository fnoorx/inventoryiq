"""StockX market-data enrichment and per-size profit checks."""

from concurrent.futures import ThreadPoolExecutor, as_completed

from services.pricing import best_payout, net_sale
from services.stockx import get_product_id, get_product_market_data
from utils.text import to_float

from .config import MIN_PROFIT_CAD, STOCKX_WORKERS
from .models import LaunchCheck, LaunchSizeMarket


def check_launch_products(products, stockx_workers=STOCKX_WORKERS):
    """Attach StockX market data and a profitability verdict to each launch product."""

    products = [product for product in products if product.style_code]
    market_by_style = fetch_market_data_for_styles(
        {product.style_code for product in products},
        max_workers=stockx_workers,
    )
    return [
        evaluate_product(product, market_by_style.get(product.style_code))
        for product in products
    ]


def fetch_market_data_for_styles(style_codes, max_workers=STOCKX_WORKERS):
    """Fetch StockX data once per unique style code."""

    style_codes = sorted(code for code in style_codes if code)
    if not style_codes:
        return {}

    market_by_style = {}
    # Threads overlap the waiting; services.stockx.stockx_get still throttles
    # and retries the actual HTTP calls.
    with ThreadPoolExecutor(max_workers=min(max_workers, len(style_codes))) as pool:
        futures = {
            pool.submit(fetch_market_data_for_style, style_code): style_code
            for style_code in style_codes
        }
        for future in as_completed(futures):
            style_code = futures[future]
            try:
                market_by_style[style_code] = future.result()
            except Exception as exc:
                print(f"StockX lookup failed for {style_code}: {exc}")
                market_by_style[style_code] = None
    return market_by_style


def fetch_market_data_for_style(style_code):
    product_id = get_product_id(style_code)
    return get_product_market_data(product_id) if product_id else None


def evaluate_product(product, market_data):
    """Check every available size and keep the most profitable one as the verdict."""

    size_checks = [
        evaluate_size(size, market_for_size(market_data, size), product.real_price)
        for size in product.available_sizes
    ]
    best = max(
        (check for check in size_checks if check.profit is not None),
        key=lambda check: check.profit,
        default=None,
    )
    return LaunchCheck(
        product=product,
        market_data=market_data,
        size_checks=size_checks,
        best_size=best.size if best else None,
        best_sale=best.avg_sales if best else None,
        best_net_sale=best.net_sales if best else None,
        best_bid=best.highest_bid if best else None,
        best_net_bid=best.net_highest_bid if best else None,
        best_profit_source=best.profit_source if best else None,
        estimated_profit=best.profit if best else None,
        worth_buying=bool(best and best.profit >= MIN_PROFIT_CAD),
    )


def evaluate_size(size, market, real_price):
    sale = to_float((market or {}).get("avg_sales"))
    highest_bid = to_float((market or {}).get("highest_bid"))
    # StockX prices are gross; profit is measured on estimated seller proceeds.
    net_sales = net_sale(sale)
    net_bid = net_sale(highest_bid)
    avg_sale_profit = profit_after_cost(net_sales, real_price)
    highest_bid_profit = profit_after_cost(net_bid, real_price)
    profit_source, profit = best_payout(avg_sale_profit, highest_bid_profit)
    net_sales_above = bool(avg_sale_profit is not None and avg_sale_profit > 0)
    net_bid_above = bool(highest_bid_profit is not None and highest_bid_profit > 0)
    return LaunchSizeMarket(
        size=size,
        market_data=market,
        avg_sales=sale,
        net_sales=net_sales,
        highest_bid=highest_bid,
        net_highest_bid=net_bid,
        avg_sale_profit=avg_sale_profit,
        highest_bid_profit=highest_bid_profit,
        profit=profit,
        profit_source=profit_source or None,
        net_sales_above_real_price=net_sales_above,
        net_highest_bid_above_real_price=net_bid_above,
        # Broader than worth_buying: the Discord command shows any size that
        # clears cost on either signal.
        worth_highlighting=net_sales_above or net_bid_above,
    )


def profit_after_cost(payout, real_price):
    if payout is None or real_price is None:
        return None
    return round(payout - real_price, 2)


def market_for_size(market_data, size):
    """Normalize a few common StockX size key formats."""

    if not market_data:
        return None
    for candidate in (size, f"M {size}", f"{size}M", f"{size}.0"):
        if candidate in market_data:
            return market_data[candidate]
    return None
