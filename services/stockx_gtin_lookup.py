"""Lookup StockX product variant details by UPC/GTIN without writing to Sheets."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import re

from services.sizes import normalize_size
from services.stockx import BASE_URL, get_json, get_product_details, stockx_product_url
from utils.stockx_api import ensure_valid_token
from utils.text import clean_text


@dataclass
class StockxGtinLookupResult:
    gtin: str
    product_id: str = ""
    variant_id: str = ""
    variant_name: str = ""
    variant_value: str = ""
    normalized_size: str = ""
    title: str = ""
    brand: str = ""
    style_id: str = ""
    product_type: str = ""
    stockx_url: str = ""
    is_flex_eligible: bool | None = None
    is_direct_eligible: bool | None = None
    gtins: list[dict] | None = None
    expected_size: str = ""
    expected_normalized_size: str = ""
    size_matches_expected: bool | None = None


def lookup_gtin(gtin: str, expected_size: str | None = None) -> StockxGtinLookupResult | None:
    normalized_gtin = normalize_gtin(gtin)
    if not normalized_gtin:
        raise ValueError("GTIN/UPC is required")

    variant = get_variant_by_gtin(normalized_gtin)
    if not variant:
        return None

    product = {}
    product_id = clean_text(variant.get("productId"))
    if product_id:
        product = get_product_details(product_id) or {}

    return build_lookup_result(
        gtin=normalized_gtin,
        variant=variant,
        product=product,
        expected_size=expected_size,
    )


def get_variant_by_gtin(gtin: str) -> dict | None:
    return get_json(
        f"{BASE_URL}/catalog/products/variants/gtins/{gtin}",
        f"Error fetching StockX variant for GTIN {gtin}",
    )


def build_lookup_result(
    gtin: str,
    variant: dict,
    product: dict | None = None,
    expected_size: str | None = None,
) -> StockxGtinLookupResult:
    product = product or {}
    variant_value = clean_text(variant.get("variantValue"))
    normalized_variant_size = normalize_size(variant_value)
    expected_size_text = clean_text(expected_size)
    expected_normalized_size = normalize_size(expected_size_text)
    size_matches_expected = None
    if expected_normalized_size:
        size_matches_expected = normalized_variant_size == expected_normalized_size

    return StockxGtinLookupResult(
        gtin=normalize_gtin(gtin),
        product_id=clean_text(variant.get("productId")),
        variant_id=clean_text(variant.get("variantId")),
        variant_name=clean_text(variant.get("variantName")),
        variant_value=variant_value,
        normalized_size=normalized_variant_size,
        title=clean_text(product.get("title")),
        brand=clean_text(product.get("brand")),
        style_id=clean_text(product.get("styleId")).upper(),
        product_type=clean_text(product.get("productType")),
        stockx_url=stockx_product_url(product, product.get("styleId") or ""),
        is_flex_eligible=variant.get("isFlexEligible"),
        is_direct_eligible=variant.get("isDirectEligible"),
        gtins=variant.get("gtins") or [],
        expected_size=expected_size_text,
        expected_normalized_size=expected_normalized_size,
        size_matches_expected=size_matches_expected,
    )


def format_lookup_result(result: StockxGtinLookupResult) -> str:
    lines = [
        "StockX GTIN Lookup",
        f"GTIN: {result.gtin}",
        f"Product: {result.title or 'N/A'}",
        f"Brand: {result.brand or 'N/A'}",
        f"Style Code: {result.style_id or 'N/A'}",
        f"Product Type: {result.product_type or 'N/A'}",
        f"Size: {result.variant_value or 'N/A'}",
        f"Normalized Size: {result.normalized_size or 'N/A'}",
        f"Variant: {result.variant_name or 'N/A'}",
        f"Product ID: {result.product_id or 'N/A'}",
        f"Variant ID: {result.variant_id or 'N/A'}",
        f"Flex Eligible: {format_bool(result.is_flex_eligible)}",
        f"Direct Eligible: {format_bool(result.is_direct_eligible)}",
        f"StockX URL: {result.stockx_url or 'N/A'}",
    ]

    if result.expected_normalized_size:
        match_text = "MATCH" if result.size_matches_expected else "MISMATCH"
        lines.append(
            f"Size Check: {match_text} "
            f"(expected {result.expected_size}, StockX returned {result.variant_value or 'N/A'})"
        )

    gtin_text = format_gtins(result.gtins or [])
    if gtin_text:
        lines.append(f"Known GTINs: {gtin_text}")

    return "\n".join(lines)


def format_gtins(gtins: list[dict]) -> str:
    values = []
    for gtin in gtins:
        identifier = clean_text(gtin.get("identifier"))
        gtin_type = clean_text(gtin.get("type"))
        if identifier and gtin_type:
            values.append(f"{gtin_type} {identifier}")
        elif identifier:
            values.append(identifier)
    return ", ".join(values)


def format_bool(value: bool | None) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    return "N/A"


def normalize_gtin(value) -> str:
    return re.sub(r"\s+", "", clean_text(value))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Lookup StockX product variant details by UPC/GTIN without writing to Sheets."
    )
    parser.add_argument("gtin", help="UPC/GTIN scanned from the product box")
    parser.add_argument(
        "--expected-size",
        help="Optional box size to compare against StockX's returned variant size",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON-shaped lookup output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ensure_valid_token()
    result = lookup_gtin(args.gtin, expected_size=args.expected_size)
    if result is None:
        return 1

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(format_lookup_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
