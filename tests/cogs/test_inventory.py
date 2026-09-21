import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest

from cogs import inventory
from services.inventory_repository import InventoryUnitInput
from services.stockx_gtin_lookup import StockxGtinLookupResult


def make_ctx(channel_id=inventory.CHANNEL_ID):
    return SimpleNamespace(
        channel=SimpleNamespace(id=channel_id),
        message=SimpleNamespace(id=123456789),
        send=AsyncMock(),
    )


def run_add(ctx, *args):
    cog = inventory.Inventory(bot=object())
    callback = inventory.Inventory.add.callback
    asyncio.run(callback(cog, ctx, *args))


async def run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


def sample_lookup():
    return StockxGtinLookupResult(
        gtin="194817794556",
        title="Aether Test Shoe",
        style_id="qx1006-500",
        product_type="sneakers",
        variant_value="M 10.0",
        normalized_size="10",
    )


def test_parse_add_arguments_supports_scanner_shortcut():
    result = inventory.parse_add_arguments(
        ("194817794556", "$180.50"),
        default_location="DEMO",
    )

    assert result == (
        "DEMO",
        date.today().strftime("%m/%d/%Y"),
        "194817794556",
        None,
        180.50,
    )


def test_parse_add_arguments_preserves_style_code_form():
    result = inventory.parse_add_arguments(
        ("WH", "07/10/2026", "QX1001-200", "7W", "200"),
    )

    assert result == ("WH", "07/10/2026", "QX1001-200", "7W", 200.0)


def test_validate_gtin_preserves_leading_zeroes():
    assert inventory.validate_gtin(" 001234567890 ") == "001234567890"


def test_inventory_fields_from_gtin_maps_sheet_values():
    assert inventory.inventory_fields_from_gtin(sample_lookup()) == (
        "Footwear",
        "QX1006-500",
        "Aether Test Shoe",
        "10",
    )


def test_scanner_command_looks_up_variant_creates_unit_and_reports_id(monkeypatch):
    ctx = make_ctx()
    lookup_gtin = Mock(return_value=sample_lookup())
    created_item = SimpleNamespace(
        inventory_id="INV-000001",
        price_paid=180.0,
        total_cost=203.4,
    )
    create_unit = Mock(
        return_value=SimpleNamespace(
            item=created_item,
            created=True,
            sheet_synced=True,
            sheet_error=None,
        )
    )
    monkeypatch.setenv(inventory.DEFAULT_LOCATION_ENV_VAR, "DEMO")
    monkeypatch.setattr(inventory.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(inventory, "lookup_gtin", lookup_gtin)
    monkeypatch.setattr(inventory, "create_inventory_unit", create_unit)

    run_add(ctx, "194817794556", "180")

    lookup_gtin.assert_called_once_with("194817794556")
    create_unit.assert_called_once_with(
        InventoryUnitInput(
            location="DEMO",
            purchase_date=date.today().strftime("%m/%d/%Y"),
            item_type="Footwear",
            style_code="QX1006-500",
            product_name="AETHER TEST SHOE",
            size="10",
            cost=180.0,
            source_identifier="194817794556",
            stockx_product_id="",
            stockx_variant_id="",
        ),
        123456789,
    )
    ctx.send.assert_awaited_once_with(
        "Added **AETHER TEST SHOE** as `INV-000001`\n"
        "`QX1006-500` | Size `10` | Cost `$203.40` | `DEMO`"
    )


def test_scanner_command_requires_default_location_for_shortcut(monkeypatch):
    ctx = make_ctx()
    monkeypatch.delenv(inventory.DEFAULT_LOCATION_ENV_VAR, raising=False)

    run_add(ctx, "194817794556", "180")

    ctx.send.assert_awaited_once_with(
        "Set `INVENTORY_DEFAULT_LOCATION` in `.env` to use `!add <UPC/GTIN> <price>`."
    )


def test_add_command_rejects_wrong_channel(monkeypatch):
    ctx = make_ctx(channel_id=123)
    to_thread = AsyncMock()
    monkeypatch.setattr(inventory.asyncio, "to_thread", to_thread)

    run_add(ctx, "194817794556", "180")

    ctx.send.assert_awaited_once_with(
        f"This command can only be used in the <#{inventory.CHANNEL_ID}> channel."
    )
    to_thread.assert_not_awaited()


def test_item_display_uses_after_tax_total_as_cost():
    item = SimpleNamespace(
        inventory_id="INV-000001",
        product_name="AETHER TEST SHOE",
        style_code="QX1006-500",
        size="10",
        item_type="Footwear",
        price_paid=180.0,
        total_cost=203.4,
        location="DEMO",
        status="Pending",
        purchase_date="07/21/2026",
        sheet_row=2,
        sheet_sync_status="synced",
    )

    text = inventory.format_inventory_item(item)

    assert "Cost `$203.40`" in text
    assert "180.00" not in text


@pytest.mark.parametrize("args", [
    ("194817794556", "100"),
    ("DEMO", "today", "194817794556", "100"),
    ("DEMO", "today", "QX1001-200", "10.5", "100"),
])
def test_optional_discount_preserves_all_add_formats(args):
    normal = inventory.parse_add_arguments(args, default_location="DEMO")
    discounted = inventory.parse_add_arguments((*args, "30%"), default_location="DEMO")
    assert discounted[:-1] == normal[:-1]
    assert normal[-1] == 100
    assert discounted[-1] == 70


@pytest.mark.parametrize("discount,expected", [("0%", 100), ("100%", 0), ("12.5%", 87.5)])
def test_discount_boundaries(discount, expected):
    assert inventory.parse_add_arguments(("194817794556", "100", discount), "DEMO")[-1] == expected


@pytest.mark.parametrize("discount", ["-1%", "101%", "NaN%", "Infinity%", "abc%", "%", "30%%"])
def test_invalid_discount_rejected_before_lookup(monkeypatch, discount):
    worker = AsyncMock()
    monkeypatch.setattr(inventory.asyncio, "to_thread", worker)
    ctx = make_ctx()
    run_add(ctx, "194817794556", "100", discount)
    assert "Discount must" in ctx.send.await_args.args[0]
    worker.assert_not_awaited()


def test_discount_rounds_to_cents():
    assert inventory.parse_add_arguments(("194817794556", "10.05", "50%"), "DEMO")[-1] == 5.03


def test_discounted_price_reaches_inventory_with_tax(monkeypatch):
    def create(values, message_id):
        assert values.price_paid == 70
        assert values.total_cost == 79.1
        assert message_id == 123456789
        return SimpleNamespace(item=SimpleNamespace(inventory_id="INV-000001", total_cost=values.total_cost), created=True, sheet_synced=True)

    monkeypatch.setenv(inventory.DEFAULT_LOCATION_ENV_VAR, "DEMO")
    monkeypatch.setattr(inventory.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(inventory, "lookup_gtin", lambda _: sample_lookup())
    create_mock = Mock(side_effect=create)
    monkeypatch.setattr(inventory, "create_inventory_unit", create_mock)
    ctx = make_ctx()
    run_add(ctx, "194817794556", "100", "30%")
    create_mock.assert_called_once()
    assert "Cost `$79.10`" in ctx.send.await_args.args[0]
