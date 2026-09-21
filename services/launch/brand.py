"""Brand launch-page fetching and product parsing."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import re

import requests

from .config import (
    BRAND_BASE_URL,
    BRAND_WORKERS,
    REQUEST_TIMEOUT_SECONDS,
    LAUNCH_IN_STOCK_URL,
    TARGET_SIZES,
)
from .models import LaunchProduct
from services.pricing import calculate_real_price
from utils.performance import timed
from utils.text import to_float


# Next.js embeds page state inside this script. That state includes products,
# price, style code, SKU IDs, and size availability.
NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL,
)


def fetch_launch_products(url=LAUNCH_IN_STOCK_URL, target_sizes=TARGET_SIZES):
    """Return launch products with at least one target size available."""

    # The in-stock page is only used as an index. Detail pages have more
    # reliable SKU-level availability, so those are fetched next.
    urls = fetch_launch_product_urls(url)
    return fetch_product_details(urls, target_sizes=set(target_sizes))


def fetch_html(url):
    """Fetch a launch page as normal HTML."""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def extract_initial_state(html_text):
    """Pull and decode the brand's embedded Next.js initialState JSON."""

    match = NEXT_DATA_RE.search(html_text)
    if not match:
        raise ValueError("Launch page __NEXT_DATA__ payload was not found.")

    next_data = json.loads(html.unescape(match.group(1)))
    initial_state = next_data.get("props", {}).get("pageProps", {}).get("initialState")
    if not initial_state:
        raise ValueError("Launch page initialState payload was not found.")

    return json.loads(initial_state)


def fetch_launch_product_urls(url=LAUNCH_IN_STOCK_URL):
    """Use the in-stock page as a lightweight index of product detail pages."""

    state = extract_initial_state(fetch_html(url))
    threads = (
        state.get("product", {})
        .get("threads", {})
        .get("data", {})
        .get("items", {})
    )

    urls = []
    seen = set()
    for thread in threads.values():
        slug = thread.get("seo", {}).get("slug")
        if not slug:
            continue

        product_url = f"{BRAND_BASE_URL}/ca/launch/t/{slug}"
        if product_url not in seen:
            seen.add(product_url)
            urls.append(product_url)

    return urls


def fetch_product_details(urls, target_sizes=TARGET_SIZES, max_workers=BRAND_WORKERS):
    """Fetch product detail pages in parallel for live SKU availability."""

    urls = list(dict.fromkeys(urls))
    if not urls:
        return []

    products = []
    worker_count = min(max_workers, len(urls))
    # Launch-page HTML reads are fast and not authenticated, so parallelism here saves
    # time before the slower StockX stage begins.
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(fetch_product_detail, url, set(target_sizes)): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                products.extend(future.result())
            except Exception as exc:
                print(f"Launch detail lookup failed for {url}: {exc}")

    return products


def fetch_product_detail(url, target_sizes=TARGET_SIZES):
    """Fetch and parse one launch detail page."""

    state = extract_initial_state(fetch_html(url))
    return parse_products(state, target_sizes=set(target_sizes), source_url=url)


@timed("launch_parse_products")
def parse_products(state, target_sizes=TARGET_SIZES, source_url=None):
    """Convert the brand's nested product/thread state into LaunchProduct objects."""

    product_state = state.get("product", {})
    threads = product_state.get("threads", {}).get("data", {}).get("items", {})
    products = product_state.get("products", {}).get("data", {}).get("items", {})
    availability_items = (
        product_state.get("availabilities", {}).get("data", {}).get("items", {})
    )

    parsed = []
    for thread in threads.values():
        slug = thread.get("seo", {}).get("slug")
        url = (
            f"{BRAND_BASE_URL}/ca/launch/t/{slug}"
            if slug
            else source_url or LAUNCH_IN_STOCK_URL
        )

        # A single launch thread can contain multiple products, for example
        # men's, grade school, preschool, and toddler versions.
        for product_id in thread.get("productIds", []):
            product = products.get(product_id)
            if not product:
                continue

            available_size_details = find_available_size_details(
                product,
                availability_items.get(product_id, {}),
                target_sizes=set(target_sizes),
            )
            if not available_size_details:
                # Filtering here prevents unnecessary StockX calls for products
                # that are not buyable in the sizes you care about.
                continue
            available_sizes = [
                size_detail["size"] for size_detail in available_size_details
            ]
            merch_price = product.get("merchPrice") if isinstance(product.get("merchPrice"), dict) else {}
            price = to_float(
                next(
                    (
                        value
                        for value in (
                            product.get("currentPrice"),
                            product.get("fullPrice"),
                            product.get("msrp"),
                            merch_price.get("currentPrice"),
                        )
                        if value is not None
                    ),
                    None,
                )
            )

            parsed.append(
                LaunchProduct(
                    title=product.get("title") or thread.get("title") or "",
                    subtitle=product.get("subtitle"),
                    url=url,
                    product_id=product_id,
                    style_code=product.get("styleColor"),
                    price=price,
                    real_price=calculate_real_price(price),
                    currency=product.get("currency") or merch_price.get("currency"),
                    image_url=product.get("imageSrc"),
                    product_type=product.get("productType"),
                    launch_status=product.get("launchStatus"),
                    available_sizes=available_sizes,
                    available_size_details=available_size_details,
                )
            )

    return parsed


def find_available_size_details(product, availability, target_sizes):
    """Return target-size availability with SKU metadata, reading both payload shapes."""

    sizes = availability.get("sizes", {})
    details_by_size = {}

    for size, size_data in sizes.items():
        if size in target_sizes and size_data.get("available"):
            details_by_size[size] = {
                "size": size,
                "localized_size": None,
                "sku_id": size_data.get("skuId"),
                "gtin": size_data.get("gtin"),
                "stock_level": None,
                "available": True,
            }

    for sku in product.get("skus", []):
        size = str(sku.get("brand_size") or "").strip()
        if size in target_sizes and sku.get("available"):
            country_specs = sku.get("country_specifications") or []
            country_spec = country_specs[0] if country_specs else {}
            details_by_size[size] = {
                "size": size,
                "localized_size": country_spec.get("localized_size"),
                "sku_id": sku.get("id"),
                "gtin": sku.get("gtin"),
                "stock_level": sku.get("level"),
                "available": True,
            }

    return sorted(details_by_size.values(), key=lambda item: size_sort_key(item["size"]))


def size_sort_key(size):
    try:
        return float(size)
    except (TypeError, ValueError):
        return 999
