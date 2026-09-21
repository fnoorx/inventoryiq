"""Scheduled and on-demand StockX order sync with Discord embed reports."""

import asyncio
import os

import discord
from discord.ext import commands, tasks

from services.stockx_order_sheet_sync import stockx_search_url, sync_stockx_payout_ready_orders
from utils.channels import channel_id, in_channel
from utils.formatting import format_money
from utils.stockx_api import ensure_valid_token

CHANNEL_ID = channel_id("STOCKX_SYNC_CHANNEL_ID")
DEFAULT_POLL_INTERVAL_MINUTES = 3 * 60
SUMMARY_COLOR = 0x2ECC71
WARNING_COLOR = 0xF1C40F
ERROR_COLOR = 0xE74C3C
FIELD_LIMIT = 1024
MAX_FIELDS_PER_EMBED = 25


class StockxSync(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._sync_lock = asyncio.Lock()

    def start_polling(self):
        self.stockx_sync_poll.change_interval(minutes=poll_interval_minutes())
        if not self.stockx_sync_poll.is_running():
            self.stockx_sync_poll.start()

    def cog_unload(self):
        self.stockx_sync_poll.cancel()

    @tasks.loop(minutes=DEFAULT_POLL_INTERVAL_MINUTES)
    async def stockx_sync_poll(self):
        await self.bot.wait_until_ready()
        channel = await get_sync_channel(self.bot)
        if channel is None:
            print(f"StockX sync channel {CHANNEL_ID} was not found.")
            return

        await self._run_and_send_sync(
            channel,
            announce_start=False,
            notify_no_changes=False,
        )

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        if message.channel.id != CHANNEL_ID:
            return

        if message.content.strip().lower() != "sync":
            return

        await self._run_and_send_sync(message.channel)

    @commands.command(name="sync")
    async def sync(self, ctx):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        await self._run_and_send_sync(ctx)

    async def _run_and_send_sync(
        self,
        destination,
        announce_start: bool = True,
        notify_no_changes: bool = True,
    ):
        if self._sync_lock.locked():
            if announce_start:
                await destination.send("StockX order sync is already running.")
            return

        async with self._sync_lock:
            if announce_start:
                await destination.send("Running StockX order sync...")
            try:
                result = await asyncio.to_thread(run_stockx_sync)
            except Exception as exc:
                await destination.send(f"StockX order sync failed: {exc}")
                return

            if not notify_no_changes and not should_notify(result):
                return

            for embed in build_sync_embeds(result):
                await destination.send(embed=embed)


def run_stockx_sync():
    ensure_valid_token()
    return sync_stockx_payout_ready_orders()


def poll_interval_minutes() -> float:
    return parse_poll_interval_minutes(os.getenv("STOCKX_SYNC_POLL_INTERVAL_MINUTES"))


def parse_poll_interval_minutes(value) -> float:
    if value is None or str(value).strip() == "":
        return DEFAULT_POLL_INTERVAL_MINUTES

    try:
        interval = float(value)
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_MINUTES

    if interval <= 0:
        return DEFAULT_POLL_INTERVAL_MINUTES
    return interval


async def get_sync_channel(bot):
    channel = bot.get_channel(CHANNEL_ID)
    if channel is not None:
        return channel

    try:
        return await bot.fetch_channel(CHANNEL_ID)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        return None


def should_notify(result) -> bool:
    return bool(result.updated or result.unmatched or result.missing_data)


def build_sync_embeds(result) -> list[discord.Embed]:
    embeds = [build_summary_embed(result)]

    if result.updated:
        embeds.extend(build_updated_embeds(result.updated))

    if result.unmatched:
        embeds.extend(build_list_embeds("Unmatched Orders", result.unmatched, format_unmatched, WARNING_COLOR))

    if result.missing_data:
        embeds.extend(build_list_embeds("Missing Data", result.missing_data, format_missing_data, ERROR_COLOR))

    return embeds


def build_summary_embed(result) -> discord.Embed:
    color = SUMMARY_COLOR
    if result.missing_data:
        color = ERROR_COLOR
    elif result.unmatched:
        color = WARNING_COLOR

    embed = discord.Embed(
        title="StockX Order Sync",
        description="Sync complete.",
        color=color,
    )
    embed.add_field(name="Updated", value=str(len(result.updated)), inline=True)
    embed.add_field(name="Unmatched", value=str(len(result.unmatched)), inline=True)
    embed.add_field(name="Missing Data", value=str(len(result.missing_data)), inline=True)
    embed.add_field(name="Already Processed", value=str(len(result.already_processed)), inline=True)

    if not result.updated and not result.unmatched and not result.missing_data:
        embed.add_field(name="Result", value="No sheet rows needed changes.", inline=False)

    return embed


def build_updated_embeds(updates) -> list[discord.Embed]:
    embeds = []
    current = discord.Embed(title="Updated Rows", color=SUMMARY_COLOR)

    for update in updates:
        if len(current.fields) >= MAX_FIELDS_PER_EMBED:
            embeds.append(current)
            current = discord.Embed(title="Updated Rows", color=SUMMARY_COLOR)

        current.add_field(
            name=updated_field_name(update),
            value=updated_field_value(update),
            inline=False,
        )

    embeds.append(current)
    return embeds


def updated_field_name(update) -> str:
    prefix = f"{update.inventory_id} | " if update.inventory_id else ""
    return f"{prefix}Row {update.row_number} | {update.style_code} size {update.size}"


def updated_field_value(update) -> str:
    product_name = update.product_name or update.style_code
    product_text = f"[{product_name}]({update.stockx_url})" if update.stockx_url else product_name
    return "\n".join(
        [
            product_text,
            f"Payout: {format_money(update.payout)} | Profit: {format_money(update.profit)}",
            f"Platform: {update.platform} | Status: {update.status}",
            f"Sale date: {update.sale_date}",
        ]
    )


def format_unmatched(item: dict) -> str:
    style_code = item.get("style_code") or "Unknown style"
    size = item.get("size") or "Unknown size"
    url = stockx_search_url(style_code)
    style_text = f"[{style_code}]({url})" if url else style_code
    order_number = item.get("order_number")
    order_text = f" | order {order_number}" if order_number else ""
    return f"{style_text} size {size}{order_text}"


def format_missing_data(item: dict) -> str:
    order_number = item.get("order_number") or "Unknown order"
    reason = item.get("reason") or "missing required fields"
    return f"- {order_number}: {reason}"


def build_list_embeds(title: str, items: list[dict], formatter, color: int) -> list[discord.Embed]:
    embeds = []
    current = discord.Embed(title=title, color=color)
    current_lines = []

    for item in items:
        line = formatter(item)
        candidate_lines = current_lines + [line]
        if current_lines and len("\n".join(candidate_lines)) > FIELD_LIMIT:
            current.add_field(name="Items", value="\n".join(current_lines), inline=False)
            current_lines = [line]
            if len(current.fields) >= MAX_FIELDS_PER_EMBED:
                embeds.append(current)
                current = discord.Embed(title=title, color=color)
        else:
            current_lines = candidate_lines

    if current_lines:
        current.add_field(name="Items", value="\n".join(current_lines), inline=False)
    embeds.append(current)

    return embeds


async def setup(bot):
    cog = StockxSync(bot)
    await bot.add_cog(cog)
    cog.start_polling()
