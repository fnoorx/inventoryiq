"""Evaluate the saved catalogue against current StockX market data."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable

import requests

from services.pricing import best_payout, calculate_total_cost, net_sale
from services.scrape_catalogue import SNAPSHOT_PATH, load_snapshot
from services.stockx import (
    get_product_details,
    get_product_id,
    get_product_market_data,
    market_style_codes_match,
    stockx_product_url,
)
from utils.text import to_float


MIN_PROFIT_CAD = 30.0
MONEY_RE = re.compile(r"-?\d[\d,]*(?:\.\d{1,2})?")


@dataclass(frozen=True)
class CatalogueProduct:
    product: str
    link: str
    style_code: str
    listed_price: float


@dataclass(frozen=True)
class CatalogueProfitCandidate:
    product: str
    link: str
    style_code: str
    listed_price: float
    discounted_price: float
    best_size: str
    avg_sale: float | None
    net_avg_sale: float | None
    highest_bid: float | None
    net_highest_bid: float | None
    estimated_profit: float
    profit_source: str
    stockx_link: str = ""


@dataclass
class CatalogueCheckResult:
    discount_percent: float
    scanned_style_codes: int = 0
    skipped_missing_price: int = 0
    unresolved_style_codes: list[str] = field(default_factory=list)
    stockx_error_style_codes: list[str] = field(default_factory=list)
    candidates: list[CatalogueProfitCandidate] = field(default_factory=list)


def load_catalogue_products(snapshot_path=SNAPSHOT_PATH) -> list[dict]:
    """Return every product in the last completed catalogue scrape."""

    return list(load_snapshot(snapshot_path).values())


def parse_catalogue_price(value) -> float | None:
    """Parse displayed CAD price text such as ``$120.00`` safely."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        price = float(value)
        return price if price >= 0 else None

    match = MONEY_RE.search(str(value))
    if not match:
        return None
    try:
        price = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return price if price >= 0 else None


def check_catalogue(
    discount_percent: float,
    *,
    products: Iterable[dict] | None = None,
    minimum_profit: float = MIN_PROFIT_CAD,
) -> CatalogueCheckResult:
    """Return saved catalogue styles estimated to exceed ``minimum_profit``.

    A catalogue tile has no reliable per-size availability, so each style is
    represented by its most profitable current StockX variant. The same style
    code is fetched once even when it appears on more than one catalogue tile.
    """

    discount = validate_discount_percent(discount_percent)
    result = CatalogueCheckResult(discount_percent=discount)
    product_rows = products if products is not None else load_catalogue_products()
    products_by_style = group_products_by_style(product_rows, result)
    result.scanned_style_codes = len(products_by_style)

    for style_code, entries in sorted(products_by_style.items()):
        try:
            product_id = get_product_id(style_code)
            if not product_id:
                result.unresolved_style_codes.append(style_code)
                continue

            stockx_product = get_product_details(product_id)
            returned_style_code = str((stockx_product or {}).get("styleId") or "").strip()
            if not returned_style_code or not market_style_codes_match(
                style_code,
                returned_style_code,
            ):
                result.unresolved_style_codes.append(style_code)
                continue

            stockx_link = stockx_product_url(stockx_product, style_code)

            market_data = get_product_market_data(product_id)
            if not market_data:
                result.unresolved_style_codes.append(style_code)
                continue
        except requests.RequestException as exc:
            print(f"Skipping catalogue style {style_code} after StockX request error: {exc}")
            result.stockx_error_style_codes.append(style_code)
            continue

        candidates = [
            evaluate_catalogue_product(
                entry,
                market_data,
                discount,
                stockx_link=stockx_link,
                minimum_profit=minimum_profit,
            )
            for entry in entries
        ]
        candidates = [candidate for candidate in candidates if candidate is not None]
        if candidates:
            # One StockX style can appear more than once in the catalogue.
            # Present the lowest-cost/highest-profit available tile once.
            result.candidates.append(
                max(candidates, key=lambda candidate: candidate.estimated_profit)
            )

    result.candidates.sort(
        key=lambda candidate: (-candidate.estimated_profit, candidate.product.casefold())
    )
    return result


def validate_discount_percent(value) -> float:
    try:
        discount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Discount must be a number from 0 up to, but not including, 100.") from exc
    if discount < 0 or discount >= 100:
        raise ValueError("Discount must be a number from 0 up to, but not including, 100.")
    return discount


def group_products_by_style(
    products: Iterable[dict],
    result: CatalogueCheckResult,
) -> dict[str, list[CatalogueProduct]]:
    grouped: dict[str, list[CatalogueProduct]] = {}
    seen: set[tuple[str, str]] = set()

    for product in products:
        listed_price = parse_catalogue_price(product.get("price"))
        style_codes = product.get("style_codes") or []
        if not style_codes or listed_price is None:
            result.skipped_missing_price += 1
            continue

        name = str(product.get("product") or "Unknown product").strip()
        link = str(product.get("link") or "").strip()
        for raw_style_code in style_codes:
            style_code = str(raw_style_code or "").strip().upper()
            key = (style_code, link)
            if not style_code or key in seen:
                continue
            seen.add(key)
            grouped.setdefault(style_code, []).append(
                CatalogueProduct(
                    product=name,
                    link=link,
                    style_code=style_code,
                    listed_price=listed_price,
                )
            )
    return grouped


def evaluate_catalogue_product(
    product: CatalogueProduct,
    market_data: dict,
    discount_percent: float,
    stockx_link: str = "",
    minimum_profit: float = MIN_PROFIT_CAD,
) -> CatalogueProfitCandidate | None:
    discounted_price = calculate_total_cost(product.listed_price, discount_percent)
    best: CatalogueProfitCandidate | None = None

    for size, values in market_data.items():
        avg_sale = to_float((values or {}).get("avg_sales"))
        highest_bid = to_float((values or {}).get("highest_bid"))
        net_avg_sale = net_sale(avg_sale)
        net_highest_bid = net_sale(highest_bid)
        profit_source, projected_payout = best_payout(net_avg_sale, net_highest_bid)
        if projected_payout is None:
            continue

        estimated_profit = round(projected_payout - discounted_price, 2)
        if estimated_profit <= minimum_profit:
            continue

        candidate = CatalogueProfitCandidate(
            product=product.product,
            link=product.link,
            style_code=product.style_code,
            listed_price=product.listed_price,
            discounted_price=discounted_price,
            best_size=str(size),
            avg_sale=avg_sale,
            net_avg_sale=net_avg_sale,
            highest_bid=highest_bid,
            net_highest_bid=net_highest_bid,
            estimated_profit=estimated_profit,
            profit_source=profit_source,
            stockx_link=stockx_link,
        )
        if best is None or candidate.estimated_profit > best.estimated_profit:
            best = candidate

    return best
