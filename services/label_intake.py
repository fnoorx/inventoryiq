"""Hybrid barcode/vision shoe-label research and inventory confirmation service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from pathlib import Path
import re
from typing import Callable

from services.barcode_decoder import (
    BarcodeResult,
    decode_barcodes,
    validate_gtin_check_digit,
)
from services.inventory_repository import InventoryRepository, InventoryUnitInput, SYNCED
from services.inventory_service import (
    InventoryCreationResult,
    create_inventory_unit,
    sheet_item_type,
)
from services.label_preprocessing import ImageVariant, preprocess_label_image
from services.label_scan_repository import (
    CONFIRMABLE_SCAN_STATES,
    LabelScan,
    LabelScanRepository,
    SCAN_CANCELLED,
    SCAN_CONFIRMED,
    SCAN_CONFLICT,
    SCAN_FAILED,
    SCAN_RECEIVED,
    SCAN_REVIEW_REQUIRED,
    SCAN_VERIFIED,
)
from services.pricing import calculate_roi, calculate_total_cost, net_sale
from services.sizes import normalize_size
from services.stockx import (
    StockxVariantMatch,
    find_variant_matches,
    get_product_id,
    get_product_details,
    get_product_market_data,
)
from services.stockx_gtin_lookup import StockxGtinLookupResult, lookup_gtin
from services.vision_label_extractor import (
    VisionExtractionError,
    VisionLabelExtraction,
    canonical_size_system,
    extract_visible_label,
    normalize_style_code,
)
from utils.performance import timed


@dataclass(frozen=True)
class MarketEstimate:
    purchase_subtotal: float
    total_cost: float
    source: str | None
    gross_market_price: float | None
    estimated_payout: float | None
    estimated_profit: float | None
    roi: float | None


@dataclass(frozen=True)
class LabelIntakeResult:
    scan: LabelScan
    created: bool
    estimate: MarketEstimate | None


@timed("photo_label_intake")
def process_label_scan(
    *,
    discord_message_id: str | int,
    discord_attachment_id: str | int,
    image_bytes: bytes,
    filename: str | None,
    content_type: str | None,
    supplied_price: float | None,
    location: str,
    purchase_date: str | None = None,
    scan_repository: LabelScanRepository | None = None,
    database_path: str | Path | None = None,
    preprocess_fn: Callable[..., list[ImageVariant]] = preprocess_label_image,
    barcode_decoder_fn: Callable[[list[ImageVariant]], list[BarcodeResult]] = decode_barcodes,
    vision_extractor_fn: Callable[[list[ImageVariant]], VisionLabelExtraction] = extract_visible_label,
    gtin_lookup_fn: Callable[[str], StockxGtinLookupResult | None] = lookup_gtin,
    product_id_fn: Callable[[str], str | None] = get_product_id,
    product_details_fn: Callable[[str], dict | None] = get_product_details,
    variant_match_fn: Callable[[str, list[str]], list[StockxVariantMatch]] = find_variant_matches,
    market_data_fn: Callable[[str, str], dict | None] = get_product_market_data,
) -> LabelIntakeResult:
    """Research one attachment without creating a permanent inventory unit."""

    if not str(location or "").strip():
        raise ValueError("A default inventory location is required for photo intake.")
    if supplied_price is not None and float(supplied_price) < 0:
        raise ValueError("Price cannot be negative.")
    purchase_date = purchase_date or date.today().strftime("%m/%d/%Y")
    variants = preprocess_fn(
        image_bytes,
        content_type=content_type,
        filename=filename,
    )
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    repository = scan_repository or LabelScanRepository(database_path)
    scan, created = repository.create_or_get(
        discord_message_id=discord_message_id,
        discord_attachment_id=discord_attachment_id,
        image_sha256=image_hash,
        supplied_price=supplied_price,
        location=str(location).strip(),
        purchase_date=purchase_date,
    )
    if not created and scan.state not in {SCAN_RECEIVED, SCAN_FAILED}:
        return LabelIntakeResult(
            scan=scan,
            created=False,
            estimate=calculate_market_estimate(scan.market_data, scan.supplied_price),
        )

    try:
        barcode_results = barcode_decoder_fn(variants)
        barcode_dicts = [barcode_result_dict(result) for result in barcode_results]
        warnings = invalid_barcode_warnings(barcode_dicts)

        vision = None
        try:
            vision = vision_extractor_fn(variants)
        except VisionExtractionError as exc:
            warnings.append(str(exc))
        except Exception as exc:
            warnings.append(f"Visible label cross-check was unavailable: {exc}")

        raw_vision = vision.model_dump(mode="json") if vision else None
        resolution = resolve_stockx_identity(
            barcode_results=barcode_dicts,
            vision=vision,
            gtin_lookup_fn=gtin_lookup_fn,
            product_id_fn=product_id_fn,
            product_details_fn=product_details_fn,
            variant_match_fn=variant_match_fn,
        )
        warnings.extend(resolution["warnings"])

        market_data = None
        if resolution["product_id"] and resolution["variant_id"]:
            try:
                market_data = market_data_fn(
                    resolution["product_id"], resolution["variant_id"]
                )
                if market_data is None:
                    warnings.append("StockX returned no market data for the exact variant.")
            except Exception as exc:
                warnings.append(f"StockX market data was unavailable: {exc}")

        scan = repository.save_analysis(
            scan.scan_id,
            state=resolution["status"],
            barcode_results=barcode_dicts,
            raw_vision=raw_vision,
            normalized_extraction=resolution["normalized"],
            market_data=market_data,
            stockx_product_id=resolution["product_id"],
            stockx_variant_id=resolution["variant_id"],
            validation_status=resolution["status"],
            warnings=deduplicate(warnings),
        )
        return LabelIntakeResult(
            scan=scan,
            created=created,
            estimate=calculate_market_estimate(market_data, scan.supplied_price),
        )
    except Exception as exc:
        repository.mark_failed(scan.scan_id, exc)
        raise


def resolve_stockx_identity(
    *,
    barcode_results: list[dict],
    vision: VisionLabelExtraction | None,
    gtin_lookup_fn,
    product_id_fn,
    product_details_fn,
    variant_match_fn,
) -> dict:
    """Decide which StockX variant a label photo refers to, and how sure we are.

    Evidence is ranked: a checksum-valid barcode decoded from the image beats a
    UPC read by the vision model, which beats a visible style code. Every path
    cross-checks StockX's answer against what is printed on the label, and any
    disagreement lands in ``conflict`` rather than a guess.
    """

    warnings = []
    local_gtins = deduplicate(
        [
            result.get("gtin")
            for result in barcode_results
            if result.get("gtin") and result.get("valid_checksum") is True
        ]
    )
    visible_gtins = []
    if vision:
        visible_gtins = deduplicate(
            [
                str(candidate).strip()
                for candidate in vision.upc_candidates
                if validate_gtin_check_digit(str(candidate).strip())
            ]
        )

    gtin_source = "barcode" if local_gtins else "vision_gtin"
    candidate_gtins = local_gtins or visible_gtins
    recognized = []
    for gtin in candidate_gtins:
        try:
            lookup = gtin_lookup_fn(gtin)
        except Exception as exc:
            warnings.append(f"StockX GTIN lookup failed for {gtin}: {exc}")
            continue
        if lookup:
            recognized.append(lookup)

    unique_variants = {
        (item.product_id, item.variant_id): item
        for item in recognized
        if item.product_id and item.variant_id
    }
    if len(unique_variants) > 1:
        lookup = next(iter(unique_variants.values()))
        normalized = normalized_from_gtin(lookup, vision, gtin_source)
        return resolution(
            SCAN_CONFLICT,
            normalized,
            lookup.product_id,
            lookup.variant_id,
            warnings + ["Multiple decoded barcodes resolve to different StockX variants."],
        )

    if recognized:
        lookup = next(iter(unique_variants.values()), recognized[0])
        status, evidence_warnings = validate_barcode_evidence(
            lookup,
            vision,
            locally_decoded=bool(local_gtins),
        )
        normalized = normalized_from_gtin(lookup, vision, gtin_source)
        return resolution(
            status,
            normalized,
            lookup.product_id,
            lookup.variant_id,
            warnings + evidence_warnings,
        )

    if local_gtins:
        return resolution(
            SCAN_CONFLICT,
            normalized_from_vision(vision, source="barcode_unrecognized"),
            "",
            "",
            warnings + ["A checksum-valid barcode was found, but StockX could not validate it."],
        )

    if candidate_gtins:
        warnings.append("Visible UPC candidates were not recognized by StockX.")
    return resolve_by_visible_style(
        vision,
        warnings=warnings,
        product_id_fn=product_id_fn,
        product_details_fn=product_details_fn,
        variant_match_fn=variant_match_fn,
    )


def resolve_by_visible_style(
    vision: VisionLabelExtraction | None,
    *,
    warnings: list[str],
    product_id_fn,
    product_details_fn,
    variant_match_fn,
) -> dict:
    """Fallback when no barcode resolved: match the printed style code and US size."""

    if vision is None:
        return resolution(
            SCAN_CONFLICT,
            None,
            "",
            "",
            warnings + ["No usable barcode or visible label extraction was available."],
        )

    style_code = vision_style_code(vision)
    size_candidates = stockx_size_candidates(vision)
    if not style_code or not size_candidates:
        missing = []
        if not style_code:
            missing.append("style code")
        if not size_candidates:
            missing.append("unambiguous US size")
        return resolution(
            SCAN_CONFLICT,
            normalized_from_vision(vision, source="visible_style"),
            "",
            "",
            warnings + [f"The label has no usable {' or '.join(missing)}."],
        )

    product_id = product_id_fn(style_code)
    if not product_id:
        return resolution(
            SCAN_CONFLICT,
            normalized_from_vision(vision, source="visible_style"),
            "",
            "",
            warnings + [f"StockX could not validate visible style {style_code}."],
        )
    product = product_details_fn(product_id) or {}
    stockx_style = normalize_style_code(product.get("styleId"))
    if stockx_style and stockx_style != style_code:
        return resolution(
            SCAN_CONFLICT,
            normalized_from_product(product, vision, None, None, source="visible_style"),
            product_id,
            "",
            warnings
            + [f"Visible style {style_code} conflicts with StockX style {stockx_style}."],
        )

    matches = variant_match_fn(product_id, size_candidates)
    if len(matches) != 1:
        problem = (
            "Multiple StockX variants match the visible sizes."
            if len(matches) > 1
            else "StockX has no exact variant matching the visible US sizes."
        )
        return resolution(
            SCAN_CONFLICT,
            normalized_from_product(product, vision, None, None, source="visible_style"),
            product_id,
            "",
            warnings + [problem],
        )

    match = matches[0]
    normalized = normalized_from_product(
        product,
        vision,
        match.normalized_size,
        None,
        source="visible_style",
    )
    return resolution(
        SCAN_REVIEW_REQUIRED,
        normalized,
        product_id,
        match.variant_id,
        warnings + ["No locally decoded barcode; confirm the selected StockX variant."],
    )


def validate_barcode_evidence(
    lookup: StockxGtinLookupResult,
    vision: VisionLabelExtraction | None,
    *,
    locally_decoded: bool,
) -> tuple[str, list[str]]:
    """Cross-check a barcode's StockX match against the visible label text."""

    warnings = []
    if not lookup.product_id or not lookup.variant_id:
        return SCAN_CONFLICT, ["StockX did not return an exact product and variant ID."]
    if vision is None:
        return SCAN_REVIEW_REQUIRED, ["Visible style and size could not be cross-checked."]

    visible_style = vision_style_code(vision)
    stockx_style = normalize_style_code(lookup.style_id)
    if visible_style and stockx_style and visible_style != stockx_style:
        return (
            SCAN_CONFLICT,
            [f"Barcode style {stockx_style} conflicts with visible style {visible_style}."],
        )
    if (
        vision.product_name
        and lookup.title
        and not product_names_appear_consistent(vision.product_name, lookup.title)
    ):
        return (
            SCAN_CONFLICT,
            [
                f"Barcode product `{lookup.title}` conflicts with visible product "
                f"`{vision.product_name}`."
            ],
        )
    visible_sizes = stockx_size_candidates(vision)
    stockx_size = normalize_size(lookup.normalized_size or lookup.variant_value)
    if visible_sizes and stockx_size not in {normalize_size(size) for size in visible_sizes}:
        return (
            SCAN_CONFLICT,
            [
                f"Barcode size {stockx_size or 'unknown'} conflicts with visible sizes "
                f"{', '.join(visible_sizes)}."
            ],
        )

    if not visible_style:
        warnings.append("Visible style code was unreadable.")
    if not visible_sizes:
        warnings.append("Visible US size was unreadable or ambiguous.")
    if not locally_decoded:
        warnings.append("GTIN came from visible-text extraction, not the local barcode decoder.")
    if warnings:
        return SCAN_REVIEW_REQUIRED, warnings
    return SCAN_VERIFIED, []


def normalized_from_gtin(
    lookup: StockxGtinLookupResult,
    vision: VisionLabelExtraction | None,
    source: str,
) -> dict:
    return {
        "brand": lookup.brand or (vision.brand if vision else None),
        "product_name": lookup.title or (vision.product_name if vision else None),
        "style_code": normalize_style_code(lookup.style_id),
        "selected_size": normalize_size(lookup.normalized_size or lookup.variant_value),
        "printed_sizes": normalized_visible_sizes(vision),
        "gtin": lookup.gtin,
        "product_type": lookup.product_type,
        "stockx_url": lookup.stockx_url,
        "source": source,
    }


def normalized_from_vision(
    vision: VisionLabelExtraction | None,
    *,
    source: str,
) -> dict | None:
    if vision is None:
        return None
    candidates = stockx_size_candidates(vision)
    return {
        "brand": vision.brand,
        "product_name": vision.product_name,
        "style_code": vision_style_code(vision),
        "selected_size": candidates[0] if len(candidates) == 1 else None,
        "printed_sizes": normalized_visible_sizes(vision),
        "gtin": next(
            (
                candidate
                for candidate in vision.upc_candidates
                if validate_gtin_check_digit(candidate)
            ),
            None,
        ),
        "product_type": "",
        "stockx_url": "",
        "source": source,
    }


def normalized_from_product(
    product: dict,
    vision: VisionLabelExtraction,
    selected_size: str | None,
    gtin: str | None,
    *,
    source: str,
) -> dict:
    return {
        "brand": product.get("brand") or vision.brand,
        "product_name": product.get("title") or vision.product_name,
        "style_code": normalize_style_code(product.get("styleId"))
        or vision_style_code(vision),
        "selected_size": selected_size,
        "printed_sizes": normalized_visible_sizes(vision),
        "gtin": gtin,
        "product_type": product.get("productType") or "",
        "stockx_url": product.get("url") or product.get("stockxUrl") or "",
        "source": source,
    }


def vision_style_code(vision: VisionLabelExtraction | None) -> str | None:
    if vision is None:
        return None
    return normalize_style_code(
        vision.normalized_style_code or vision.raw_style_code
    )


def product_names_appear_consistent(visible_name: str, stockx_name: str) -> bool:
    """Loose token overlap, ignoring brand and gender words that labels often omit."""

    ignored = {
        "DEMO",
        "AETHER",
        "ADIDAS",
        "W",
        "M",
        "MEN",
        "MENS",
        "WOMEN",
        "WOMENS",
        "SHOE",
        "SHOES",
        "SNEAKER",
        "SNEAKERS",
    }

    def tokens(value):
        return {
            token
            for token in re.findall(r"[A-Z0-9]+", str(value or "").upper())
            if token not in ignored and len(token) > 1
        }

    visible_tokens = tokens(visible_name)
    stockx_tokens = tokens(stockx_name)
    if not visible_tokens or not stockx_tokens:
        return True
    required_overlap = min(2, len(visible_tokens), len(stockx_tokens))
    return len(visible_tokens & stockx_tokens) >= required_overlap


def normalized_visible_sizes(vision: VisionLabelExtraction | None) -> list[dict]:
    if vision is None:
        return []
    results = []
    for size in vision.sizes:
        system = canonical_size_system(size.system)
        value = str(size.value or "").strip().upper()
        if not value:
            continue
        results.append(
            {"system": system, "value": value, "normalized": normalize_visible_size(system, value)}
        )
    return results


def normalize_visible_size(system: str, value: str) -> str:
    normalized_value = normalize_size(value)
    if system == "US_W" and normalized_value and not normalized_value.endswith("W"):
        return f"{normalized_value}W"
    if system == "US_Y" and normalized_value and not normalized_value.endswith("Y"):
        return f"{normalized_value}Y"
    if system == "US_M":
        return normalized_value.removesuffix("M")
    return normalized_value


def stockx_size_candidates(vision: VisionLabelExtraction | None) -> list[str]:
    return deduplicate(
        [
            item["normalized"]
            for item in normalized_visible_sizes(vision)
            if item["system"] in {"US_W", "US_M", "US_Y"} and item["normalized"]
        ]
    )


def calculate_market_estimate(
    market_data: dict | None,
    supplied_price: float | None,
) -> MarketEstimate | None:
    """Project payout, profit and ROI for the exact variant at the supplied price."""

    if supplied_price is None:
        return None
    subtotal = float(supplied_price)
    total_cost = calculate_total_cost(subtotal)
    candidates = []
    for key, source in (("avg_sales", "average sale"), ("highest_bid", "highest bid")):
        try:
            gross = float((market_data or {}).get(key))
        except (TypeError, ValueError):
            continue
        payout = net_sale(gross)
        if payout is not None:
            candidates.append((payout, source, gross))

    if not candidates:
        return MarketEstimate(subtotal, total_cost, None, None, None, None, None)
    payout, source, gross = max(candidates, key=lambda candidate: candidate[0])
    profit = round(payout - total_cost, 2)
    return MarketEstimate(
        purchase_subtotal=subtotal,
        total_cost=total_cost,
        source=source,
        gross_market_price=gross,
        estimated_payout=payout,
        estimated_profit=profit,
        roi=calculate_roi(profit, total_cost),
    )


def edit_label_scan(
    scan_id: str,
    *,
    style_code: str,
    size: str,
    supplied_price: float | None,
    location: str,
    purchase_date: str,
    scan_repository: LabelScanRepository | None = None,
    product_id_fn=get_product_id,
    product_details_fn=get_product_details,
    variant_match_fn=find_variant_matches,
    market_data_fn=get_product_market_data,
) -> LabelIntakeResult:
    """Re-resolve a draft from manually entered style/size; always needs review."""

    repository = scan_repository or LabelScanRepository()
    scan = repository.get(scan_id)
    if scan is None:
        raise ValueError(f"Label scan `{scan_id}` was not found.")
    if scan.state in {SCAN_CONFIRMED, SCAN_CANCELLED}:
        raise ValueError(f"Label scan `{scan_id}` can no longer be edited.")

    normalized_style = normalize_style_code(style_code)
    normalized_size_value = normalize_size(size)
    if not normalized_style or not normalized_size_value:
        raise ValueError("Style code and size are required.")
    if not str(location or "").strip():
        raise ValueError("Location is required.")
    if supplied_price is not None and float(supplied_price) < 0:
        raise ValueError("Price cannot be negative.")
    repository.update_draft_fields(
        scan_id,
        supplied_price=supplied_price,
        location=location,
        purchase_date=purchase_date,
    )
    product_id = product_id_fn(normalized_style)
    product = product_details_fn(product_id) if product_id else None
    warnings = ["Manually edited; review and confirm the selected StockX variant."]
    status = SCAN_REVIEW_REQUIRED
    variant_id = ""
    market_data = None

    if not product_id or not product:
        status = SCAN_CONFLICT
        warnings.append(f"StockX could not validate style {normalized_style}.")
    else:
        stockx_style = normalize_style_code(product.get("styleId"))
        matches = variant_match_fn(product_id, [normalized_size_value])
        if stockx_style and stockx_style != normalized_style:
            status = SCAN_CONFLICT
            warnings.append(
                f"Entered style {normalized_style} conflicts with StockX style {stockx_style}."
            )
        elif len(matches) != 1:
            status = SCAN_CONFLICT
            warnings.append("The entered size did not resolve to exactly one StockX variant.")
        else:
            variant_id = matches[0].variant_id
            normalized_size_value = matches[0].normalized_size
            market_data = market_data_fn(product_id, variant_id)

    existing = scan.normalized_extraction or {}
    normalized = {
        **existing,
        "brand": (product or {}).get("brand") or existing.get("brand"),
        "product_name": (product or {}).get("title") or existing.get("product_name"),
        "style_code": normalized_style,
        "selected_size": normalized_size_value,
        "product_type": (product or {}).get("productType") or existing.get("product_type", ""),
        "source": "manual_review",
    }
    scan = repository.save_analysis(
        scan_id,
        state=status,
        barcode_results=scan.barcode_results,
        raw_vision=scan.raw_vision,
        normalized_extraction=normalized,
        market_data=market_data,
        stockx_product_id=product_id or "",
        stockx_variant_id=variant_id,
        validation_status=status,
        warnings=warnings,
    )
    return LabelIntakeResult(
        scan=scan,
        created=False,
        estimate=calculate_market_estimate(market_data, supplied_price),
    )


def confirm_label_scan(
    scan_id: str,
    *,
    scan_repository: LabelScanRepository | None = None,
    inventory_repository: InventoryRepository | None = None,
    sheet=None,
) -> InventoryCreationResult:
    """Turn a reviewed scan into inventory; re-confirming returns the same unit."""

    scan_repository = scan_repository or LabelScanRepository()
    inventory_repository = inventory_repository or InventoryRepository(
        scan_repository.database_path
    )
    scan = scan_repository.get(scan_id)
    if scan is None:
        raise ValueError(f"Label scan `{scan_id}` was not found.")

    if scan.linked_inventory_id:
        item = inventory_repository.get(scan.linked_inventory_id)
        if item is None:
            raise RuntimeError(
                f"Scan `{scan_id}` references missing inventory item `{scan.linked_inventory_id}`."
            )
        return InventoryCreationResult(
            item=item,
            created=False,
            sheet_synced=item.sheet_sync_status == SYNCED,
            sheet_error=item.sheet_sync_error,
        )
    if scan.state not in CONFIRMABLE_SCAN_STATES:
        raise ValueError(
            f"Label scan `{scan_id}` is `{scan.state}` and cannot create inventory."
        )
    if scan.supplied_price is None:
        raise ValueError("Add a purchase price with Edit before adding this scan to inventory.")

    normalized = scan.normalized_extraction or {}
    required = {
        "product name": normalized.get("product_name"),
        "style code": normalized.get("style_code"),
        "size": normalized.get("selected_size"),
        "StockX product ID": scan.stockx_product_id,
        "StockX variant ID": scan.stockx_variant_id,
    }
    missing = [label for label, value in required.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(f"The scan is missing {', '.join(missing)} and cannot be confirmed.")

    creation = create_inventory_unit(
        InventoryUnitInput(
            location=scan.location,
            purchase_date=scan.purchase_date,
            item_type=sheet_item_type(normalized.get("product_type")),
            style_code=str(normalized["style_code"]).upper(),
            product_name=str(normalized["product_name"]).upper(),
            size=str(normalized["selected_size"]),
            price_paid=scan.supplied_price,
            source_identifier=str(normalized.get("gtin") or scan.scan_id),
            stockx_product_id=scan.stockx_product_id,
            stockx_variant_id=scan.stockx_variant_id,
        ),
        scan.discord_message_id,
        sheet=sheet,
        repository=inventory_repository,
    )
    scan_repository.mark_confirmed(scan.scan_id, creation.item.inventory_id)
    return creation


def cancel_label_scan(
    scan_id: str,
    *,
    scan_repository: LabelScanRepository | None = None,
) -> LabelScan:
    return (scan_repository or LabelScanRepository()).mark_cancelled(scan_id)


def resolution(status, normalized, product_id, variant_id, warnings) -> dict:
    return {
        "status": status,
        "normalized": normalized,
        "product_id": str(product_id or ""),
        "variant_id": str(variant_id or ""),
        "warnings": warnings,
    }


def barcode_result_dict(result: BarcodeResult | dict) -> dict:
    if isinstance(result, BarcodeResult):
        return result.as_dict()
    return dict(result)


def invalid_barcode_warnings(results: list[dict]) -> list[str]:
    return [
        f"Ignored {result.get('format') or 'barcode'} `{result.get('text')}`: invalid check digit."
        for result in results
        if result.get("gtin") and result.get("valid_checksum") is False
    ]


def deduplicate(values: list) -> list:
    seen = set()
    output = []
    for value in values:
        if value is None:
            continue
        key = str(value) if isinstance(value, (dict, list)) else value
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output
