from services import scrape_catalogue
import json
import pytest


class FakeElement:
    def __init__(self, text, span_texts=None):
        self.text = text
        self.span_texts = span_texts or []

    def find_elements(self, _by, _selector):
        return [FakeElement(text) for text in self.span_texts]

    def get_attribute(self, attribute):
        if attribute == "textContent":
            return self.text
        return ""


class FakeCard:
    def __init__(self, elements):
        self.elements = elements

    def find_elements(self, _by, _selector):
        return self.elements


def test_extract_price_ignores_non_money_text_and_returns_displayed_price():
    card = FakeCard([FakeElement("Men's 10"), FakeElement("CAD $129.99"), FakeElement("Add to bag")])

    assert scrape_catalogue.extract_price(card) == "CAD $129.99"


def test_extract_price_joins_split_currency_dollars_decimal_and_cents_spans():
    card = FakeCard([FakeElement("$24.40", span_texts=["$", "24", ".", "40"])])

    assert scrape_catalogue.extract_price(card) == "$24.40"


def test_build_snapshot_persists_price_for_later_catalogue_checks():
    snapshot = scrape_catalogue.build_snapshot(
        [
            {
                "product": "Aether Test Shoe",
                "link": "https://example.com/test.html",
                "style_codes": ["TEST-001"],
                "price": "$120.00",
            }
        ]
    )

    assert snapshot["https://example.com/test"]["price"] == "$120.00"


@pytest.mark.parametrize("old,new,changed", [
    ("$100", "$70.00", True),
    ("$70", "$100", True),
    ("CAD $1,000.00", "$1000", False),
    ("", "$70", False),
    ("$100", "", False),
    ("$100", "NaN", False),
])
def test_price_change_detection(old, new, changed):
    product = {"link": "https://example.com/shoe.html", "price": old}
    previous = scrape_catalogue.build_snapshot([product])
    current = [{**product, "price": new}]
    assert scrape_catalogue.find_new_products(previous, current) == []
    changes = scrape_catalogue.find_price_changes(previous, current)
    assert bool(changes) is changed
    if changed:
        assert changes[0]["old_price"] == old
        assert changes[0]["price"] == new


def test_snapshot_separates_new_items_and_changes_and_avoids_repeat_alert(tmp_path):
    old = {"link": "https://example.com/shoe", "price": "$100"}
    updated = {**old, "price": "$70"}
    new = {"link": "https://example.com/new", "price": "$50"}
    previous = scrape_catalogue.build_snapshot([old])
    current = [updated, new]
    new_items = scrape_catalogue.find_new_products(previous, current)
    changes = scrape_catalogue.find_price_changes(previous, current)
    assert new_items == [new]
    assert len(changes) == 1
    snapshot_path = tmp_path / "snapshot.json"
    changes_path = tmp_path / "changes.json"
    scrape_catalogue.save_snapshot(current, new_items, snapshot_path, changes_path, price_changes=changes)
    saved_changes = json.loads(changes_path.read_text())
    assert list(saved_changes["new_products"]) == [new["link"]]
    assert saved_changes["price_changes"][0]["old_price"] == "$100"
    saved = scrape_catalogue.load_snapshot(snapshot_path)
    assert scrape_catalogue.find_price_changes(saved, current) == []
    assert scrape_catalogue.find_new_products(saved, current) == []
