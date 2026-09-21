"""Launch scraper settings."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
LAUNCH_PRODUCTS_PATH = ROOT_DIR / "data" / "launch_products.json"

LAUNCH_IN_STOCK_URL = "https://brand.example.test/ca/launch/in-stock"
BRAND_BASE_URL = "https://brand.example.test"

# Only these sizes are checked, which also avoids unnecessary StockX calls.
TARGET_SIZES = {"9", "10", "11"}  # Illustrative demo sizes.

# Launch pages are cheap HTTP reads, so this can be higher than StockX workers.
BRAND_WORKERS = 8
# StockX is the slow, rate-limited part. Workers let several products progress
# while services.stockx.stockx_get still spaces out the actual HTTP requests.
STOCKX_WORKERS = 3
REQUEST_TIMEOUT_SECONDS = 20

MIN_PROFIT_CAD = 40
