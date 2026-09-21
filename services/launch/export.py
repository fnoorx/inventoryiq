"""JSON export helpers for inspecting availability-only launch scrape results."""

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path

from .config import LAUNCH_IN_STOCK_URL, LAUNCH_PRODUCTS_PATH, TARGET_SIZES
from .brand import fetch_launch_products, size_sort_key


def export_available_launch_products(
    target_sizes=TARGET_SIZES,
    output_path=LAUNCH_PRODUCTS_PATH,
):
    products = fetch_launch_products(target_sizes=target_sizes)
    save_launch_products(products, target_sizes=target_sizes, output_path=output_path)
    return products


def save_launch_products(products, target_sizes=TARGET_SIZES, output_path=LAUNCH_PRODUCTS_PATH):
    """Write the availability-only scrape result to disk for inspection/testing."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "saved_at": datetime.now().isoformat(),
        "source_url": LAUNCH_IN_STOCK_URL,
        "target_sizes": sorted(target_sizes, key=size_sort_key),
        "count": len(products),
        "products": [asdict(product) for product in products],
    }

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    return output_path
