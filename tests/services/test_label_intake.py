import sqlite3
from unittest.mock import Mock

import pytest

from services.barcode_decoder import BarcodeResult
from services.inventory_repository import InventoryRepository, InventoryUnitInput, SYNC_FAILED
from services.label_intake import (
    cancel_label_scan,
    confirm_label_scan,
    process_label_scan,
    stockx_size_candidates,
)
from services.label_preprocessing import ImageVariant
from services.label_scan_repository import (
    LabelScanRepository,
    SCAN_CANCELLED,
    SCAN_CONFLICT,
    SCAN_CONFIRMED,
    SCAN_REVIEW_REQUIRED,
    SCAN_VERIFIED,
)
from services.stockx import StockxVariantMatch
from services.stockx_gtin_lookup import StockxGtinLookupResult
from services.vision_label_extractor import VisionLabelExtraction, VisibleSize
from tests.services.test_inventory_identity import FakeInventorySheet, inventory_header


def fake_preprocess(_data, **_kwargs):
    return [ImageVariant("original", b"jpeg", "image/jpeg", 100, 50)]


def meridian_vision(style="QX1002-300"):
    return VisionLabelExtraction(
        brand="Aether",
        product_name="W Aether Meridian Pace 3",
        raw_style_code=style.replace("-", " "),
        normalized_style_code=style,
        upc_candidates=["0196604444156"],
        sizes=[
            VisibleSize(system="US_W", value="9"),
            VisibleSize(system="US_M", value="7.5"),
            VisibleSize(system="UK", value="6.5"),
            VisibleSize(system="EU", value="40.5"),
            VisibleSize(system="CM", value="26"),
        ],
        raw_visible_text="W AETHER MERIDIAN PACE 3 QX1002 300 W 9 M 7.5",
        warnings=[],
        unreadable_fields=[],
    )


def meridian_lookup(style="QX1002-300"):
    return StockxGtinLookupResult(
        gtin="0196604444156",
        product_id="product-meridian",
        variant_id="variant-9w",
        variant_value="W 9",
        normalized_size="9W",
        title="Aether Meridian Pace 3 Mist Grey (Women's)",
        brand="Aether",
        style_id=style,
        product_type="sneakers",
    )


def local_barcode(valid=True):
    return BarcodeResult(
        text="0196604444156" if valid else "0196604444157",
        format="EAN13",
        source_variant="original",
        valid_checksum=valid,
        gtin="0196604444156" if valid else "0196604444157",
    )


def market_values():
    return {
        "avg_sales": 300,
        "highest_bid": 250,
        "lowest_ask": 320,
        "flex_lowest_ask": 315,
        "beat_US": 290,
    }


def process(tmp_path, **overrides):
    repository = overrides.pop("scan_repository", LabelScanRepository(tmp_path / "bot.db"))
    kwargs = {
        "discord_message_id": "message-1",
        "discord_attachment_id": "attachment-1",
        "image_bytes": b"fixture-image",
        "filename": "label.jpg",
        "content_type": "image/jpeg",
        "supplied_price": None,
        "location": "DEMO",
        "purchase_date": "07/21/2026",
        "scan_repository": repository,
        "preprocess_fn": fake_preprocess,
        "barcode_decoder_fn": lambda _variants: [local_barcode()],
        "vision_extractor_fn": lambda _variants: meridian_vision(),
        "gtin_lookup_fn": lambda _gtin: meridian_lookup(),
        "market_data_fn": lambda _product, _variant: market_values(),
    }
    kwargs.update(overrides)
    return process_label_scan(**kwargs)


def test_barcode_and_visible_evidence_verify_exact_variant(tmp_path):
    market = Mock(return_value=market_values())

    result = process(tmp_path, market_data_fn=market)

    assert result.scan.state == SCAN_VERIFIED
    assert result.scan.stockx_product_id == "product-meridian"
    assert result.scan.stockx_variant_id == "variant-9w"
    assert result.scan.normalized_extraction["selected_size"] == "9W"
    assert [size["normalized"] for size in result.scan.normalized_extraction["printed_sizes"][:2]] == [
        "9W",
        "7.5",
    ]
    assert result.estimate is None
    market.assert_called_once_with("product-meridian", "variant-9w")


def test_second_supplied_turfline_label_verifies_style_and_size(tmp_path):
    vision = VisionLabelExtraction(
        brand="Aether",
        product_name="Aether Turfline Pro",
        raw_style_code="QX1008 700",
        normalized_style_code="QX1008-700",
        upc_candidates=["0198726649396"],
        sizes=[
            VisibleSize(system="US_M", value="8.5"),
            VisibleSize(system="UK", value="7.5"),
            VisibleSize(system="EU", value="42"),
            VisibleSize(system="CM", value="26.5"),
        ],
        raw_visible_text="TURFLINE 360 UT QX1008 700 8.5",
        warnings=[],
        unreadable_fields=[],
    )
    barcode = BarcodeResult(
        text="0198726649396",
        format="EAN13",
        source_variant="original",
        valid_checksum=True,
        gtin="0198726649396",
    )
    lookup = StockxGtinLookupResult(
        gtin="0198726649396",
        product_id="product-vapor-edge",
        variant_id="variant-8-5",
        variant_value="M 8.5",
        normalized_size="8.5",
        title="Aether Turfline Pro Ivory",
        brand="Aether",
        style_id="QX1008-700",
        product_type="sneakers",
    )

    result = process(
        tmp_path,
        barcode_decoder_fn=lambda _variants: [barcode],
        vision_extractor_fn=lambda _variants: vision,
        gtin_lookup_fn=lambda _gtin: lookup,
    )

    assert result.scan.state == SCAN_VERIFIED
    assert result.scan.normalized_extraction["style_code"] == "QX1008-700"
    assert result.scan.normalized_extraction["selected_size"] == "8.5"


def test_dual_womens_and_mens_sizes_are_kept_distinct():
    assert stockx_size_candidates(meridian_vision()) == ["9W", "7.5"]


def test_barcode_failure_uses_visible_style_and_requires_review(tmp_path):
    variant_lookup = Mock(
        return_value=[StockxVariantMatch("variant-9w", "W 9", "9W")]
    )

    result = process(
        tmp_path,
        barcode_decoder_fn=lambda _variants: [],
        vision_extractor_fn=lambda _variants: meridian_vision().model_copy(
            update={"upc_candidates": []}
        ),
        product_id_fn=lambda style: "product-meridian" if style == "QX1002-300" else None,
        product_details_fn=lambda _product: {
            "title": "Aether Meridian Pace 3 (Women's)",
            "styleId": "QX1002-300",
            "brand": "Aether",
            "productType": "sneakers",
        },
        variant_match_fn=variant_lookup,
    )

    assert result.scan.state == SCAN_REVIEW_REQUIRED
    assert result.scan.stockx_variant_id == "variant-9w"
    assert "confirm the selected" in " ".join(result.scan.warnings)
    variant_lookup.assert_called_once_with("product-meridian", ["9W", "7.5"])


def test_invalid_check_digit_is_ignored_before_style_fallback(tmp_path):
    result = process(
        tmp_path,
        barcode_decoder_fn=lambda _variants: [local_barcode(valid=False)],
        vision_extractor_fn=lambda _variants: meridian_vision().model_copy(
            update={"upc_candidates": []}
        ),
        product_id_fn=lambda _style: "product-meridian",
        product_details_fn=lambda _product: {
            "title": "Meridian",
            "styleId": "QX1002-300",
            "productType": "sneakers",
        },
        variant_match_fn=lambda _product, _sizes: [
            StockxVariantMatch("variant-9w", "W 9", "9W")
        ],
    )

    assert result.scan.state == SCAN_REVIEW_REQUIRED
    assert "invalid check digit" in " ".join(result.scan.warnings)


def test_barcode_and_visible_style_conflict_cannot_be_confirmed(tmp_path):
    result = process(
        tmp_path,
        vision_extractor_fn=lambda _variants: meridian_vision("QX1008-700"),
    )

    assert result.scan.state == SCAN_CONFLICT
    assert "conflicts with visible style" in " ".join(result.scan.warnings)
    with pytest.raises(ValueError, match="cannot create inventory"):
        confirm_label_scan(
            result.scan.scan_id,
            scan_repository=LabelScanRepository(tmp_path / "bot.db"),
            inventory_repository=InventoryRepository(tmp_path / "bot.db"),
            sheet=FakeInventorySheet([inventory_header(with_id=True)]),
        )


def test_barcode_and_visible_product_name_conflict(tmp_path):
    result = process(
        tmp_path,
        vision_extractor_fn=lambda _variants: meridian_vision().model_copy(
            update={"product_name": "Completely Different Boot"}
        ),
    )

    assert result.scan.state == SCAN_CONFLICT
    assert "conflicts with visible product" in " ".join(result.scan.warnings)


def test_multiple_visible_variant_matches_are_a_conflict(tmp_path):
    result = process(
        tmp_path,
        barcode_decoder_fn=lambda _variants: [],
        vision_extractor_fn=lambda _variants: meridian_vision().model_copy(
            update={"upc_candidates": []}
        ),
        product_id_fn=lambda _style: "product-meridian",
        product_details_fn=lambda _product: {
            "title": "Meridian",
            "styleId": "QX1002-300",
            "productType": "sneakers",
        },
        variant_match_fn=lambda _product, _sizes: [
            StockxVariantMatch("variant-a", "W 9", "9W"),
            StockxVariantMatch("variant-b", "M 7.5", "7.5"),
        ],
    )

    assert result.scan.state == SCAN_CONFLICT
    assert "Multiple StockX variants" in " ".join(result.scan.warnings)


def test_market_estimate_uses_after_tax_cost_and_existing_fee_rate(tmp_path):
    result = process(tmp_path, supplied_price=180)

    assert result.estimate.total_cost == 203.4
    assert result.estimate.source == "average sale"
    assert result.estimate.estimated_payout == 267.0
    assert result.estimate.estimated_profit == 63.6
    assert result.estimate.roi == 0.3127


def test_confirmation_creates_exactly_one_permanent_id(tmp_path):
    database = tmp_path / "bot.db"
    scan_repository = LabelScanRepository(database)
    result = process(tmp_path, scan_repository=scan_repository, supplied_price=180)
    inventory_repository = InventoryRepository(database)
    sheet = FakeInventorySheet([inventory_header(with_id=True)])

    first = confirm_label_scan(
        result.scan.scan_id,
        scan_repository=scan_repository,
        inventory_repository=inventory_repository,
        sheet=sheet,
    )
    repeated = confirm_label_scan(
        result.scan.scan_id,
        scan_repository=scan_repository,
        inventory_repository=inventory_repository,
        sheet=sheet,
    )

    assert first.item.inventory_id == "INV-000001"
    assert repeated.item.inventory_id == "INV-000001"
    assert repeated.created is False
    assert scan_repository.get(result.scan.scan_id).state == SCAN_CONFIRMED
    assert len(sheet.batch_updates) == 1
    next_item, _ = inventory_repository.create_unit(
        InventoryUnitInput(
            location="DEMO",
            purchase_date="07/21/2026",
            item_type="Footwear",
            style_code="TEST-001",
            product_name="NEXT UNIT",
            size="10",
            price_paid=1,
        ),
        "different-message",
    )
    assert next_item.inventory_id == "INV-000002"


def test_sheet_failure_preserves_confirmed_inventory_and_id(tmp_path):
    database = tmp_path / "bot.db"
    scan_repository = LabelScanRepository(database)
    result = process(tmp_path, scan_repository=scan_repository, supplied_price=180)
    inventory_repository = InventoryRepository(database)
    sheet = FakeInventorySheet([inventory_header(with_id=True)])
    sheet.fail_batch_before_apply = True

    creation = confirm_label_scan(
        result.scan.scan_id,
        scan_repository=scan_repository,
        inventory_repository=inventory_repository,
        sheet=sheet,
    )
    repeated = confirm_label_scan(
        result.scan.scan_id,
        scan_repository=scan_repository,
        inventory_repository=inventory_repository,
        sheet=sheet,
    )

    assert creation.item.inventory_id == "INV-000001"
    assert creation.sheet_synced is False
    assert inventory_repository.get("INV-000001").sheet_sync_status == SYNC_FAILED
    assert repeated.item.inventory_id == "INV-000001"
    assert scan_repository.get(result.scan.scan_id).linked_inventory_id == "INV-000001"


def test_cancelled_scan_creates_no_inventory(tmp_path):
    database = tmp_path / "bot.db"
    scan_repository = LabelScanRepository(database)
    result = process(tmp_path, scan_repository=scan_repository, supplied_price=180)
    cancelled = cancel_label_scan(result.scan.scan_id, scan_repository=scan_repository)

    assert cancelled.state == SCAN_CANCELLED
    with pytest.raises(ValueError, match="cannot create inventory"):
        confirm_label_scan(
            result.scan.scan_id,
            scan_repository=scan_repository,
            inventory_repository=InventoryRepository(database),
            sheet=FakeInventorySheet([inventory_header(with_id=True)]),
        )


def test_scan_migration_and_identity_deduplication_are_idempotent(tmp_path):
    database = tmp_path / "bot.db"
    first_repository = LabelScanRepository(database)
    scan, created = first_repository.create_or_get(
        discord_message_id="message-1",
        discord_attachment_id="attachment-1",
        image_sha256="a" * 64,
        supplied_price=None,
        location="DEMO",
        purchase_date="07/21/2026",
    )
    second_repository = LabelScanRepository(database)
    repeated_message, message_created = second_repository.create_or_get(
        discord_message_id="message-1",
        discord_attachment_id="attachment-1",
        image_sha256="b" * 64,
        supplied_price=None,
        location="DEMO",
        purchase_date="07/21/2026",
    )
    repeated_hash, hash_created = second_repository.create_or_get(
        discord_message_id="message-2",
        discord_attachment_id="attachment-2",
        image_sha256="a" * 64,
        supplied_price=None,
        location="DEMO",
        purchase_date="07/21/2026",
    )

    assert created is True
    assert message_created is False
    assert hash_created is False
    assert repeated_message.scan_id == repeated_hash.scan_id == scan.scan_id
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "label_scans" in tables
