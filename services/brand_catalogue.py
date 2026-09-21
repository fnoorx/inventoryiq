"""Scrape a public brand catalogue site and enrich it with StockX data."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable

from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

from services.catalogue_profitability import check_catalogue
from services.pricing import purchase_discount_percent
from services.product_classification import classify_product_category

DISCOUNT_PERCENT = float(purchase_discount_percent())
MINIMUM_PROFIT = 30


BRAND_CATALOGUE_URL = "https://brand.example.test/ca/w"
# The public catalogue is currently roughly 2,500 products and loads in
# batches. Give the browser enough time to reach the end of the infinite grid.
BRAND_CATALOGUE_TIMEOUT_SECONDS = 900
BRAND_SCROLL_PAUSE_SECONDS = 0.8
BRAND_MAX_STABLE_SCROLLS = 5
BRAND_PRODUCT_CARD_SELECTOR = 'main [data-testid="product-card"]'

# The retailer's product URL ends in a colour/style code, for example ``QX2005-673``.
STYLE_CODE_RE = re.compile(r"/([A-Za-z0-9]+-[A-Za-z0-9]{3})(?:[/?#]|$)", re.I)
PRICE_RE = re.compile(r"(?:CAD\s*)?\$\s*([\d,]+(?:\.\d{1,2})?)", re.I)

@dataclass(frozen=True)
class BrandCatalogueScan:
    """Products discovered and the profitability results for one scan."""

    category: str | None
    products: list[dict]
    result: object


def normalize_category(category: str | None) -> str | None:
    """Normalize the command filter; blank means both supported categories."""

    value = str(category or "").strip().casefold()
    if not value:
        return None
    if value not in {"apparel", "footwear"}:
        raise ValueError("Category must be `apparel`, `footwear`, or blank for both.")
    return value


def extract_style_codes(hrefs: list[str]) -> list[str]:
    """Extract unique brand style codes from all colourway links on a card."""

    codes: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        match = STYLE_CODE_RE.search(str(href or ""))
        if not match:
            continue
        code = match.group(1).upper()
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def parse_price(value: str | None) -> float | None:
    """Parse the first CAD price from a card's accessible price text."""

    match = PRICE_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _card_texts(card) -> tuple[str, str]:
    titles = card.find_elements(By.CSS_SELECTOR, ".product-card__title")
    subtitles = card.find_elements(By.CSS_SELECTOR, ".product-card__subtitle")
    if titles:
        name = (titles[0].text or "").strip() or "Unknown brand product"
        subtitle = (subtitles[0].text or "").strip() if subtitles else ""
        return name, subtitle

    links = card.find_elements(By.CSS_SELECTOR, 'a[href*="/ca/t/"]')
    texts = []
    for link in links:
        value = (link.text or "").strip()
        if value and value not in texts:
            texts.append(value)
    name = texts[0] if texts else "Unknown brand product"
    subtitle = texts[1] if len(texts) > 1 else ""
    return name, subtitle


def parse_product_card(card) -> dict | None:
    """Convert a rendered brand product figure into a catalogue row."""

    links = card.find_elements(By.CSS_SELECTOR, 'a[href*="/ca/t/"]')
    hrefs = [link.get_attribute("href") or "" for link in links]
    style_codes = extract_style_codes(hrefs)
    if not style_codes:
        return None

    name, subtitle = _card_texts(card)
    product_name = " — ".join(value for value in (name, subtitle) if value)

    price_text = ""
    price_links = card.find_elements(By.CSS_SELECTOR, '[aria-label*="price"]')
    if price_links:
        price_text = price_links[0].get_attribute("aria-label") or price_links[0].text or ""
    if not price_text:
        price_text = card.text or ""

    link = hrefs[0]
    image_url = ""
    images = card.find_elements(By.CSS_SELECTOR, "img")
    if images:
        image_url = images[0].get_attribute("src") or ""

    return {
        "product": product_name,
        "link": link,
        "style_codes": style_codes,
        "price": price_text,
        "image_url": image_url,
        "category": classify_product_category(product_name),
    }


def _dismiss_cookie_prompt(driver) -> None:
    """Dismiss the brand's optional cookie prompt if it is shown."""

    buttons = driver.find_elements(By.XPATH, "//button[normalize-space()='Decline All']")
    if buttons:
        try:
            buttons[0].click()
        except WebDriverException:
            pass


def _catalogue_total(driver) -> int | None:
    headings = driver.find_elements(By.CSS_SELECTOR, "main h1, h1")
    for heading in headings:
        match = re.search(r"\(([\d,]+)\)", heading.text or "")
        if match:
            return int(match.group(1).replace(",", ""))
    return None


def _visible_product_cards(driver) -> list:
    """Return current product cards, with a fallback for older brand markup."""

    cards = driver.find_elements(By.CSS_SELECTOR, BRAND_PRODUCT_CARD_SELECTOR)
    if cards:
        return cards
    return driver.find_elements(By.CSS_SELECTOR, "main figure")


def _load_all_products(driver, timeout: int = BRAND_CATALOGUE_TIMEOUT_SECONDS) -> list[dict]:
    """Accumulate products while scrolling the brand's virtualized infinite grid.

    brand may remove earlier cards from the DOM as later batches render. Keeping
    only the final set of elements can therefore reduce a full scan to the last
    few cards. Parse and retain every unique card while it is visible.
    """

    deadline = time.monotonic() + timeout
    stable_scrolls = 0
    previous_unique_count = 0
    total = None
    products_by_key: dict[tuple[str, str], dict] = {}

    while time.monotonic() < deadline:
        _dismiss_cookie_prompt(driver)
        cards = _visible_product_cards(driver)
        total = total or _catalogue_total(driver)

        for card in cards:
            try:
                product = parse_product_card(card)
            except StaleElementReferenceException:
                continue
            if not product:
                continue
            key = (product["link"], ",".join(product["style_codes"]))
            products_by_key[key] = product

        unique_count = len(products_by_key)
        if total is not None and unique_count >= total:
            return list(products_by_key.values())

        if unique_count == previous_unique_count:
            stable_scrolls += 1
        else:
            stable_scrolls = 0
            previous_unique_count = unique_count
        if stable_scrolls >= BRAND_MAX_STABLE_SCROLLS:
            return list(products_by_key.values())

        if cards:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'end'});",
                    cards[-1],
                )
            except StaleElementReferenceException:
                pass
        driver.execute_script(
            "window.scrollBy(0, Math.max(window.innerHeight * 0.9, 600));"
        )
        time.sleep(BRAND_SCROLL_PAUSE_SECONDS)

    raise TimeoutError("The brand catalogue did not finish loading before the scan timeout.")


def create_driver() -> webdriver.Chrome:
    """Create a headless Chrome instance suitable for a background Discord job."""

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    return webdriver.Chrome(options=options)


def fetch_brand_catalogue_products(
    category: str | None = None,
    *,
    driver_factory: Callable[[], object] = create_driver,
) -> list[dict]:
    """Scrape every loaded brand catalogue product card for the requested category."""

    normalized_category = normalize_category(category)
    driver = driver_factory()
    try:
        driver.get(BRAND_CATALOGUE_URL)
        loaded_products = _load_all_products(driver)
        products = []
        seen: set[tuple[str, str]] = set()
        for product in loaded_products:
            if product["category"] not in {"apparel", "footwear"}:
                continue
            if normalized_category and product["category"] != normalized_category:
                continue
            key = (product["link"], ",".join(product["style_codes"]))
            if key not in seen:
                seen.add(key)
                products.append(product)
        return products
    finally:
        driver.quit()


def scan_brand_catalogue(category: str | None = None) -> BrandCatalogueScan:
    """Discover brand catalogue products, then check their styles against StockX."""

    normalized_category = normalize_category(category)
    products = fetch_brand_catalogue_products(normalized_category)
    result = check_catalogue(
        DISCOUNT_PERCENT,
        products=products,
        minimum_profit=MINIMUM_PROFIT,
    )
    return BrandCatalogueScan(
        category=normalized_category,
        products=products,
        result=result,
    )
