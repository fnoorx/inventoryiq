from types import SimpleNamespace
from unittest.mock import Mock

from services import stockx


def test_product_lookup_selects_exact_style_instead_of_first_search_result(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "products": [
                {
                    "productId": "closest-product",
                    "styleId": "QX1007-601",
                },
                {
                    "productId": "exact-product",
                    "styleId": "qx1007 600",
                },
            ]
        },
    )
    monkeypatch.setattr(stockx, "stockx_get", lambda *args, **kwargs: response)
    monkeypatch.setattr(stockx, "get_headers", lambda: {})

    assert stockx.get_product_id("QX1007-600") == "exact-product"


def test_product_lookup_returns_none_when_stockx_only_returns_close_matches(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "products": [
                {
                    "productId": "closest-product",
                    "styleId": "QX1007-601",
                }
            ]
        },
    )
    monkeypatch.setattr(stockx, "stockx_get", lambda *args, **kwargs: response)
    monkeypatch.setattr(stockx, "get_headers", lambda: {})

    assert stockx.get_product_id("QX1007-600") is None


def test_product_lookup_accepts_either_member_of_combined_stockx_style(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "products": [
                {
                    "productId": "combined-product",
                    "styleId": "QX1004-400/QX1003-400",
                }
            ]
        },
    )
    monkeypatch.setattr(stockx, "stockx_get", lambda *args, **kwargs: response)
    monkeypatch.setattr(stockx, "get_headers", lambda: {})

    assert stockx.get_product_id("QX1004-400") == "combined-product"
    assert stockx.get_product_id("QX1003-400") == "combined-product"
    assert stockx.get_product_id("QX1004-400/QX1003-400") == "combined-product"


def test_combined_style_query_does_not_accept_partially_overlapping_result():
    assert not stockx.market_style_codes_match(
        "QX1004-400/QX1003-400",
        "QX1004-400/DIFFERENT-001",
    )


def test_variant_lookup_matches_normalized_inventory_size(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        json=lambda: [
            {"variantId": "variant-10", "variantValue": "M 10.0"},
            {"variantId": "variant-11", "variantValue": "M 11.0"},
        ],
    )
    monkeypatch.setattr(stockx, "stockx_get", lambda *args, **kwargs: response)
    monkeypatch.setattr(stockx, "get_headers", lambda: {})

    assert stockx.get_variant_id("product-1", "10") == "variant-10"


def test_market_lookup_does_not_fall_back_to_all_sizes_when_size_is_missing(monkeypatch):
    monkeypatch.setattr(stockx, "get_product_id", lambda style_code: "product-1")
    monkeypatch.setattr(stockx, "get_variant_id", lambda product_id, size: None)
    get_product_market_data = Mock()
    monkeypatch.setattr(stockx, "get_product_market_data", get_product_market_data)

    assert stockx.get_market_data("QX1001-200", "10") is None
    get_product_market_data.assert_not_called()


def test_detailed_market_lookup_reuses_inventory_product_identity(monkeypatch):
    get_product_id = Mock()
    monkeypatch.setattr(stockx, "get_product_id", get_product_id)
    monkeypatch.setattr(
        stockx,
        "get_product_details",
        lambda product_id: {
            "title": "Aether Trail Test",
            "styleId": "QX1001-200",
        },
    )
    monkeypatch.setattr(
        stockx,
        "get_market_data_for_product",
        lambda product_id, size: {"lowest_ask": 150},
    )

    details = stockx.get_market_data_details(
        "QX1001-200",
        "10",
        product_id="product-1",
        product_name="Aether Trail Test",
    )

    assert details == stockx.StockxMarketDetails(
        product_name="Aether Trail Test",
        style_code="QX1001-200",
        size="10",
        market_data={"lowest_ask": 150},
    )
    get_product_id.assert_not_called()


def test_detailed_inventory_lookup_uses_stored_variant_without_listing_variants(
    monkeypatch,
):
    monkeypatch.setattr(
        stockx,
        "get_product_details",
        lambda product_id: {
            "title": "Aether Trail Test",
            "styleId": "QX1001-200",
        },
    )
    variant_market = Mock(return_value={"lowest_ask": 150})
    size_lookup = Mock()
    monkeypatch.setattr(stockx, "get_product_market_data", variant_market)
    monkeypatch.setattr(stockx, "get_market_data_for_product", size_lookup)

    details = stockx.get_market_data_details(
        "QX1001-200",
        "10",
        product_id="product-1",
        product_name="Aether Trail Test",
        variant_id="variant-10",
    )

    assert details.market_data == {"lowest_ask": 150}
    variant_market.assert_called_once_with("product-1", "variant-10")
    size_lookup.assert_not_called()


def test_detailed_inventory_lookup_falls_back_when_stored_variant_is_stale(monkeypatch):
    monkeypatch.setattr(
        stockx,
        "get_product_details",
        lambda product_id: {
            "title": "Aether Trail Test",
            "styleId": "QX1001-200",
        },
    )
    monkeypatch.setattr(stockx, "get_product_market_data", lambda *_args: None)
    size_lookup = Mock(return_value={"lowest_ask": 150})
    monkeypatch.setattr(stockx, "get_market_data_for_product", size_lookup)

    details = stockx.get_market_data_details(
        "QX1001-200",
        "10",
        product_id="product-1",
        variant_id="stale-variant",
    )

    assert details.market_data == {"lowest_ask": 150}
    size_lookup.assert_called_once_with("product-1", "10")


def test_detailed_market_lookup_rejects_mismatched_returned_style(monkeypatch):
    get_market_data = Mock()
    monkeypatch.setattr(
        stockx,
        "get_product_details",
        lambda product_id: {
            "title": "Completely Different Product",
            "styleId": "QX1007-601",
        },
    )
    monkeypatch.setattr(stockx, "get_market_data_for_product", get_market_data)

    details = stockx.get_market_data_details(
        "QX1007-600",
        product_id="closest-product",
        product_name="Stored Product",
    )

    assert details is None
    get_market_data.assert_not_called()


def test_detailed_market_lookup_accepts_individual_member_and_displays_returned_styles(
    monkeypatch,
):
    monkeypatch.setattr(
        stockx,
        "get_product_details",
        lambda product_id: {
            "title": "Aether Trail Test",
            "styleId": "QX1004-400/QX1003-400",
        },
    )
    monkeypatch.setattr(
        stockx,
        "get_market_data_for_product",
        lambda product_id, size: {"lowest_ask": 150},
    )

    details = stockx.get_market_data_details(
        "QX1003-400",
        "10",
        product_id="combined-product",
    )

    assert details == stockx.StockxMarketDetails(
        product_name="Aether Trail Test",
        style_code="QX1004-400/QX1003-400",
        size="10",
        market_data={"lowest_ask": 150},
    )
