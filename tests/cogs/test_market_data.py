import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs import market_data
from services.stockx import StockxMarketDetails


class FakeRepository:
    def __init__(self, item=None):
        self.item = item

    def get(self, inventory_id):
        return self.item


def inventory_item():
    return SimpleNamespace(
        inventory_id="INV-000001",
        style_code="QX1001-200",
        size="10",
        product_name="Aether Trail Test",
        stockx_product_id="product-1",
        stockx_variant_id="variant-10",
    )


async def run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


def test_resolve_market_lookup_preserves_style_code_lookup():
    lookup = market_data.resolve_market_lookup(" qx1001-200 ", "10")

    assert lookup == market_data.MarketLookup(style_code="QX1001-200", size="10")


def test_resolve_market_lookup_uses_inventory_style_and_size():
    lookup = market_data.resolve_market_lookup(
        "inv-000001",
        repository=FakeRepository(inventory_item()),
    )

    assert lookup == market_data.MarketLookup(
        style_code="QX1001-200",
        size="10",
        inventory_id="INV-000001",
        product_name="Aether Trail Test",
        stockx_product_id="product-1",
        stockx_variant_id="variant-10",
    )


def test_inventory_market_lookup_rejects_override_size():
    with pytest.raises(ValueError, match="stored unit size"):
        market_data.resolve_market_lookup(
            "INV-000001",
            "11",
            repository=FakeRepository(inventory_item()),
        )


def test_inventory_market_lookup_reports_missing_item():
    with pytest.raises(ValueError, match="was not found"):
        market_data.resolve_market_lookup(
            "INV-000001",
            repository=FakeRepository(),
        )


def test_malformed_inventory_id_is_not_treated_as_a_style_code():
    with pytest.raises(ValueError, match="INV-000001"):
        market_data.resolve_market_lookup("INV-ABC")


def test_inventory_market_message_identifies_resolved_unit():
    messages = market_data.build_message(
        {
            "avg_sales": 120,
            "highest_bid": 110,
            "lowest_ask": 130,
            "flex_lowest_ask": 125,
            "beat_US": 115,
        },
        product_name="Aether Trail Test",
        style_code="QX1001-200",
        size="10",
        inventory_id="INV-000001",
    )

    assert "Product   Aether Trail Test" in messages[0]
    assert "Style     QX1001-200" in messages[0]
    assert "Size      10" in messages[0]
    assert "Inventory INV-000001" in messages[0]


def test_send_market_lookup_resolves_inventory_before_stockx(monkeypatch):
    lookup = market_data.MarketLookup(
        style_code="QX1001-200",
        size="10",
        inventory_id="INV-000001",
        product_name="Aether Trail Test",
        stockx_product_id="product-1",
        stockx_variant_id="variant-10",
    )
    resolve = Mock(return_value=lookup)
    get_market = Mock(
        return_value=StockxMarketDetails(
            product_name="Aether Trail Test",
            style_code="QX1001-200",
            size="10",
            market_data={
                "avg_sales": 120,
                "highest_bid": 110,
                "lowest_ask": 130,
                "flex_lowest_ask": 125,
                "beat_US": 115,
            },
        )
    )
    monkeypatch.setattr(market_data.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(market_data, "resolve_market_lookup", resolve)
    monkeypatch.setattr(market_data, "get_market_data_details", get_market)
    destination = SimpleNamespace(send=AsyncMock())
    cog = market_data.MarketData(bot=object())

    asyncio.run(cog._send_market_lookup(destination, "INV-000001"))

    resolve.assert_called_once_with("INV-000001", None)
    get_market.assert_called_once_with(
        "QX1001-200",
        "10",
        "product-1",
        "Aether Trail Test",
        "variant-10",
    )
    assert "INV-000001" in destination.send.await_args.args[0]


def test_send_market_lookup_reports_no_exact_stockx_product(monkeypatch):
    monkeypatch.setattr(market_data.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(
        market_data,
        "resolve_market_lookup",
        Mock(return_value=market_data.MarketLookup(style_code="QX1007-600")),
    )
    monkeypatch.setattr(market_data, "get_market_data_details", Mock(return_value=None))
    destination = SimpleNamespace(send=AsyncMock())
    cog = market_data.MarketData(bot=object())

    asyncio.run(cog._send_market_lookup(destination, "QX1007-600"))

    destination.send.assert_awaited_once_with(
        "No exact StockX product or market data was found for `QX1007-600`."
    )
