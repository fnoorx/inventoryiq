"""Catalogue scrape alerts and saved-snapshot profitability checks."""

import asyncio

from discord.ext import commands

from services.catalogue_profitability import check_catalogue, validate_discount_percent
from services.product_classification import (
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    GENDER_LABELS,
    GENDER_ORDER,
    classify_product_category,
    classify_product_gender,
)
from services.scrape_catalogue import main as scrape
from utils.channels import channel_id, in_channel
from utils.formatting import format_money

CHANNEL_ID = channel_id("CATALOGUE_CHANNEL_ID")


class CatalogueAlerts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._check_lock = asyncio.Lock()

    @commands.command()
    async def scrape(self, ctx):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        await ctx.send("Running catalogue scraper...")

        data = await asyncio.to_thread(scrape)
        if data is None:
            await ctx.send("Failed to scrape catalogue.")
            return
        if not data:
            await ctx.send("No new products or price changes found.")
            return

        for message in build_message(data):
            await ctx.send(message)

    @commands.command(name="check")
    async def check(self, ctx, discount: str = "0"):
        """Check every priced style in the latest saved catalogue snapshot."""

        if not await in_channel(ctx, CHANNEL_ID):
            return

        try:
            discount_percent = validate_discount_percent(discount)
        except ValueError as exc:
            await ctx.send(str(exc))
            return

        if self._check_lock.locked():
            await ctx.send("A catalogue profitability check is already running.")
            return

        async with self._check_lock:
            await ctx.send(
                f"Checking the saved catalogue at **{format_check_price(discount_percent)}**. "
                "This can take a while because StockX requests are rate-limited."
            )
            try:
                result = await asyncio.to_thread(check_catalogue, discount_percent)
            except Exception as exc:
                print(f"Catalogue profitability check failed: {exc}")
                await ctx.send(f"Catalogue profitability check failed: {exc}")
                return

            if result.scanned_style_codes == 0:
                await ctx.send(
                    "No saved catalogue products with both a style code and price were found. "
                    "Run `!scrape` after price extraction is available."
                )
                return

            if not result.candidates:
                await ctx.send(
                    f"No catalogue styles are estimated to make over $30 at "
                    f"{format_check_price(result.discount_percent)}. "
                    f"Checked {result.scanned_style_codes} style codes"
                    f"{format_stockx_skip_summary(result)}."
                )
                return

            for message in build_check_messages(result):
                await ctx.send(message)
            if result.stockx_error_style_codes:
                await ctx.send(format_stockx_retry_hint(result, "!check"))

    @check.error
    async def check_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Usage: `!check <discount_percent>` — for example, `!check 30`.")
            return
        raise error

def build_message(data: list[dict], limit: int = 1950) -> list[str]:
    new_products = [product for product in data if product.get("change_type") != "price_change"]
    price_changes = [product for product in data if product.get("change_type") == "price_change"]
    blocks = ["**New products found:**"] if new_products else []
    current_category = None
    current_gender = None

    for product in sorted_products(new_products):
        category = classify_product_category(product.get("product"))
        gender = classify_product_gender(product.get("product"))
        if category != current_category:
            blocks.append(f"__**{CATEGORY_LABELS[category]}**__")
            current_category = category
            current_gender = None
        if gender != current_gender:
            blocks.append(f"**{GENDER_LABELS[gender]}**")
            current_gender = gender

        name = str(product.get("product") or "Unknown product").strip()
        style_codes = ", ".join(product.get("style_codes") or []) or "No style code"
        price = str(product.get("price") or "").strip()
        link = str(product.get("link") or "").strip()
        product_lines = [f"**Item: {name}**", style_codes]
        if price:
            product_lines.append(f"Price: {price}")
        if link:
            product_lines.append(link)
        blocks.append("\n".join(product_lines))

    if price_changes:
        blocks.append("__**Price changes**__")
        for product in sorted(price_changes, key=lambda row: str(row.get("product") or "").casefold()):
            name = str(product.get("product") or "Unknown product").strip()
            codes = ", ".join(product.get("style_codes") or []) or "No style code"
            lines = [
                f"**Item: {name}**",
                codes,
                f"Old price: {product['old_price']} → New price: {product['price']}",
            ]
            if product.get("link"):
                lines.append(product["link"])
            blocks.append("\n".join(lines))

    messages = []
    message = ""
    for block in blocks:
        candidate = f"{message}\n{block}" if message else block
        if message and len(candidate) + 1 > limit:
            messages.append(message + "\n")
            message = block
        else:
            message = candidate

    if message:
        messages.append(message + "\n")

    return messages


def build_check_messages(
    result,
    limit: int = 1950,
    *,
    title: str = "Catalogue profitability",
    minimum_profit: float = 30,
    source_label: str = "Catalogue",
) -> list[str]:
    """Format only profitable catalogue candidates for Discord."""

    blocks = [
        (
            f"**{title} — {format_discount(result.discount_percent)} off**\n"
            f"{len(result.candidates)} styles estimated over ${float(minimum_profit):g} profit "
            f"from {result.scanned_style_codes} checked."
        )
    ]
    for candidate in result.candidates:
        name = candidate.product or candidate.style_code
        product_links = []
        if candidate.link:
            product_links.append(f"[{source_label}]({candidate.link})")
        if candidate.stockx_link:
            product_links.append(f"[StockX]({candidate.stockx_link})")
        blocks.append(
            "\n".join(
                line
                for line in [
                    f"**{name}**",
                    f"`{candidate.style_code}` | Best StockX size: `{candidate.best_size}`",
                    " | ".join(product_links),
                    (
                        f"Listed: {format_money(candidate.listed_price)} | "
                        f"{format_check_cost(result.discount_percent)}: "
                        f"{format_money(candidate.discounted_price)}"
                    ),
                    (
                        f"Net avg: {format_money(candidate.net_avg_sale)} | "
                        f"Net bid: {format_money(candidate.net_highest_bid)}"
                    ),
                    (
                        f"**Estimated profit: {format_money(candidate.estimated_profit)}** "
                        f"via {candidate.profit_source}"
                    ),
                ]
                if line
            )
        )

    messages = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def format_discount(value: float) -> str:
    return f"{float(value):g}%"


def format_check_price(discount_percent: float) -> str:
    return (
        "original price"
        if float(discount_percent) == 0
        else f"{format_discount(discount_percent)} off"
    )


def format_check_cost(discount_percent: float) -> str:
    return (
        "Original + tax cost"
        if float(discount_percent) == 0
        else f"{format_discount(discount_percent)} + tax cost"
    )


def format_stockx_skip_summary(result) -> str:
    skipped = len(result.stockx_error_style_codes)
    if skipped == 0:
        return ""
    return f"; {skipped} {plural(skipped, 'style')} skipped after StockX request retries"


def format_stockx_retry_hint(result, command: str) -> str:
    skipped = len(result.stockx_error_style_codes)
    return (
        f"Skipped {skipped} {plural(skipped, 'style')} after StockX request retries. "
        f"Run `{command}` again later to retry them."
    )


def plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def sorted_products(data: list[dict]) -> list[dict]:
    category_rank = {value: index for index, value in enumerate(CATEGORY_ORDER)}
    gender_rank = {value: index for index, value in enumerate(GENDER_ORDER)}
    return sorted(
        data,
        key=lambda product: (
            category_rank[classify_product_category(product.get("product"))],
            gender_rank[classify_product_gender(product.get("product"))],
            str(product.get("product") or "").casefold(),
            str(product.get("link") or "").casefold(),
        ),
    )


async def setup(bot):
    await bot.add_cog(CatalogueAlerts(bot))
