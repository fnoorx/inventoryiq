"""Discord command for scanning the public brand catalogue."""

import asyncio

from discord.ext import commands

from cogs.catalogue_alerts import (
    build_check_messages,
    format_stockx_retry_hint,
    format_stockx_skip_summary,
)
from services.brand_catalogue import (
    DISCOUNT_PERCENT,
    MINIMUM_PROFIT,
    normalize_category,
    scan_brand_catalogue,
)
from utils.channels import channel_id, in_channel

CHANNEL_ID = channel_id("BRAND_CATALOGUE_CHANNEL_ID")


class BrandCatalogue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._scan_lock = asyncio.Lock()

    @commands.command(name="brand")
    async def brand(self, ctx, category: str = ""):
        """Scan brand catalogue apparel, footwear, or both when no category is given."""

        if not await in_channel(ctx, CHANNEL_ID):
            return

        try:
            normalized_category = normalize_category(category)
        except ValueError as exc:
            await ctx.send(f"{exc} Usage: `!brand`, `!brand apparel`, or `!brand footwear`.")
            return

        if self._scan_lock.locked():
            await ctx.send("A brand catalogue scan is already running.")
            return

        category_label = normalized_category or "apparel and footwear"
        async with self._scan_lock:
            await ctx.send(
                f"Scanning all brand catalogue {category_label} products and checking StockX profitability. "
                "This can take a while because the catalog and StockX requests are large."
            )
            try:
                scan = await asyncio.to_thread(scan_brand_catalogue, normalized_category)
            except Exception as exc:
                print(f"Brand catalogue scan failed: {exc}")
                await ctx.send(f"Brand catalogue scan failed: {exc}")
                return

            result = scan.result
            if result.scanned_style_codes == 0:
                await ctx.send(
                    f"No priced brand catalogue {category_label} style codes were found."
                )
                return

            if not result.candidates:
                await ctx.send(
                    f"No brand catalogue {category_label} styles are estimated to make over ${MINIMUM_PROFIT:g} CAD "
                    f"at {DISCOUNT_PERCENT:g}% off plus tax. Checked {result.scanned_style_codes} style codes"
                    f"{format_stockx_skip_summary(result)}."
                )
                return

            for message in build_check_messages(
                result,
                title="Brand catalogue profitability",
                minimum_profit=MINIMUM_PROFIT,
                source_label="Brand catalogue",
            ):
                await ctx.send(message)
            if result.stockx_error_style_codes:
                await ctx.send(format_stockx_retry_hint(result, "!brand"))

    @brand.error
    async def brand_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send("Usage: `!brand`, `!brand apparel`, or `!brand footwear`.")
            return
        raise error


async def setup(bot):
    await bot.add_cog(BrandCatalogue(bot))
