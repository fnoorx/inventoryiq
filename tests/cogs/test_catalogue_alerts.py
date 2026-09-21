import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs import catalogue_alerts
from services.catalogue_profitability import CatalogueCheckResult, CatalogueProfitCandidate


SAMPLE_PRODUCTS = [
    {
        "product": "Aether x Harbor Collective - Full-Zip Hoodie",
        "link": "https://catalogue.example.test/qx2003-aether-x-harbor-collective-full-zip-hoodie.html",
        "style_codes": ["QX2003-010"],
    },
    {
        "product": "Aether Studio - Women's Layer Top",
        "link": "https://catalogue.example.test/qx2004-aether-studio-women-s-layer-top.html",
        "style_codes": ["QX2004-010"],
    },
]


def make_ctx(channel_id=catalogue_alerts.CHANNEL_ID):
    return SimpleNamespace(
        channel=SimpleNamespace(id=channel_id),
        send=AsyncMock(),
    )


def run_scrape_command(ctx):
    cog = catalogue_alerts.CatalogueAlerts(bot=object())
    command_callback = catalogue_alerts.CatalogueAlerts.scrape.callback
    asyncio.run(command_callback(cog, ctx))


def run_check_command(ctx, discount="30"):
    cog = catalogue_alerts.CatalogueAlerts(bot=object())
    command_callback = catalogue_alerts.CatalogueAlerts.check.callback
    asyncio.run(command_callback(cog, ctx, discount))


def run_check_without_discount(ctx):
    cog = catalogue_alerts.CatalogueAlerts(bot=object())
    command_callback = catalogue_alerts.CatalogueAlerts.check.callback
    asyncio.run(command_callback(cog, ctx))


def test_build_message_formats_products():
    messages = catalogue_alerts.build_message(SAMPLE_PRODUCTS)

    assert messages == [
        "**New products found:**\n"
        "__**Apparel**__\n"
        "**Men's / Unisex**\n"
        "**Item: Aether x Harbor Collective - Full-Zip Hoodie**\n"
        "QX2003-010\n"
        "https://catalogue.example.test/qx2003-aether-x-harbor-collective-full-zip-hoodie.html\n"
        "**Women's**\n"
        "**Item: Aether Studio - Women's Layer Top**\n"
        "QX2004-010\n"
        "https://catalogue.example.test/qx2004-aether-studio-women-s-layer-top.html\n"
    ]


def test_build_message_sorts_by_category_then_gender_then_name():
    products = [
        product("Women's Utility Bag"),
        product("Aether Sprint - Big Kids' Shoes"),
        product("Aether Trail - Women's Shoes"),
        product("Unisex Bottle"),
        product("Women's Leggings"),
        product("Men's Jacket"),
        product("Alpha Unisex Jacket"),
        product("Aether Trail - Men's Shoes"),
    ]

    message = "".join(catalogue_alerts.build_message(products))

    expected_order = [
        "__**Apparel**__",
        "**Item: Alpha Unisex Jacket**",
        "**Item: Men's Jacket**",
        "**Item: Women's Leggings**",
        "__**Footwear**__",
        "**Item: Aether Trail - Men's Shoes**",
        "**Item: Aether Trail - Women's Shoes**",
        "**Item: Aether Sprint - Big Kids' Shoes**",
        "__**Other**__",
        "**Item: Unisex Bottle**",
        "**Item: Women's Utility Bag**",
    ]
    positions = [message.index(value) for value in expected_order]
    assert positions == sorted(positions)


def test_product_classification_handles_catalogue_names():
    assert catalogue_alerts.classify_product_category("Aether Trail - Men's Shoes") == "footwear"
    assert catalogue_alerts.classify_product_category("Aether Rally - Women's Shoe") == "footwear"
    assert catalogue_alerts.classify_product_category("Aether Trail - Track Spikes") == "footwear"
    assert catalogue_alerts.classify_product_category("W NK PRO ALPHA BRA") == "apparel"
    assert catalogue_alerts.classify_product_category("Aether Trail - Men's Mid Layer") == "apparel"
    assert catalogue_alerts.classify_product_category("Aether Water Bottle") == "other"

    assert catalogue_alerts.classify_product_gender("Unisex Running Shoes") == "mens"
    assert catalogue_alerts.classify_product_gender("Aether Men's Jacket") == "mens"
    assert catalogue_alerts.classify_product_gender("W NK PRO ALPHA BRA") == "womens"
    assert catalogue_alerts.classify_product_gender("Aether Big Kids' Shoes") == "kids"


def product(name):
    slug = name.lower().replace(" ", "-").replace("'", "")
    return {
        "product": name,
        "link": f"https://example.com/{slug}",
        "style_codes": ["TEST-001"],
    }


def test_scrape_command_rejects_wrong_channel(monkeypatch):
    to_thread = AsyncMock(return_value=SAMPLE_PRODUCTS)
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx(channel_id=123)

    run_scrape_command(ctx)

    ctx.send.assert_awaited_once_with(
        f"This command can only be used in the <#{catalogue_alerts.CHANNEL_ID}> channel."
    )
    to_thread.assert_not_awaited()


def test_scrape_command_handles_scraper_failure(monkeypatch):
    to_thread = AsyncMock(return_value=None)
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx()

    run_scrape_command(ctx)

    assert [call.args[0] for call in ctx.send.await_args_list] == [
        "Running catalogue scraper...",
        "Failed to scrape catalogue.",
    ]


def test_scrape_command_handles_no_new_products(monkeypatch):
    to_thread = AsyncMock(return_value=[])
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx()

    run_scrape_command(ctx)

    assert [call.args[0] for call in ctx.send.await_args_list] == [
        "Running catalogue scraper...",
        "No new products or price changes found.",
    ]


def test_scrape_command_sends_new_product_messages(monkeypatch):
    to_thread = AsyncMock(return_value=SAMPLE_PRODUCTS)
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx()

    run_scrape_command(ctx)

    sent_messages = [call.args[0] for call in ctx.send.await_args_list]
    assert sent_messages[0] == "Running catalogue scraper..."
    assert sent_messages[1] == catalogue_alerts.build_message(SAMPLE_PRODUCTS)[0]


def test_build_message_includes_catalogue_price_when_present():
    message = "".join(catalogue_alerts.build_message([{**SAMPLE_PRODUCTS[0], "price": "$120.00"}]))

    assert "Price: $120.00" in message


def test_price_changes_appear_after_all_new_product_categories():
    changed = {**product("Changed Hoodie"), "change_type": "price_change", "old_price": "$100", "price": "$70"}
    messages = catalogue_alerts.build_message(
        [changed, product("New Shoe"), product("New Hoodie"), product("New Bottle")],
        limit=250,
    )
    text = "".join(messages)
    assert text.index("Price changes") > text.index("New Bottle")
    assert text.count("Changed Hoodie") == 1
    assert "Old price: $100 → New price: $70" in text
    assert all(len(message) <= 250 for message in messages)


def test_price_only_scan_posts_changes(monkeypatch):
    changed = {**product("Changed Hoodie"), "change_type": "price_change", "old_price": "$100", "price": "$70"}
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", AsyncMock(return_value=[changed]))
    ctx = make_ctx()
    run_scrape_command(ctx)
    text = "".join(call.args[0] for call in ctx.send.await_args_list)
    assert "Price changes" in text
    assert "New products found" not in text
    assert "Old price: $100 → New price: $70" in text


def test_build_check_messages_only_formats_profitable_candidates():
    result = CatalogueCheckResult(
        discount_percent=30,
        scanned_style_codes=4,
        candidates=[
            CatalogueProfitCandidate(
                product="Aether Test Shoe",
                link="https://example.com/test",
                style_code="TEST-001",
                listed_price=100,
                discounted_price=79.1,
                best_size="10",
                avg_sale=160,
                net_avg_sale=142.4,
                highest_bid=150,
                net_highest_bid=133.5,
                estimated_profit=63.3,
                profit_source="avg sale",
                stockx_link="https://stockx.com/demo-test-shoe",
            )
        ],
    )

    message = "".join(catalogue_alerts.build_check_messages(result))

    assert "1 styles estimated over $30 profit from 4 checked" in message
    assert "TEST-001" in message
    assert "$63.30" in message
    assert "$79.10" in message
    assert "$100.00" in message
    assert "[Catalogue](https://example.com/test)" in message
    assert "[StockX](https://stockx.com/demo-test-shoe)" in message


def test_check_command_runs_saved_catalogue_check(monkeypatch):
    result = CatalogueCheckResult(
        discount_percent=30,
        scanned_style_codes=1,
        candidates=[
            CatalogueProfitCandidate(
                product="Aether Test Shoe",
                link="",
                style_code="TEST-001",
                listed_price=100,
                discounted_price=79.1,
                best_size="10",
                avg_sale=160,
                net_avg_sale=142.4,
                highest_bid=150,
                net_highest_bid=133.5,
                estimated_profit=63.3,
                profit_source="avg sale",
            )
        ],
    )
    to_thread = AsyncMock(return_value=result)
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx()

    run_check_command(ctx)

    assert ctx.send.await_args_list[0].args[0].startswith("Checking the saved catalogue at **30% off**")
    assert "TEST-001" in ctx.send.await_args_list[1].args[0]
    to_thread.assert_awaited_once_with(catalogue_alerts.check_catalogue, 30.0)


def test_check_command_uses_original_price_when_discount_is_omitted(monkeypatch):
    result = CatalogueCheckResult(discount_percent=0, scanned_style_codes=1)
    to_thread = AsyncMock(return_value=result)
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx()

    run_check_without_discount(ctx)

    assert "**original price**" in ctx.send.await_args_list[0].args[0]
    assert "original price" in ctx.send.await_args_list[1].args[0]
    to_thread.assert_awaited_once_with(catalogue_alerts.check_catalogue, 0.0)


def test_check_command_reports_stockx_skips(monkeypatch):
    result = CatalogueCheckResult(
        discount_percent=30,
        scanned_style_codes=2,
        stockx_error_style_codes=["TIMEOUT-001"],
    )
    to_thread = AsyncMock(return_value=result)
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx()

    run_check_command(ctx)

    assert "1 style skipped after StockX request retries" in ctx.send.await_args_list[1].args[0]


def test_check_command_rejects_wrong_channel(monkeypatch):
    to_thread = AsyncMock()
    monkeypatch.setattr(catalogue_alerts.asyncio, "to_thread", to_thread)
    ctx = make_ctx(channel_id=123)

    run_check_command(ctx)

    ctx.send.assert_awaited_once_with(
        f"This command can only be used in the <#{catalogue_alerts.CHANNEL_ID}> channel."
    )
    to_thread.assert_not_awaited()
