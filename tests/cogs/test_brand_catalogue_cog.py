import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs import brand_catalogue


def make_ctx(channel_id=123):
    return SimpleNamespace(channel=SimpleNamespace(id=channel_id), send=AsyncMock())


def run_command(ctx, category=""):
    cog = brand_catalogue.BrandCatalogue(bot=object())
    callback = brand_catalogue.BrandCatalogue.brand.callback
    asyncio.run(callback(cog, ctx, category))


def test_brand_command_accepts_blank_channel_configuration(monkeypatch):
    result = SimpleNamespace(
        scanned_style_codes=1,
        candidates=[],
        stockx_error_style_codes=[],
    )
    scan = SimpleNamespace(result=result)
    to_thread = AsyncMock(return_value=scan)
    monkeypatch.setattr(brand_catalogue, "CHANNEL_ID", "")
    monkeypatch.setattr(brand_catalogue.asyncio, "to_thread", to_thread)

    ctx = make_ctx()
    run_command(ctx)

    assert "No brand catalogue apparel and footwear styles" in ctx.send.await_args_list[1].args[0]
    to_thread.assert_awaited_once_with(brand_catalogue.scan_brand_catalogue, None)


def test_brand_command_accepts_configured_channel(monkeypatch):
    result = SimpleNamespace(
        scanned_style_codes=0,
        candidates=[],
        stockx_error_style_codes=[],
    )
    to_thread = AsyncMock(return_value=SimpleNamespace(result=result))
    monkeypatch.setattr(brand_catalogue.asyncio, "to_thread", to_thread)

    ctx = make_ctx(channel_id=brand_catalogue.CHANNEL_ID)
    run_command(ctx)

    assert "wrong channel" not in " ".join(
        call.args[0].lower() for call in ctx.send.await_args_list
    )
    to_thread.assert_awaited_once_with(brand_catalogue.scan_brand_catalogue, None)


def test_brand_command_rejects_different_channel(monkeypatch):
    to_thread = AsyncMock()
    monkeypatch.setattr(brand_catalogue.asyncio, "to_thread", to_thread)

    ctx = make_ctx(channel_id=123)
    run_command(ctx)

    ctx.send.assert_awaited_once_with(
        f"This command can only be used in the <#{brand_catalogue.CHANNEL_ID}> channel."
    )
    to_thread.assert_not_awaited()


def test_brand_command_passes_category_filter(monkeypatch):
    result = SimpleNamespace(
        scanned_style_codes=0,
        candidates=[],
        stockx_error_style_codes=[],
    )
    to_thread = AsyncMock(return_value=SimpleNamespace(result=result))
    monkeypatch.setattr(brand_catalogue, "CHANNEL_ID", "")
    monkeypatch.setattr(brand_catalogue.asyncio, "to_thread", to_thread)

    ctx = make_ctx()
    run_command(ctx, "footwear")

    assert "No priced brand catalogue footwear style codes" in ctx.send.await_args_list[1].args[0]
    to_thread.assert_awaited_once_with(brand_catalogue.scan_brand_catalogue, "footwear")


def test_brand_command_rejects_unknown_category(monkeypatch):
    to_thread = AsyncMock()
    monkeypatch.setattr(brand_catalogue, "CHANNEL_ID", "")
    monkeypatch.setattr(brand_catalogue.asyncio, "to_thread", to_thread)

    ctx = make_ctx()
    run_command(ctx, "accessories")

    assert "Category must be" in ctx.send.await_args_list[0].args[0]
    to_thread.assert_not_awaited()
