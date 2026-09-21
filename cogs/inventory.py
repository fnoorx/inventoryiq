"""Discord commands for creating and inspecting permanent inventory units."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import os

from discord.ext import commands

from services.inventory_repository import InventoryRepository, InventoryUnitInput
from services.inventory_service import (
    backfill_inventory_ids,
    create_inventory_unit,
    retry_inventory_sheet_sync,
    sheet_item_type,
)
from services.stockx import get_product_id, get_product_name
from services.stockx_gtin_lookup import lookup_gtin, normalize_gtin
from utils.channels import channel_id, in_channel
from utils.text import clean_text

CHANNEL_ID = channel_id("INVENTORY_CHANNEL_ID")
DEFAULT_LOCATION_ENV_VAR = "INVENTORY_DEFAULT_LOCATION"
ADD_USAGE = (
    "Usage:\n"
    "`!add <UPC/GTIN> <price> [discount%]`\n"
    "`!add <location> <date> <UPC/GTIN> <price> [discount%]`\n"
    "`!add <location> <date> <style_code> <size> <price> [discount%]`"
)


class Inventory(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def add(self, ctx, *args: str):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        try:
            location, purchase_date, identifier, size, price = parse_add_arguments(
                args,
                default_location=os.getenv(DEFAULT_LOCATION_ENV_VAR),
            )

            if size is None:
                gtin = validate_gtin(identifier)
                lookup = await asyncio.to_thread(lookup_gtin, gtin)
                if lookup is None:
                    await ctx.send(f"No StockX product variant was found for `{gtin}`.")
                    return
                item_type, style_code, name, size = inventory_fields_from_gtin(lookup)
                stockx_product_id = clean_text(lookup.product_id)
                stockx_variant_id = clean_text(lookup.variant_id)
            else:
                product_id = await asyncio.to_thread(get_product_id, identifier)
                if not product_id:
                    await ctx.send(f"No StockX product was found for `{identifier}`.")
                    return

                product = await asyncio.to_thread(get_product_name, product_id)
                if product is None:
                    await ctx.send(f"StockX product details were unavailable for `{identifier}`.")
                    return
                name, product_type = product
                item_type = sheet_item_type(product_type)
                style_code = identifier.upper()
                stockx_product_id = clean_text(product_id)
                stockx_variant_id = ""

            message_id = getattr(getattr(ctx, "message", None), "id", None)
            if message_id is None:
                raise ValueError("Discord message ID is unavailable; inventory was not created.")

            creation = await asyncio.to_thread(
                create_inventory_unit,
                InventoryUnitInput(
                    location=location,
                    purchase_date=purchase_date,
                    item_type=item_type,
                    style_code=style_code,
                    product_name=name.upper(),
                    size=size,
                    price_paid=price,
                    source_identifier=identifier,
                    stockx_product_id=stockx_product_id,
                    stockx_variant_id=stockx_variant_id,
                ),
                message_id,
            )
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        except Exception as exc:
            print(f"Failed to add inventory item: {exc}")
            await ctx.send(f"Failed to add item to inventory: {exc}")
            return

        if not creation.sheet_synced:
            await ctx.send(
                f"Created `{creation.item.inventory_id}` for **{name.upper()}**, but Google Sheets "
                "sync failed. The SQLite item is preserved and queued for `!inventoryretry`.\n"
                f"Error: {creation.sheet_error}"
            )
            return

        action = "Added" if creation.created else "Already processed"
        await ctx.send(
            f"{action} **{name.upper()}** as `{creation.item.inventory_id}`\n"
            f"`{style_code}` | Size `{size}` | Cost `${creation.item.total_cost:.2f}` | `{location}`"
        )

    @commands.command(name="item")
    async def item(self, ctx, inventory_id: str):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        item = await asyncio.to_thread(get_inventory_unit, inventory_id)
        if item is None:
            await ctx.send(f"Inventory item `{clean_text(inventory_id).upper()}` was not found.")
            return
        await ctx.send(format_inventory_item(item))

    @commands.command(name="inventoryretry")
    async def inventoryretry(self, ctx, inventory_id: str | None = None):
        if not await in_channel(ctx, CHANNEL_ID):
            return

        try:
            results = await asyncio.to_thread(retry_inventory_sheet_sync, inventory_id)
        except Exception as exc:
            await ctx.send(f"Inventory Sheet retry failed: {exc}")
            return

        if not results:
            await ctx.send("No inventory items are waiting for Google Sheets retry.")
            return

        synced = [result for result in results if result.sheet_synced]
        failed = [result for result in results if not result.sheet_synced]
        already_synced = [result for result in synced if result.already_synced]
        await ctx.send(
            f"Inventory Sheet retry complete: {len(synced) - len(already_synced)} synced, "
            f"{len(already_synced)} already synced, {len(failed)} still pending."
        )

    @commands.command(name="backfillinventory")
    async def backfillinventory(self, ctx, confirmation: str = ""):
        if not await in_channel(ctx, CHANNEL_ID):
            return
        if confirmation != "CONFIRM":
            await ctx.send(
                "Backfill makes permanent IDs for existing physical rows. "
                "Run `!backfillinventory CONFIRM` only after reviewing the Sheet and backing it up."
            )
            return

        try:
            result = await asyncio.to_thread(backfill_inventory_ids)
        except Exception as exc:
            await ctx.send(f"Inventory backfill could not start: {exc}")
            return

        await ctx.send(format_backfill_result(result))

    @add.error
    async def add_error(self, ctx, _error):
        await ctx.send(ADD_USAGE)


def parse_add_arguments(args, default_location: str | None = None):
    discount = None
    if args and clean_text(args[-1]).endswith("%"):
        discount_text = clean_text(args[-1])[:-1]
        try:
            discount = Decimal(discount_text)
        except InvalidOperation as exc:
            raise ValueError("Discount must be a percentage from 0% to 100%, such as `30%`.") from exc
        if not discount.is_finite() or not 0 <= discount <= 100:
            raise ValueError("Discount must be a percentage from 0% to 100%, such as `30%`.")
        args = args[:-1]
    if len(args) == 2:
        location = clean_text(default_location)
        if not location:
            raise ValueError(
                f"Set `{DEFAULT_LOCATION_ENV_VAR}` in `.env` to use `!add <UPC/GTIN> <price>`."
            )
        purchase_date_input = "today"
        identifier, price_input = args
        size = None
    elif len(args) == 4:
        location, purchase_date_input, identifier, price_input = args
        size = None
    elif len(args) == 5:
        location, purchase_date_input, identifier, size, price_input = args
    else:
        raise ValueError(ADD_USAGE)

    location = clean_text(location)
    identifier = clean_text(identifier)
    size = clean_text(size) or None
    if not location or not identifier:
        raise ValueError(ADD_USAGE)

    price = parse_price(price_input)
    if discount is not None:
        price = float((Decimal(str(price)) * (1 - discount / 100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        ))

    return (
        location,
        format_purchase_date(purchase_date_input),
        identifier,
        size,
        price,
    )


def format_purchase_date(value: str) -> str:
    text = clean_text(value)
    if text.lower() == "today":
        purchase_date = date.today()
    elif text.lower() == "yesterday":
        purchase_date = date.today() - timedelta(days=1)
    else:
        try:
            purchase_date = datetime.strptime(text, "%m/%d/%Y").date()
        except ValueError as exc:
            raise ValueError("Date must be `today`, `yesterday`, or `MM/DD/YYYY`.") from exc
    return purchase_date.strftime("%m/%d/%Y")


def parse_price(value: str) -> float:
    text = clean_text(value).replace("$", "").replace(",", "")
    try:
        price = float(text)
    except ValueError as exc:
        raise ValueError("Price must be a number, such as `180` or `180.50`.") from exc
    if not math.isfinite(price):
        raise ValueError("Price must be a finite number.")
    if price < 0:
        raise ValueError("Price cannot be negative.")
    return price


def validate_gtin(value: str) -> str:
    gtin = normalize_gtin(value)
    if not gtin.isdigit() or len(gtin) not in {8, 12, 13, 14}:
        raise ValueError("The scanned UPC/GTIN must contain 8, 12, 13, or 14 digits.")
    return gtin


def inventory_fields_from_gtin(lookup):
    fields = {
        "style code": clean_text(lookup.style_id),
        "product name": clean_text(lookup.title),
        "size": clean_text(lookup.normalized_size or lookup.variant_value),
    }
    missing = [label for label, value in fields.items() if not value]
    if missing:
        raise ValueError(f"StockX did not return the required {', '.join(missing)}.")

    return (
        sheet_item_type(lookup.product_type),
        fields["style code"].upper(),
        fields["product name"],
        fields["size"],
    )


def get_inventory_unit(inventory_id: str):
    return InventoryRepository().get(inventory_id)


def format_inventory_item(item) -> str:
    sheet_row = str(item.sheet_row) if item.sheet_row is not None else "Not synced"
    return (
        f"**{item.inventory_id}** - {item.product_name}\n"
        f"`{item.style_code}` | Size `{item.size}` | `{item.item_type}`\n"
        f"Cost `${item.total_cost:.2f}`\n"
        f"Location `{item.location}` | Status `{item.status}`\n"
        f"Purchased `{item.purchase_date}` | Sheet row `{sheet_row}` | Sync `{item.sheet_sync_status}`"
    )


def format_backfill_result(result) -> str:
    lines = [
        "Inventory backfill complete.",
        f"Assigned: {len(result.assigned)} | Resumed: {len(result.resumed)} | "
        f"Valid existing: {len(result.valid_existing)} | Imported to SQLite: "
        f"{len(result.imported_existing)}",
        f"Malformed IDs: {len(result.malformed_ids)} | Duplicate IDs: "
        f"{len(result.duplicate_ids)} | Failures: {len(result.failures)}",
    ]
    if result.malformed_ids:
        lines.append("Malformed: " + format_backfill_audit(result.malformed_ids))
    if result.duplicate_ids:
        lines.append("Duplicates: " + format_backfill_audit(result.duplicate_ids))
    if result.failures:
        lines.append("Failures: " + format_backfill_failures(result.failures))
        lines.append("Resume safely by running the same deliberate command again after fixing the error.")
    return "\n".join(lines)


def format_backfill_audit(entries: list[dict], limit: int = 10) -> str:
    descriptions = []
    for entry in entries[:limit]:
        original = entry.get("inventory_id") or "blank"
        replacement = entry.get("replacement_inventory_id") or "not replaced"
        first_row = f", first row {entry['first_row']}" if entry.get("first_row") else ""
        descriptions.append(
            f"row {entry['row']} `{original}` -> `{replacement}`{first_row}"
        )
    if len(entries) > limit:
        descriptions.append(f"and {len(entries) - limit} more")
    return "; ".join(descriptions)


def format_backfill_failures(entries: list[dict], limit: int = 3) -> str:
    descriptions = []
    for entry in entries[:limit]:
        row_text = str(entry.get("row", "unknown"))
        if entry.get("through_row") and entry["through_row"] != entry.get("row"):
            row_text += f"-{entry['through_row']}"
        inventory_id = entry.get("inventory_id") or "unknown ID"
        error = str(entry.get("error") or "unknown error").replace("\n", " ")[:300]
        descriptions.append(f"row {row_text} `{inventory_id}`: {error}")
    if len(entries) > limit:
        descriptions.append(f"and {len(entries) - limit} more")
    return "; ".join(descriptions)


async def setup(bot):
    await bot.add_cog(Inventory(bot))
