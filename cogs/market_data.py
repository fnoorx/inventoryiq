"""Market lookups by style code or inventory ID."""

import asyncio
from dataclasses import dataclass

from discord.ext import commands

from services.inventory_repository import InventoryRepository, is_valid_inventory_id
from services.stockx import get_market_data_details
from utils.channels import channel_id, in_channel
from utils.formatting import format_money

CHANNEL_ID = channel_id("MARKET_CHANNEL_ID")


class MarketData(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        if message.channel.id != CHANNEL_ID:
            return

        if message.content.strip().startswith("!"):
            return

        lookup = parse_lookup_input(message.content)
        if lookup is None:
            await message.channel.send("Usage: `<style_code> [size]` or `<inventory_id>`")
            return

        identifier, size = lookup
        await self._send_market_lookup(message.channel, identifier, size)

    @commands.command()
    async def market(self, ctx, identifier: str, size: str = None):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        await self._send_market_lookup(ctx, identifier, size)

    async def _send_market_lookup(self, destination, identifier: str, size: str | None = None):
        try:
            lookup = await asyncio.to_thread(resolve_market_lookup, identifier, size)
        except ValueError as exc:
            await destination.send(str(exc))
            return

        details = await asyncio.to_thread(
            get_market_data_details,
            lookup.style_code,
            lookup.size,
            lookup.stockx_product_id,
            lookup.product_name,
            lookup.stockx_variant_id,
        )
        if details is None:
            await destination.send(
                f"No exact StockX product or market data was found for `{lookup.style_code}`."
            )
            return

        messages = build_message(
            details.market_data,
            product_name=details.product_name,
            style_code=details.style_code,
            size=details.size,
            inventory_id=lookup.inventory_id,
        )

        for msg in messages:
            await destination.send(msg)

    @market.error
    async def market_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: !market <style_code> [size] or !market <inventory_id>")


@dataclass(frozen=True)
class MarketLookup:
    style_code: str
    size: str | None = None
    inventory_id: str | None = None
    product_name: str | None = None
    stockx_product_id: str | None = None
    stockx_variant_id: str | None = None


def resolve_market_lookup(
    identifier: str,
    size: str | None = None,
    repository: InventoryRepository | None = None,
) -> MarketLookup:
    normalized_identifier = str(identifier).strip().upper()
    if normalized_identifier.startswith("INV-") and not is_valid_inventory_id(
        normalized_identifier
    ):
        raise ValueError("Inventory IDs must use the format `INV-000001`.")
    if not is_valid_inventory_id(normalized_identifier):
        return MarketLookup(style_code=normalized_identifier, size=size)

    if size:
        raise ValueError(
            "Do not provide a size with an inventory ID; the stored unit size is used automatically."
        )

    repository = repository or InventoryRepository()
    item = repository.get(normalized_identifier)
    if item is None:
        raise ValueError(f"Inventory item `{normalized_identifier}` was not found.")
    if not item.style_code or not item.size:
        raise ValueError(
            f"Inventory item `{normalized_identifier}` has no stored style code or size."
        )
    return MarketLookup(
        style_code=item.style_code,
        size=item.size,
        inventory_id=item.inventory_id,
        product_name=item.product_name,
        stockx_product_id=item.stockx_product_id or None,
        stockx_variant_id=item.stockx_variant_id or None,
    )

MARKET_FIELDS = (
    ("avg_sales", "Average Sale"),
    ("highest_bid", "Highest Bid"),
    ("lowest_ask", "Lowest Ask"),
    ("flex_lowest_ask", "Flex"),
    ("beat_US", "Beat US"),
)


def parse_lookup_input(content: str) -> tuple[str, str | None] | None:
    parts = content.strip().split()
    if not parts:
        return None

    style_code = parts[0]
    size = " ".join(parts[1:]) or None
    return style_code, size


def build_message(
    market_data: dict,
    product_name: str | None = None,
    style_code: str | None = None,
    size: str | None = None,
    inventory_id: str | None = None,
    limit: int = 1950,
) -> list[str]:
    data_blocks = [
        _format_market_block(size_key, data)
        for size_key, data in _iter_market_rows(market_data, size=size)
    ]

    if not data_blocks:
        return ["No market data found."]

    blocks = []
    identity = []
    if product_name:
        identity.append(f"Product   {product_name}")
    if style_code:
        identity.append(f"Style     {style_code.upper()}")
    identity.append(f"Size      {size or 'All sizes'}")
    if inventory_id:
        identity.append(f"Inventory {inventory_id}")
    blocks.append(identity)

    blocks.extend(data_blocks)

    return _chunk_code_blocks(blocks, limit=limit)


def _iter_market_rows(market_data: dict, size: str | None = None):
    if _is_market_row(market_data):
        yield size, market_data
        return

    for size_key, data in market_data.items():
        if _is_market_row(data):
            yield size_key, data


def _is_market_row(data: object) -> bool:
    return isinstance(data, dict) and any(key in data for key, _ in MARKET_FIELDS)


def _format_market_block(size_key: str | None, data: dict) -> list[str]:
    lines = []
    if size_key:
        lines.append(f"Size   {size_key}")

    for key, label in MARKET_FIELDS:
        lines.append(f"{label:<14}{format_money(data.get(key), decimals=0)}")

    return lines


def _chunk_code_blocks(blocks: list[list[str]], limit: int) -> list[str]:
    messages = []
    current_lines = []

    for block in blocks:
        candidate = current_lines + ([""] if current_lines else []) + block
        if current_lines and len(_as_code_block(candidate)) > limit:
            messages.append(_as_code_block(current_lines))
            current_lines = block
        else:
            current_lines = candidate

    if current_lines:
        messages.append(_as_code_block(current_lines))

    return messages


def _as_code_block(lines: list[str]) -> str:
    return "```\n" + "\n".join(lines).rstrip() + "\n```"


async def setup(bot):
    await bot.add_cog(MarketData(bot))
