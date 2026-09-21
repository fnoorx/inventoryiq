import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cogs import photo_intake
from services.label_intake import LabelIntakeResult, MarketEstimate
from services.label_scan_repository import LabelScan, SCAN_VERIFIED


def sample_scan(**overrides):
    values = {
        "scan_id": "SCAN-0123456789ABCDEF",
        "discord_message_id": "123",
        "discord_attachment_id": "456",
        "image_sha256": "a" * 64,
        "state": SCAN_VERIFIED,
        "barcode_results": [
            {
                "text": "0196604444156",
                "format": "EAN13",
                "source_variant": "original",
                "valid_checksum": True,
                "gtin": "0196604444156",
            }
        ],
        "raw_vision": {},
        "normalized_extraction": {
            "brand": "Aether",
            "product_name": "Aether Meridian Pace 3",
            "style_code": "QX1002-300",
            "selected_size": "9W",
            "printed_sizes": [
                {"system": "US_W", "value": "9", "normalized": "9W"},
                {"system": "US_M", "value": "7.5", "normalized": "7.5"},
            ],
            "gtin": "0196604444156",
            "product_type": "sneakers",
            "source": "barcode",
        },
        "market_data": {
            "avg_sales": 300,
            "highest_bid": 250,
            "lowest_ask": 320,
            "flex_lowest_ask": 315,
            "beat_US": 290,
        },
        "stockx_product_id": "product-1",
        "stockx_variant_id": "variant-1",
        "validation_status": SCAN_VERIFIED,
        "warnings": [],
        "error_details": None,
        "supplied_price": 180,
        "location": "DEMO",
        "purchase_date": "07/21/2026",
        "linked_inventory_id": None,
        "created_at": "created",
        "updated_at": "updated",
        "confirmed_at": None,
    }
    values.update(overrides)
    return LabelScan(**values)


def make_ctx(attachments, channel_id=photo_intake.CHANNEL_ID):
    return SimpleNamespace(
        channel=SimpleNamespace(id=channel_id),
        message=SimpleNamespace(id=123, attachments=attachments),
        send=AsyncMock(),
    )


def run_photo(ctx, *args):
    cog = photo_intake.PhotoIntake(bot=object())
    asyncio.run(photo_intake.PhotoIntake.photo.callback(cog, ctx, *args))


async def run_inline(function, *args, **kwargs):
    return function(*args, **kwargs)


def test_preview_embed_contains_required_label_market_and_estimate_fields():
    estimate = MarketEstimate(180, 203.4, "average sale", 300, 267, 63.6, 0.3127)
    embed = photo_intake.build_photo_embed(
        LabelIntakeResult(scan=sample_scan(), created=True, estimate=estimate)
    )

    assert embed.title == "Aether Meridian Pace 3"
    fields = {field.name: field.value for field in embed.fields}
    assert "QX1002-300" in fields["Product"]
    assert "9W" in fields["Product"]
    assert "US_W 9" in fields["Printed sizes"]
    assert "US_M 7.5" in fields["Printed sizes"]
    assert "0196604444156" in fields["Barcode"]
    assert "Average sale: $300.00" in fields["Exact StockX market (CAD)"]
    assert "Cost (after tax): $203.40" in fields["Purchase estimate"]
    assert "Estimated profit: $63.60" in fields["Purchase estimate"]
    assert "31.27%" in fields["Purchase estimate"]
    assert "SCAN-0123456789ABCDEF" in embed.footer.text


def test_persistent_view_has_all_review_actions():
    async def labels_and_ids():
        view = photo_intake.PhotoIntakeView(photo_intake.PhotoIntake(bot=object()))
        return {(item.label, item.custom_id) for item in view.children}

    actions = asyncio.run(labels_and_ids())

    assert actions == {
        ("Add to Inventory", "photo_intake:add_inventory"),
        ("Research All Sizes", "photo_intake:all_sizes"),
        ("Edit", "photo_intake:edit"),
        ("Cancel", "photo_intake:cancel"),
    }


def test_photo_command_reports_missing_attachment():
    ctx = make_ctx([])

    run_photo(ctx)

    ctx.send.assert_awaited_once_with(photo_intake.PHOTO_USAGE)


def test_photo_command_rejects_oversized_attachment(monkeypatch):
    attachment = SimpleNamespace(
        id=456,
        filename="label.jpg",
        content_type="image/jpeg",
        size=11,
        read=AsyncMock(),
    )
    ctx = make_ctx([attachment])
    monkeypatch.setenv(photo_intake.DEFAULT_LOCATION_ENV_VAR, "DEMO")
    monkeypatch.setattr(photo_intake, "configured_max_image_bytes", lambda: 10)

    run_photo(ctx)

    assert "too large" in ctx.send.await_args.args[0]
    attachment.read.assert_not_awaited()


def test_research_only_photo_creates_preview_but_no_inventory(monkeypatch):
    attachment = SimpleNamespace(
        id=456,
        filename="label.jpg",
        content_type="image/jpeg",
        size=5,
        read=AsyncMock(return_value=b"image"),
    )
    ctx = make_ctx([attachment])
    scan = sample_scan(supplied_price=None)
    process = Mock(return_value=LabelIntakeResult(scan=scan, created=True, estimate=None))
    monkeypatch.setenv(photo_intake.DEFAULT_LOCATION_ENV_VAR, "DEMO")
    monkeypatch.setattr(photo_intake, "configured_max_image_bytes", lambda: 10)
    monkeypatch.setattr(photo_intake.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(photo_intake, "process_label_scan", process)

    run_photo(ctx)

    process.assert_called_once()
    sent = ctx.send.await_args.kwargs
    fields = {field.name: field.value for field in sent["embed"].fields}
    assert "Research only" in fields["Purchase estimate"]
    assert isinstance(sent["view"], photo_intake.PhotoIntakeView)
