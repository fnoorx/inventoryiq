import requests
from unittest.mock import Mock

from services import catalogue_profitability as profitability
from services.pricing import calculate_total_cost


def product(style_code="TEST-001", price="$100.00", link="https://example.com/test"):
    return {
        "product": "Test Shoe",
        "link": link,
        "style_codes": [style_code],
        "price": price,
    }


def market_data(avg=160, bid=150):
    return {
        "9": {
            "avg_sales": avg,
            "highest_bid": bid,
        },
        "10": {
            "avg_sales": 120,
            "highest_bid": 110,
        },
    }


def test_parse_catalogue_price_and_discount_calculation():
    assert profitability.parse_catalogue_price("CAD $1,234.50") == 1234.5
    assert profitability.parse_catalogue_price(120) == 120.0
    assert profitability.parse_catalogue_price("not priced") is None
    assert calculate_total_cost(100, 0) == 113.0
    assert calculate_total_cost(100, 30) == 79.1


def test_check_catalogue_uses_each_unique_style_once_and_returns_best_size(monkeypatch):
    calls = []
    monkeypatch.setattr(
        profitability,
        "get_product_id",
        lambda style_code: calls.append(style_code) or f"product-{style_code}",
    )
    monkeypatch.setattr(
        profitability,
        "get_product_details",
        lambda product_id: {
            "styleId": product_id.removeprefix("product-"),
            "urlKey": "demo-test-shoe",
        },
    )
    monkeypatch.setattr(profitability, "get_product_market_data", lambda _product_id: market_data())

    result = profitability.check_catalogue(
        30,
        products=[product(), product(link="https://example.com/duplicate")],
    )

    assert calls == ["TEST-001"]
    assert result.scanned_style_codes == 1
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.best_size == "9"
    assert candidate.discounted_price == 79.1
    assert candidate.net_avg_sale == 142.4
    assert candidate.estimated_profit == 63.3
    assert candidate.profit_source == "avg sale"
    assert candidate.stockx_link == "https://stockx.com/demo-test-shoe"


def test_check_catalogue_requires_profit_strictly_over_thirty(monkeypatch):
    monkeypatch.setattr(profitability, "get_product_id", lambda _style_code: "product-1")
    monkeypatch.setattr(
        profitability,
        "get_product_details",
        lambda _product_id: {"styleId": "TEST-001", "urlKey": "test-shoe"},
    )
    # 100 * .89 = 89 net. At 41% off with 13% tax, the profit is below $30.
    monkeypatch.setattr(
        profitability,
        "get_product_market_data",
        lambda _product_id: {"9": {"avg_sales": 100, "highest_bid": None}},
    )

    result = profitability.check_catalogue(41, products=[product(price="$100")])

    assert result.candidates == []


def test_check_catalogue_skips_unpriced_and_tracks_unresolved_styles(monkeypatch):
    monkeypatch.setattr(profitability, "get_product_id", lambda _style_code: None)

    result = profitability.check_catalogue(
        10,
        products=[product(style_code="MISSING-001"), product(style_code="NO-PRICE-001", price="")],
    )

    assert result.scanned_style_codes == 1
    assert result.skipped_missing_price == 1
    assert result.unresolved_style_codes == ["MISSING-001"]


def test_check_catalogue_skips_only_styles_with_persistent_stockx_errors(monkeypatch):
    def product_id(style_code):
        if style_code == "TIMEOUT-001":
            raise requests.ReadTimeout("StockX timed out")
        return "product-good"

    monkeypatch.setattr(profitability, "get_product_id", product_id)
    monkeypatch.setattr(
        profitability,
        "get_product_details",
        lambda _product_id: {"styleId": "GOOD-001", "urlKey": "good-shoe"},
    )
    monkeypatch.setattr(profitability, "get_product_market_data", lambda _product_id: market_data())

    result = profitability.check_catalogue(
        30,
        products=[product("TIMEOUT-001"), product("GOOD-001")],
    )

    assert result.stockx_error_style_codes == ["TIMEOUT-001"]
    assert [candidate.style_code for candidate in result.candidates] == ["GOOD-001"]


def test_check_catalogue_rejects_product_detail_style_mismatch(monkeypatch):
    monkeypatch.setattr(profitability, "get_product_id", lambda _style_code: "nearest-product")
    monkeypatch.setattr(
        profitability,
        "get_product_details",
        lambda _product_id: {"styleId": "TEST-002", "urlKey": "wrong-shoe"},
    )
    market_lookup = Mock()
    monkeypatch.setattr(profitability, "get_product_market_data", market_lookup)

    result = profitability.check_catalogue(30, products=[product("TEST-001")])

    assert result.candidates == []
    assert result.unresolved_style_codes == ["TEST-001"]
    market_lookup.assert_not_called()


def test_validate_discount_percent_rejects_invalid_ranges():
    assert profitability.validate_discount_percent("30") == 30.0
    for value in ("nope", -1, 100):
        try:
            profitability.validate_discount_percent(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected {value!r} to be rejected")
