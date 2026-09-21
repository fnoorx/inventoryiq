"""Discord command and persistent review actions for shoe-label photo intake."""

from __future__ import annotations

import asyncio
import os
import re

import discord
from discord.ext import commands

from cogs.inventory import (
    CHANNEL_ID,
    DEFAULT_LOCATION_ENV_VAR,
    format_purchase_date,
    parse_price,
)
from cogs.market_data import build_message as build_market_messages
from services.label_intake import (
    LabelIntakeResult,
    calculate_market_estimate,
    cancel_label_scan,
    confirm_label_scan,
    edit_label_scan,
    process_label_scan,
)
from services.label_preprocessing import (
    ImageValidationError,
    configured_max_image_bytes,
)
from services.label_scan_repository import LabelScanRepository
from services.stockx import get_product_market_data
from utils.channels import in_channel
from utils.formatting import format_money, format_percent


PHOTO_USAGE = "Attach one label image and use `!photo` or `!photo 180`."
SCAN_FOOTER_PATTERN = re.compile(r"\b(SCAN-[A-F0-9]{16})\b")
STATUS_COLORS = {
    "verified": 0x2ECC71,
    "review_required": 0xF1C40F,
    "conflict": 0xE74C3C,
    "confirmed": 0x3498DB,
    "cancelled": 0x7F8C8D,
    "failed": 0xE74C3C,
}


class PhotoIntake(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="photo")
    async def photo(self, ctx, price: str | None = None):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        attachments = list(getattr(getattr(ctx, "message", None), "attachments", []) or [])
        if not attachments:
            await ctx.send(PHOTO_USAGE)
            return
        if len(attachments) != 1:
            await ctx.send("Attach exactly one shoe-label image per `!photo` command.")
            return

        try:
            supplied_price = parse_price(price) if price is not None else None
            location = str(os.getenv(DEFAULT_LOCATION_ENV_VAR) or "").strip()
            if not location:
                raise ValueError(
                    f"Set `{DEFAULT_LOCATION_ENV_VAR}` in `.env` before using `!photo`."
                )
            attachment = attachments[0]
            maximum = configured_max_image_bytes()
            attachment_size = getattr(attachment, "size", None)
            if attachment_size is not None and attachment_size > maximum:
                raise ImageValidationError(
                    f"The image is too large. Maximum size is {maximum / (1024 * 1024):.1f} MB."
                )
            image_bytes = await attachment.read()
            result = await asyncio.to_thread(
                process_label_scan,
                discord_message_id=ctx.message.id,
                discord_attachment_id=attachment.id,
                image_bytes=image_bytes,
                filename=getattr(attachment, "filename", None),
                content_type=getattr(attachment, "content_type", None),
                supplied_price=supplied_price,
                location=location,
            )
        except (ValueError, ImageValidationError) as exc:
            await ctx.send(str(exc))
            return
        except Exception as exc:
            print(f"Photo-label intake failed: {exc}")
            await ctx.send(f"Photo-label intake failed: {exc}")
            return

        await ctx.send(embed=build_photo_embed(result), view=PhotoIntakeView(self))

    @photo.error
    async def photo_error(self, ctx, error):
        if isinstance(error, commands.TooManyArguments):
            await ctx.send(PHOTO_USAGE)
            return
        raise error

    async def add_scan_to_inventory(self, interaction: discord.Interaction) -> None:
        scan_id = scan_id_from_interaction(interaction)
        if not scan_id:
            await interaction.response.send_message(
                "This preview has no valid scan ID.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            creation = await asyncio.to_thread(confirm_label_scan, scan_id)
        except Exception as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if creation.sheet_synced:
            action = "Added" if creation.created else "Already added"
            message = f"{action} as `{creation.item.inventory_id}`."
        else:
            message = (
                f"Created `{creation.item.inventory_id}` in SQLite, but its Sheet sync is pending. "
                "Use `!inventoryretry`; confirmation will not create another ID."
            )
        await interaction.followup.send(message, ephemeral=True)
        await refresh_preview_message(interaction, scan_id)

    async def research_all_sizes(self, interaction: discord.Interaction) -> None:
        scan_id = scan_id_from_interaction(interaction)
        if not scan_id:
            await interaction.response.send_message(
                "This preview has no valid scan ID.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        scan = await asyncio.to_thread(LabelScanRepository().get, scan_id)
        if scan is None or not scan.stockx_product_id:
            await interaction.followup.send(
                "This scan has no validated StockX product.", ephemeral=True
            )
            return
        market = await asyncio.to_thread(get_product_market_data, scan.stockx_product_id)
        if not market:
            await interaction.followup.send(
                "No all-size market data was returned.", ephemeral=True
            )
            return
        normalized = scan.normalized_extraction or {}
        messages = build_market_messages(
            market,
            product_name=normalized.get("product_name"),
            style_code=normalized.get("style_code"),
            size=None,
        )
        for message in messages:
            await interaction.followup.send(message, ephemeral=True)

    async def open_edit_modal(self, interaction: discord.Interaction) -> None:
        scan_id = scan_id_from_interaction(interaction)
        if not scan_id:
            await interaction.response.send_message(
                "This preview has no valid scan ID.", ephemeral=True
            )
            return
        scan = await asyncio.to_thread(LabelScanRepository().get, scan_id)
        if scan is None:
            await interaction.response.send_message("Scan not found.", ephemeral=True)
            return
        await interaction.response.send_modal(PhotoEditModal(self, scan))

    async def cancel_scan(self, interaction: discord.Interaction) -> None:
        scan_id = scan_id_from_interaction(interaction)
        if not scan_id:
            await interaction.response.send_message(
                "This preview has no valid scan ID.", ephemeral=True
            )
            return
        try:
            scan = await asyncio.to_thread(cancel_label_scan, scan_id)
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        result = LabelIntakeResult(scan=scan, created=False, estimate=None)
        await interaction.response.edit_message(embed=build_photo_embed(result), view=None)


class PhotoIntakeView(discord.ui.View):
    """Review buttons under a scan preview; persistent so they survive bot restarts."""

    def __init__(self, cog: PhotoIntake):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Add to Inventory",
        style=discord.ButtonStyle.success,
        custom_id="photo_intake:add_inventory",
    )
    async def add_inventory(self, interaction: discord.Interaction, _button):
        await self.cog.add_scan_to_inventory(interaction)

    @discord.ui.button(
        label="Research All Sizes",
        style=discord.ButtonStyle.primary,
        custom_id="photo_intake:all_sizes",
    )
    async def all_sizes(self, interaction: discord.Interaction, _button):
        await self.cog.research_all_sizes(interaction)

    @discord.ui.button(
        label="Edit",
        style=discord.ButtonStyle.secondary,
        custom_id="photo_intake:edit",
    )
    async def edit(self, interaction: discord.Interaction, _button):
        await self.cog.open_edit_modal(interaction)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
        custom_id="photo_intake:cancel",
    )
    async def cancel(self, interaction: discord.Interaction, _button):
        await self.cog.cancel_scan(interaction)


class PhotoEditModal(discord.ui.Modal, title="Edit label intake draft"):
    style_code = discord.ui.TextInput(label="Style code", max_length=32)
    size = discord.ui.TextInput(label="StockX size", max_length=20)
    price = discord.ui.TextInput(
        label="Price paid subtotal (optional)", required=False, max_length=20
    )
    location = discord.ui.TextInput(label="Location", max_length=50)
    purchase_date = discord.ui.TextInput(
        label="Purchase date", placeholder="today or MM/DD/YYYY", max_length=20
    )

    def __init__(self, cog: PhotoIntake, scan):
        super().__init__(timeout=300)
        self.cog = cog
        self.scan_id = scan.scan_id
        normalized = scan.normalized_extraction or {}
        self.style_code.default = str(normalized.get("style_code") or "")
        self.size.default = str(normalized.get("selected_size") or "")
        self.price.default = "" if scan.supplied_price is None else str(scan.supplied_price)
        self.location.default = scan.location
        self.purchase_date.default = scan.purchase_date

    async def on_submit(self, interaction: discord.Interaction):
        try:
            supplied_price = parse_price(str(self.price)) if str(self.price).strip() else None
            result = await asyncio.to_thread(
                edit_label_scan,
                self.scan_id,
                style_code=str(self.style_code),
                size=str(self.size),
                supplied_price=supplied_price,
                location=str(self.location).strip(),
                purchase_date=format_purchase_date(str(self.purchase_date)),
            )
        except Exception as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=build_photo_embed(result), view=PhotoIntakeView(self.cog)
        )


def build_photo_embed(result: LabelIntakeResult) -> discord.Embed:
    scan = result.scan
    normalized = scan.normalized_extraction or {}
    status = scan.validation_status or scan.state
    embed = discord.Embed(
        title=normalized.get("product_name") or "Shoe Label Research",
        description=f"Validation: **{status.replace('_', ' ').title()}**",
        color=STATUS_COLORS.get(status, 0x95A5A6),
    )
    embed.add_field(
        name="Product",
        value=(
            f"Style: `{normalized.get('style_code') or 'Unreadable'}`\n"
            f"StockX size: `{normalized.get('selected_size') or 'Unresolved'}`\n"
            f"Brand: {normalized.get('brand') or 'N/A'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Printed sizes",
        value=format_printed_sizes(normalized.get("printed_sizes") or []),
        inline=False,
    )
    embed.add_field(
        name="Barcode",
        value=format_barcode(scan),
        inline=False,
    )
    embed.add_field(
        name="Exact StockX market (CAD)",
        value=format_market(scan.market_data),
        inline=False,
    )
    embed.add_field(
        name="Purchase estimate",
        value=format_estimate(result.estimate, scan.supplied_price),
        inline=False,
    )
    if scan.warnings or scan.error_details:
        warning_lines = [*scan.warnings]
        if scan.error_details:
            warning_lines.append(scan.error_details)
        embed.add_field(
            name="Warnings / conflicts",
            value="\n".join(f"• {warning}" for warning in warning_lines)[:1024],
            inline=False,
        )
    embed.set_footer(text=f"Scan ID: {scan.scan_id} • Images are processed in memory, not retained")
    return embed


def format_printed_sizes(sizes: list[dict]) -> str:
    if not sizes:
        return "None readable"
    return " • ".join(
        f"{size.get('system', 'UNKNOWN')} {size.get('value', '?')}"
        for size in sizes
    )[:1024]


def format_barcode(scan) -> str:
    normalized = scan.normalized_extraction or {}
    gtin = normalized.get("gtin")
    if gtin:
        source = normalized.get("source") or "unknown"
        return f"`{gtin}` ({source.replace('_', ' ')})"
    if scan.barcode_results:
        return "Decoded, but no checksum-valid StockX GTIN"
    return "No usable UPC/EAN"


def format_market(market: dict | None) -> str:
    market = market or {}
    fields = [
        ("Average sale", "avg_sales"),
        ("Highest bid", "highest_bid"),
        ("Lowest ask", "lowest_ask"),
        ("Flex ask", "flex_lowest_ask"),
        ("Beat US", "beat_US"),
    ]
    return "\n".join(f"{label}: {format_money(market.get(key))}" for label, key in fields)


def format_estimate(estimate, supplied_price) -> str:
    if supplied_price is None:
        return "Research only — no purchase price supplied."
    if estimate is None:
        return "Estimate unavailable."
    lines = [f"Cost (after tax): {format_money(estimate.total_cost)}"]
    if estimate.estimated_payout is None:
        lines.append("Estimated payout/profit: N/A")
    else:
        lines.extend(
            [
                f"Estimated payout: {format_money(estimate.estimated_payout)} via {estimate.source}",
                f"Estimated profit: {format_money(estimate.estimated_profit)}",
                f"Estimated ROI: {format_percent(estimate.roi)}",
                "Estimate uses current market data and fee assumptions; it is not guaranteed.",
            ]
        )
    return "\n".join(lines)


def scan_id_from_interaction(interaction: discord.Interaction) -> str | None:
    # The scan ID travels in the embed footer, so buttons need no server-side state.
    message = getattr(interaction, "message", None)
    embeds = getattr(message, "embeds", []) or []
    if not embeds:
        return None
    footer = getattr(getattr(embeds[0], "footer", None), "text", "") or ""
    match = SCAN_FOOTER_PATTERN.search(footer)
    return match.group(1) if match else None


async def refresh_preview_message(interaction: discord.Interaction, scan_id: str) -> None:
    message = getattr(interaction, "message", None)
    if message is None:
        return
    scan = await asyncio.to_thread(LabelScanRepository().get, scan_id)
    if scan is None:
        return
    result = LabelIntakeResult(
        scan=scan,
        created=False,
        estimate=calculate_market_estimate(scan.market_data, scan.supplied_price),
    )
    try:
        await message.edit(embed=build_photo_embed(result), view=None)
    except Exception:
        pass


async def setup(bot):
    cog = PhotoIntake(bot)
    await bot.add_cog(cog)
    bot.add_view(PhotoIntakeView(cog))
