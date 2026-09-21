"""Synthetic catalogue adapter for the public portfolio edition.

The authenticated private catalogue integration is intentionally excluded.
Snapshot comparison and price-change detection remain shared application logic.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from selenium.webdriver.common.by import By

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "catalogue_snapshot.json"
NEW_PRODUCTS_PATH = DATA_DIR / "new_products.json"
PRICE_SELECTORS = ("[data-price]", ".price")
PRICE_TEXT_RE = re.compile(r"(?:CAD\s*)?\$\s*\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*(?:\.\d{1,2})?\s*CAD", re.IGNORECASE)

def extract_price(card_el):
    """Return the displayed catalogue price text, if the product tile has one.

    The catalogue markup has changed between storefront versions, so this uses
    a small selector set and only accepts text that actually contains a money
    value. Numeric non-price text such as a size must not become a cost.
    """

    for selector in PRICE_SELECTORS:
        try:
            elements = card_el.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            continue

        for element in elements:
            text = price_text(element)
            match = PRICE_TEXT_RE.search(text)
            if match:
                return match.group(0).strip()
    return ""


def price_text(price_element) -> str:
    """Read a price container whose currency and cents may be separate spans."""

    try:
        spans = price_element.find_elements(By.CSS_SELECTOR, "span")
    except Exception:
        spans = []

    if spans:
        parts = []
        for span in spans:
            try:
                part = span.get_attribute("textContent") or span.text or ""
            except Exception:
                part = ""
            parts.append(str(part).strip())
        combined = "".join(parts).strip()
        if combined:
            return combined

    try:
        return (price_element.get_attribute("textContent") or price_element.text or "").strip()
    except Exception:
        return ""


def normalize_link(link: str) -> str:
    return (link or "").lower().strip().removesuffix(".html")


def load_snapshot(snapshot_path: Path = SNAPSHOT_PATH) -> dict:
    if not snapshot_path.exists():
        return {}

    with snapshot_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    products = payload.get("products", {})
    if not isinstance(products, dict):
        return {}

    return products


def build_snapshot(current_products: list[dict]) -> dict:
    snapshot = {}
    for product in current_products:
        key = normalize_link(product.get("link"))
        if not key:
            continue
        snapshot[key] = {
            "product": product.get("product"),
            "link": product.get("link"),
            "style_codes": product.get("style_codes", []),
            "price": product.get("price", ""),
        }
    return snapshot


def save_snapshot(
    current_products: list[dict],
    new_products: list[dict],
    snapshot_path: Path = SNAPSHOT_PATH,
    new_products_path: Path = NEW_PRODUCTS_PATH,
    *,
    price_changes: list[dict] | None = None,
) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(),
        "products": build_snapshot(current_products)
    }
    with snapshot_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    new_products_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "saved_at": datetime.now().isoformat(),
        "new_products": build_snapshot(new_products),
        "price_changes": price_changes or [],
    }
    with new_products_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)


def find_new_products(previous_snapshot: dict, current_products: list[dict]) -> list[dict]:
    new_products = []
    for product in current_products:
        key = normalize_link(product.get("link"))
        if key not in previous_snapshot:
            new_products.append(product)

    return new_products


def comparable_price(value) -> Decimal | None:
    """Compare money values, ignoring currency labels and display formatting."""

    text = str(value or "").strip().upper().replace("CAD", "").replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def find_price_changes(previous_snapshot: dict, current_products: list[dict]) -> list[dict]:
    changes = []
    for product in current_products:
        previous = previous_snapshot.get(normalize_link(product.get("link")))
        if previous is None:
            continue
        old_price = comparable_price(previous.get("price"))
        new_price = comparable_price(product.get("price"))
        # Missing prices cannot establish a price change (including old snapshots).
        if old_price is not None and new_price is not None and old_price != new_price:
            changes.append({**product, "change_type": "price_change", "old_price": previous["price"]})
    return changes


def demo_catalogue():
    """Fictional products: no real account, retailer or inventory data."""

    return [
        {"product": "Demo Running Shoe", "link": "https://catalogue.example.test/shoe",
         "style_codes": ["DEMO-001"], "price": "$70.00"},
        {"product": "Demo Fleece Hoodie", "link": "https://catalogue.example.test/hoodie",
         "style_codes": ["DEMO-002"], "price": "$50.00"},
    ]


def main():
    """Return synthetic new products and changes relative to the local snapshot."""

    products = demo_catalogue()
    previous = load_snapshot()
    new_products = find_new_products(previous, products)
    changes = find_price_changes(previous, products)
    save_snapshot(products, new_products, price_changes=changes)
    return new_products + changes
