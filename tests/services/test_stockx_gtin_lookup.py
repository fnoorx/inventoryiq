from services.stockx_gtin_lookup import (
    build_lookup_result,
    format_lookup_result,
    normalize_gtin,
)


def test_build_lookup_result_combines_variant_product_and_matches_expected_size():
    variant = {
        "productId": "product-1",
        "variantId": "variant-1",
        "variantName": "shoe size",
        "variantValue": "M 10.0",
        "isFlexEligible": True,
        "isDirectEligible": False,
        "gtins": [{"identifier": "001234567890", "type": "UPC"}],
    }
    product = {
        "title": "Aether Test Shoe",
        "brand": "Aether",
        "styleId": " qx1001-200 ",
        "productType": "sneakers",
        "urlKey": "demo-test-shoe",
    }

    result = build_lookup_result(" 001234567890 ", variant, product, expected_size="10")

    assert result.gtin == "001234567890"
    assert result.product_id == "product-1"
    assert result.variant_id == "variant-1"
    assert result.variant_value == "M 10.0"
    assert result.normalized_size == "10"
    assert result.title == "Aether Test Shoe"
    assert result.style_id == "QX1001-200"
    assert result.stockx_url == "https://stockx.com/demo-test-shoe"
    assert result.is_flex_eligible is True
    assert result.is_direct_eligible is False
    assert result.expected_normalized_size == "10"
    assert result.size_matches_expected is True


def test_build_lookup_result_marks_size_mismatch():
    variant = {
        "productId": "product-1",
        "variantId": "variant-1",
        "variantValue": "W 7.0",
    }

    result = build_lookup_result("123", variant, {}, expected_size="7")

    assert result.normalized_size == "7W"
    assert result.expected_normalized_size == "7"
    assert result.size_matches_expected is False


def test_format_lookup_result_includes_size_check_and_known_gtins():
    result = build_lookup_result(
        "123",
        {
            "productId": "product-1",
            "variantId": "variant-1",
            "variantName": "shoe size",
            "variantValue": "M 10",
            "isFlexEligible": True,
            "isDirectEligible": False,
            "gtins": [{"identifier": "123", "type": "UPC"}],
        },
        {
            "title": "Aether Test Shoe",
            "brand": "Aether",
            "styleId": "QX1001-200",
            "productType": "sneakers",
        },
        expected_size="10",
    )

    text = format_lookup_result(result)

    assert "Product: Aether Test Shoe" in text
    assert "Style Code: QX1001-200" in text
    assert "Size Check: MATCH" in text
    assert "Known GTINs: UPC 123" in text
    assert "StockX URL: https://stockx.com/search?s=QX1001-200" in text


def test_normalize_gtin_preserves_leading_zeroes_and_removes_whitespace():
    assert normalize_gtin("  001 234\n567890 ") == "001234567890"
