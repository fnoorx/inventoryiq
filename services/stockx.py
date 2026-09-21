"""Throttled StockX catalog and market-data client."""

from dataclasses import dataclass
import os
import re
import threading
import time
from urllib.parse import quote_plus

import requests

from services.sizes import normalize_size
from utils.performance import record_timing
from utils.stockx_api import get_valid_access_token
from utils.text import to_float

BASE_URL = "https://api.stockx.com/v2"
REQUEST_TIMEOUT_SECONDS = 20

# StockX rate-limits bursty traffic. Multiple bot commands and launch scans can
# share this module, so all StockX HTTP calls go through one process-wide gate.
STOCKX_MIN_REQUEST_INTERVAL_SECONDS = 1.0
STOCKX_MAX_RATE_LIMIT_RETRIES = 1
STOCKX_MAX_AUTH_RETRIES = 1
# Network timeouts are transient more often than they are permanent. Retry a
# request twice (three attempts total) before letting the caller decide how to
# handle that specific product.
STOCKX_MAX_NETWORK_RETRIES = 2
STOCKX_NETWORK_RETRY_DELAYS_SECONDS = (3.0, 8.0)

_stockx_request_lock = threading.Lock()
_next_stockx_request_at = 0


@dataclass(frozen=True)
class StockxMarketDetails:
    product_name: str
    style_code: str
    size: str | None
    market_data: dict


@dataclass(frozen=True)
class StockxVariantMatch:
    variant_id: str
    variant_value: str
    normalized_size: str


def get_headers():
    jwt = get_valid_access_token()
    if not jwt:
        raise RuntimeError("StockX access token is not loaded. Run bot startup auth first.")
    return {"Authorization": f"Bearer {jwt}", "x-api-key": os.getenv("STOCKX_API_KEY")}


def stockx_get(url, **kwargs):
    """Make one throttled StockX GET request.

    The lock spaces requests globally, even when caller code uses worker
    threads. Without this, parallel launch market checks can trigger 429s.
    """

    kwargs.setdefault("timeout", REQUEST_TIMEOUT_SECONDS)
    rate_limit_retries = 0
    auth_retries = 0
    network_retries = 0
    attempts = 0
    started_at = time.perf_counter()

    while True:
        wait_for_stockx_slot()
        attempts += 1
        try:
            response = requests.get(url, **kwargs)
        except requests.RequestException as exc:
            if network_retries < STOCKX_MAX_NETWORK_RETRIES:
                delay = STOCKX_NETWORK_RETRY_DELAYS_SECONDS[network_retries]
                network_retries += 1
                time.sleep(delay)
                continue
            record_timing(
                "stockx_get",
                started_at,
                status="network_error",
                attempts=attempts,
                auth_retries=auth_retries,
                rate_limit_retries=rate_limit_retries,
                network_retries=network_retries,
                error_type=type(exc).__name__,
            )
            raise

        if response.status_code == 401 and auth_retries < STOCKX_MAX_AUTH_RETRIES:
            rejected_token = bearer_token_from_headers(kwargs.get("headers"))
            refreshed_token = get_valid_access_token(rejected_token=rejected_token)
            if refreshed_token and refreshed_token != rejected_token:
                headers = dict(kwargs.get("headers") or {})
                headers["Authorization"] = f"Bearer {refreshed_token}"
                kwargs["headers"] = headers
                auth_retries += 1
                continue

        if response.status_code == 429 and rate_limit_retries < STOCKX_MAX_RATE_LIMIT_RETRIES:
            # Retry-After is an additional server-directed delay. The retry
            # still passes through the process-wide request-start gate.
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if retry_after:
                time.sleep(retry_after)
            rate_limit_retries += 1
            continue

        record_timing(
            "stockx_get",
            started_at,
            status=response.status_code,
            attempts=attempts,
            auth_retries=auth_retries,
            rate_limit_retries=rate_limit_retries,
            network_retries=network_retries,
        )
        return response


def wait_for_stockx_slot() -> None:
    global _next_stockx_request_at

    with _stockx_request_lock:
        now = time.monotonic()
        if now < _next_stockx_request_at:
            time.sleep(_next_stockx_request_at - now)
        _next_stockx_request_at = time.monotonic() + STOCKX_MIN_REQUEST_INTERVAL_SECONDS


def bearer_token_from_headers(headers) -> str | None:
    authorization = str((headers or {}).get("Authorization") or "").strip()
    prefix = "Bearer "
    if authorization.startswith(prefix):
        return authorization[len(prefix):].strip() or None
    return None


def get_json(url, message, **kwargs):
    """GET a StockX endpoint and return its JSON, or None after logging the failure."""

    response = stockx_get(url, headers=get_headers(), **kwargs)
    if response.status_code == 200:
        return response.json()
    print_stockx_error(message, response)
    return None


def get_product_id(style_code):
    if not split_market_style_codes(style_code):
        return None

    data = get_json(
        f"{BASE_URL}/catalog/search",
        "Error fetching product Id",
        params={"query": style_code, "pageNumber": 1, "pageSize": 5},
    )
    for product in (data or {}).get("products") or []:
        if not market_style_codes_match(style_code, product.get("styleId")):
            continue
        product_id = str(product.get("productId") or "").strip()
        if product_id:
            return product_id
    return None


def normalize_market_style_member(value) -> str:
    """Normalize harmless style-code separators without allowing fuzzy matches."""

    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def split_market_style_codes(value) -> tuple[str, ...]:
    """Return normalized members of a slash-delimited StockX style value."""

    return tuple(
        normalized
        for part in str(value or "").split("/")
        if (normalized := normalize_market_style_member(part))
    )


def market_style_codes_match(requested, returned) -> bool:
    """Validate a query against StockX's single or combined returned style."""

    requested_codes = set(split_market_style_codes(requested))
    returned_codes = set(split_market_style_codes(returned))
    if not requested_codes or not returned_codes:
        return False

    # A single style may belong to a StockX product represented by multiple
    # slash-delimited styles. A combined query must represent the same full set
    # so related search results cannot pass through on only one shared code.
    if len(requested_codes) == 1:
        return requested_codes.issubset(returned_codes)
    return requested_codes == returned_codes


def stockx_product_url(product: dict | None, fallback_style_code: str = "") -> str:
    """Return a direct StockX URL when available, otherwise an exact-style search."""

    url_key = str((product or {}).get("urlKey") or "").strip().strip("/")
    if url_key:
        return f"https://stockx.com/{url_key}"
    style_code = str(fallback_style_code or "").strip()
    if style_code:
        return f"https://stockx.com/search?s={quote_plus(style_code)}"
    return ""


def get_variant_id(product_id, size):
    # Inventory units store normalized sizes, while StockX may return values
    # such as "M 10.0" or "US 10". Compare normalized representations.
    target_size = normalize_size(size)
    matches = [
        variant
        for variant in get_product_variants(product_id) or []
        if normalize_size(variant.get("variantValue")) == target_size
    ]
    if len(matches) == 1:
        return matches[0].get("variantId")
    if len(matches) > 1:
        print(f"Multiple size {size} variants found for product {product_id}")
        return None
    print(f"Size {size} not found for product {product_id}")
    return None


def get_product_name(product_id):
    data = get_product_details(product_id)
    if data is not None:
        return data["title"], data["productType"]
    return None


def get_product_details(product_id):
    return get_json(f"{BASE_URL}/catalog/products/{product_id}", "Error fetching product details")


def get_product_variants(product_id):
    data = get_json(
        f"{BASE_URL}/catalog/products/{product_id}/variants",
        "Error fetching product variants",
    )
    if isinstance(data, dict):
        return data.get("variants") or []
    return data


def find_variant_matches(product_id: str, sizes: list[str]) -> list[StockxVariantMatch]:
    """Return every unique StockX variant matching any normalized visible US size."""

    targets = {normalize_size(size) for size in sizes if normalize_size(size)}
    matches = []
    seen_ids = set()
    for variant in get_product_variants(product_id) or []:
        normalized = normalize_size(variant.get("variantValue"))
        variant_id = str(variant.get("variantId") or "").strip()
        if normalized not in targets or not variant_id or variant_id in seen_ids:
            continue
        seen_ids.add(variant_id)
        matches.append(
            StockxVariantMatch(
                variant_id=variant_id,
                variant_value=str(variant.get("variantValue") or "").strip(),
                normalized_size=normalized,
            )
        )
    return matches


def get_market_data(style_code, size: str = None):
    # StockX market endpoints use product/variant IDs, not retailer style codes.
    product_id = get_product_id(style_code)
    if not product_id:
        return None
    return get_market_data_for_product(product_id, size)


def get_market_data_details(
    style_code: str,
    size: str | None = None,
    product_id: str | None = None,
    product_name: str | None = None,
    variant_id: str | None = None,
) -> StockxMarketDetails | None:
    """Return market values plus the product identity used for display."""

    resolved_product_id = product_id or get_product_id(style_code)
    if not resolved_product_id:
        return None

    # Search can return a related product when it has no exact result, and an
    # inventory unit can hold a stale product ID. Revalidate the resolved
    # product before using its identity or market values.
    product = get_product_details(resolved_product_id)
    if product is None:
        return None

    returned_style_code = str(product.get("styleId") or "").strip()
    if not returned_style_code or not market_style_codes_match(style_code, returned_style_code):
        return None

    resolved_product_name = str(product.get("title") or product_name or "").strip()
    if not resolved_product_name:
        return None

    resolved_variant_id = str(variant_id or "").strip()
    if size and resolved_variant_id:
        market_data = get_product_market_data(resolved_product_id, resolved_variant_id)
        if market_data is None:
            # A persisted StockX variant can be re-keyed. Resolve it again by
            # the immutable unit's stored size rather than failing permanently.
            market_data = get_market_data_for_product(resolved_product_id, size)
    else:
        market_data = get_market_data_for_product(resolved_product_id, size)
    if market_data is None:
        return None
    return StockxMarketDetails(
        product_name=resolved_product_name,
        style_code=returned_style_code.upper(),
        size=size,
        market_data=market_data,
    )


def get_market_data_for_product(product_id: str, size: str | None = None):
    variant_id = None
    if size:
        variant_id = get_variant_id(product_id, size)
        if not variant_id:
            return None
    return get_product_market_data(product_id, variant_id)


def get_product_market_data(product_id, variant_id=None):
    params = {"currencyCode": "CAD"}
    if variant_id:
        # One variant is cheaper when the caller asks for a specific size.
        data = get_json(
            f"{BASE_URL}/catalog/products/{product_id}/variants/{variant_id}/market-data",
            "Error fetching variant market data",
            params=params,
        )
        return market_snapshot(data) if data is not None else None

    # Product market data is keyed by variant ID, so fetch the variants first to
    # translate those IDs into human-readable sizes for later matching.
    size_by_variant = {
        variant["variantId"]: variant["variantValue"]
        for variant in get_product_variants(product_id) or []
    }
    data = get_json(
        f"{BASE_URL}/catalog/products/{product_id}/market-data",
        "Error fetching product market data",
        params=params,
    )
    if data is None:
        return None
    return {
        size_by_variant.get(variant["variantId"], variant["variantId"]): market_snapshot(variant)
        for variant in data
    }


def market_snapshot(data: dict) -> dict:
    return {
        "avg_sales": to_float(data["sellFasterAmount"]),
        "highest_bid": to_float(data["highestBidAmount"]),
        "lowest_ask": to_float(data["lowestAskAmount"]),
        "flex_lowest_ask": to_float(data["flexLowestAskAmount"]),
        "beat_US": to_float(data["flexMarketData"]["beatUS"]),
    }


def print_stockx_error(message, response):
    """Print enough error detail to debug without dumping full HTML pages."""

    body = (response.text or "").replace("\n", " ").strip()
    if len(body) > 240:
        body = body[:237] + "..."
    print(f"{message}: {response.status_code} - {body}")


def parse_retry_after(value):
    """Return StockX retry delay, defaulting when the header is missing."""

    try:
        return max(float(value), 0)
    except (TypeError, ValueError):
        return 5
