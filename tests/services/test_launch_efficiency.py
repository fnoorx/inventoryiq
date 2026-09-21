from unittest.mock import Mock

from services.launch import brand


def test_parse_products_derives_sizes_from_one_availability_detail_pass(monkeypatch):
    size_details = [
        {
            "size": "10.5",
            "localized_size": "10.5",
            "sku_id": "sku-1",
            "gtin": "123",
            "stock_level": "HIGH",
            "available": True,
        }
    ]
    find_details = Mock(return_value=size_details)
    monkeypatch.setattr(brand, "find_available_size_details", find_details)
    state = {
        "product": {
            "threads": {
                "data": {
                    "items": {
                        "thread-1": {
                            "seo": {"slug": "test-product"},
                            "productIds": ["product-1"],
                            "title": "Test Product",
                        }
                    }
                }
            },
            "products": {
                "data": {
                    "items": {
                        "product-1": {
                            "title": "Test Product",
                            "styleColor": "TEST-001",
                            "currentPrice": 100,
                        }
                    }
                }
            },
            "availabilities": {"data": {"items": {"product-1": {}}}},
        }
    }

    products = brand.parse_products(state, target_sizes={"10.5"})

    assert len(products) == 1
    assert products[0].available_sizes == ["10.5"]
    assert products[0].available_size_details == size_details
    find_details.assert_called_once()
