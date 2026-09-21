from services import brand_catalogue


class FakeLink:
    def __init__(self, href, text="", aria_label=""):
        self.href = href
        self.text = text
        self.aria_label = aria_label

    def get_attribute(self, name):
        return {
            "href": self.href,
            "aria-label": self.aria_label,
            "src": "https://static.brand.example.test/image.jpg",
        }.get(name, "")


class FakeImage:
    def get_attribute(self, name):
        return "https://static.brand.example.test/image.jpg" if name == "src" else ""


class FakeCard:
    def __init__(self, name, subtitle, hrefs, price_text):
        self.name = name
        self.subtitle = subtitle
        self.hrefs = hrefs
        self.price_text = price_text
        self.text = f"{name}\n{subtitle}\n{price_text}"

    def find_elements(self, by, selector):
        if selector == 'a[href*="/ca/t/"]':
            return [
                FakeLink(href, self.name if index == 0 else "")
                for index, href in enumerate(self.hrefs)
            ] + [FakeLink(self.hrefs[0], self.subtitle)]
        if selector in ('a[aria-label*="price"]', '[aria-label*="price"]'):
            return [FakeLink("", aria_label=self.price_text)]
        if selector == "img":
            return [FakeImage()]
        return []


class FakeDriver:
    def __init__(self, cards):
        self.cards = cards
        self.scrolled = False
        self.quit_called = False

    def get(self, url):
        assert url == brand_catalogue.BRAND_CATALOGUE_URL

    def find_elements(self, by, selector):
        if selector in (brand_catalogue.BRAND_PRODUCT_CARD_SELECTOR, "main figure"):
            return self.cards
        if selector in ("main h1, h1",):
            return []
        if selector == "//button[normalize-space()='Decline All']":
            return []
        return []

    def execute_script(self, script, *args):
        self.scrolled = True

    def quit(self):
        self.quit_called = True


def test_category_and_style_helpers():
    assert brand_catalogue.normalize_category("") is None
    assert brand_catalogue.normalize_category(" FOOTWEAR ") == "footwear"
    assert brand_catalogue.classify_product_category("Women's Road Running Shoes") == "footwear"
    assert brand_catalogue.classify_product_category("Men's Fleece Hoodie") == "apparel"
    assert brand_catalogue.extract_style_codes([
        "https://brand.example.test/ca/t/test/QX2002-001",
        "https://brand.example.test/ca/t/test/QX2002-001",
        "https://brand.example.test/ca/t/test/QX2002-100",
    ]) == ["QX2002-001", "QX2002-100"]
    assert brand_catalogue.parse_price("current price $1,234.50, original price $2,000") == 1234.5


def test_fetch_brand_catalogue_products_filters_to_requested_category(monkeypatch):
    monkeypatch.setattr(brand_catalogue, "BRAND_SCROLL_PAUSE_SECONDS", 0)
    driver = FakeDriver([
        FakeCard(
            "Aether Test Hoodie",
            "Men's Fleece Hoodie",
            ["https://brand.example.test/ca/t/test-hoodie/QX2001-010"],
            "current price $79.99, original price $120",
        ),
        FakeCard(
            "Aether Test Shoe",
            "Women's Road Running Shoes",
            ["https://brand.example.test/ca/t/test-shoe/QX2009-100"],
            "$129.99",
        ),
    ])

    products = brand_catalogue.fetch_brand_catalogue_products(
        "apparel",
        driver_factory=lambda: driver,
    )

    assert [product["style_codes"] for product in products] == [["QX2001-010"]]
    assert products[0]["price"].startswith("current price $79.99")
    assert driver.quit_called is True


def test_fetch_brand_catalogue_products_blank_scans_apparel_and_footwear(monkeypatch):
    monkeypatch.setattr(brand_catalogue, "BRAND_SCROLL_PAUSE_SECONDS", 0)
    driver = FakeDriver([
        FakeCard(
            "Aether Test Hoodie",
            "Men's Fleece Hoodie",
            ["https://brand.example.test/ca/t/test-hoodie/QX2001-010"],
            "$79.99",
        ),
        FakeCard(
            "Aether Test Bottle",
            "Water Bottle",
            ["https://brand.example.test/ca/t/test-bottle/QX2008-001"],
            "$25.00",
        ),
    ])

    products = brand_catalogue.fetch_brand_catalogue_products(driver_factory=lambda: driver)

    assert [product["style_codes"] for product in products] == [["QX2001-010"]]


class LazyDriver(FakeDriver):
    def __init__(self, batches):
        super().__init__(batches[0])
        self.batches = batches
        self.batch_index = 0

    def find_elements(self, by, selector):
        if selector == "main h1, h1":
            return [FakeLink("", text=f"All Products ({sum(map(len, self.batches))})")]
        return super().find_elements(by, selector)

    def execute_script(self, script, *args):
        super().execute_script(script, *args)
        if "scrollBy" in script and self.batch_index < len(self.batches) - 1:
            self.batch_index += 1
            self.cards = self.batches[self.batch_index]


def test_virtualized_grid_accumulates_products_removed_from_dom(monkeypatch):
    monkeypatch.setattr(brand_catalogue, "BRAND_SCROLL_PAUSE_SECONDS", 0)
    batches = []
    for start, count in ((0, 6), (6, 6), (12, 2)):
        batches.append(
            [
                FakeCard(
                    f"Aether Hoodie {index}",
                    "Men's Fleece Hoodie",
                    [f"https://brand.example.test/ca/t/hoodie-{index}/QX{index:04d}-010"],
                    "$79.99",
                )
                for index in range(start, start + count)
            ]
        )
    driver = LazyDriver(batches)

    products = brand_catalogue.fetch_brand_catalogue_products(
        "apparel",
        driver_factory=lambda: driver,
    )

    assert len(products) == 14
    assert products[0]["style_codes"] == ["QX0000-010"]
    assert products[-1]["style_codes"] == ["QX0013-010"]


def test_scan_brand_catalogue_uses_configured_profitability_assumptions(monkeypatch):
    monkeypatch.setattr(brand_catalogue, "DISCOUNT_PERCENT", 20)
    monkeypatch.setattr(brand_catalogue, "MINIMUM_PROFIT", 25)
    products = [{"product": "Aether Test Shoe", "style_codes": ["QX2001-010"], "price": "$100"}]
    captured = {}

    monkeypatch.setattr(
        brand_catalogue,
        "fetch_brand_catalogue_products",
        lambda category: products,
    )

    def check(discount, *, products, minimum_profit):
        captured.update(
            discount=discount,
            products=products,
            minimum_profit=minimum_profit,
        )
        return object()

    monkeypatch.setattr(brand_catalogue, "check_catalogue", check)
    scan = brand_catalogue.scan_brand_catalogue("footwear")

    assert scan.category == "footwear"
    assert captured == {"discount": 20, "products": products, "minimum_profit": 25}
