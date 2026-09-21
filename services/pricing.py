"""Purchase-cost and resale-payout assumptions, configurable through the environment."""

from decimal import Decimal, ROUND_HALF_UP
import os

from utils.text import to_float

# StockX average sale is gross. Net sale estimates what the seller keeps after fees.
STOCKX_NET_RATE = 0.89


def purchase_discount_percent() -> Decimal:
    discount = Decimal(os.getenv("PURCHASE_DISCOUNT_PERCENT", "0"))
    if not discount.is_finite() or not 0 <= discount <= 100:
        raise ValueError("PURCHASE_DISCOUNT_PERCENT must be between 0 and 100")
    return discount


def purchase_tax_multiplier() -> Decimal:
    tax = Decimal(os.getenv("PURCHASE_TAX_PERCENT", "13"))
    if not tax.is_finite() or tax < 0:
        raise ValueError("PURCHASE_TAX_PERCENT must be non-negative")
    return 1 + tax / 100


def round_money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_total_cost(price_paid: float, discount_percent: float = 0) -> float:
    """Checkout cost of a purchase after an optional discount and sales tax."""

    discount = 1 - Decimal(str(discount_percent)) / 100
    return round_money(Decimal(str(price_paid)) * discount * purchase_tax_multiplier())


def calculate_real_price(price: float | None) -> float | None:
    """Estimated checkout cost using the configured discount and tax."""

    if price is None:
        return None
    return calculate_total_cost(price, purchase_discount_percent())


def net_sale(gross: float | None) -> float | None:
    """Estimated seller proceeds after StockX fees."""

    amount = to_float(gross)
    return None if amount is None else round(amount * STOCKX_NET_RATE, 2)


def best_payout(avg_sale_payout: float | None, highest_bid_payout: float | None) -> tuple[str, float | None]:
    """Return the better projected payout and which market signal produced it."""

    options = [("avg sale", avg_sale_payout), ("highest bid", highest_bid_payout)]
    options = [(source, payout) for source, payout in options if payout is not None]
    if not options:
        return "", None
    return max(options, key=lambda option: option[1])


def calculate_roi(profit: float | None, cost: float | None) -> float | None:
    if profit is None or not cost:
        return None
    return round(profit / cost, 4)
