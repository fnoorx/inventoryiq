"""Discord command that scrapes launch availability and ranks profitable buys."""

import asyncio

import discord
from discord.ext import commands

from services.launch import build_launch_candidates, rank_candidates, scrape_launch
from utils.channels import channel_id, in_channel
from utils.formatting import format_money, format_percent

CHANNEL_ID = channel_id("LAUNCH_CHANNEL_ID")
PROFIT_COLOR = 0x2ECC71


class Launch(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def launch(self, ctx, budget: float = None):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        if budget is not None and budget <= 0:
            await ctx.send("Budget must be greater than $0.")
            return

        budget_text = f" with budget {format_money(budget)}" if budget is not None else ""
        await ctx.send(f"Checking launch products{budget_text}...")

        checks = await asyncio.to_thread(scrape_launch)

        if not checks:
            await ctx.send("No launch products found with target sizes available.")
            return

        candidates = build_launch_candidates(checks, profitable_only=True)
        if not candidates:
            await ctx.send(
                f"Checked {len(checks)} launch target-size products. "
                "No profitable products found."
            )
            return

        ranking = rank_candidates(candidates, budget=budget)
        if not ranking.candidates:
            if budget is None:
                await ctx.send("No profitable products had enough price/profit data to rank.")
                return
            await ctx.send(
                f"Found {len(candidates)} profitable launch products, "
                f"but none fit within budget {format_money(budget)}."
            )
            return

        await ctx.send(
            _ranking_summary(
                ranking,
                checked_count=len(checks),
                candidate_count=len(candidates),
            )
        )
        for embed in build_embeds(ranking.candidates):
            await ctx.send(embed=embed)

    @launch.error
    async def launch_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("Use `!launch` or `!launch 500`.")
            return
        raise error


def build_embeds(candidates):
    return [build_candidate_embed(candidate) for candidate in candidates]


def _ranking_summary(ranking, checked_count, candidate_count):
    if ranking.budget is None:
        return (
            f"Found {candidate_count} profitable launch products "
            f"from {checked_count} target-size products. Ranked by ROI.\n"
            f"Total real price: {format_money(ranking.total_real_price)} | "
            f"Projected profit: {format_money(ranking.total_profit)}"
        )

    return (
        f"Found {candidate_count} profitable launch products "
        f"from {checked_count} target-size products. "
        f"Budget: {format_money(ranking.budget)} | "
        f"Selected: {len(ranking.candidates)}\n"
        f"Total real price: {format_money(ranking.total_real_price)} | "
        f"Projected profit: {format_money(ranking.total_profit)}"
    )


def build_candidate_embed(candidate):
    product = candidate.product
    size_check = candidate.size_check
    title = product.title

    embed = discord.Embed(
        title=title,
        url=product.url,
        description=_product_description(product),
        color=PROFIT_COLOR,
    )
    embed.add_field(
        name="Retailer",
        value=(
            f"Price: {format_money(product.price)}\n"
            f"Real: {format_money(product.real_price)}\n"
            f"Selected Size: {size_check.size}\n"
            f"Available Sizes: {', '.join(product.available_sizes)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Best Market",
        value=(
            f"Source: {size_check.profit_source or 'N/A'}\n"
            f"Avg Net: {format_money(size_check.net_sales)}\n"
            f"Bid Net: {format_money(size_check.net_highest_bid)}\n"
            f"Profit: {format_money(size_check.profit)}\n"
            f"ROI: {format_percent(candidate.roi)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Selected Size Check",
        value=_size_check_text(size_check),
        inline=False,
    )

    if product.image_url:
        embed.set_thumbnail(url=product.image_url)
    return embed


def _product_description(product):
    parts = [product.style_code or "No style code"]
    if product.subtitle:
        parts.append(product.subtitle)
    return " | ".join(parts)


def _size_check_text(size_check):
    flags = []
    # net > real uses StockX avg sale after fees. net bid > real uses the
    # highest bid after fees, because a bid also loses seller fees.
    if size_check.net_sales_above_real_price:
        flags.append("net > real")
    if size_check.net_highest_bid_above_real_price:
        flags.append("net bid > real")

    flag_text = f"\nSignals: {', '.join(flags)}" if flags else ""
    return (
        f"Avg: {format_money(size_check.avg_sales)}\n"
        f"Net Avg: {format_money(size_check.net_sales)}\n"
        f"Bid: {format_money(size_check.highest_bid)}\n"
        f"Net Bid: {format_money(size_check.net_highest_bid)}\n"
        f"Profit: {format_money(size_check.profit)} via "
        f"{size_check.profit_source or 'N/A'}"
        f"{flag_text}"
    )


async def setup(bot):
    await bot.add_cog(Launch(bot))
