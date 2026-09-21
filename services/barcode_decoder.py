"""Local barcode decoding and evidence-based UPC/EAN validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import re

import cv2
import numpy as np
import zxingcpp

from services.label_preprocessing import ImageVariant
from utils.performance import timed


SUPPORTED_FORMATS = {"UPCA", "UPCE", "EAN8", "EAN13", "QRCODE"}


@dataclass(frozen=True)
class BarcodeResult:
    text: str
    format: str
    source_variant: str
    valid_checksum: bool | None
    gtin: str | None

    def as_dict(self) -> dict:
        return asdict(self)


@timed("barcode_decode")
def decode_barcodes(variants: list[ImageVariant]) -> list[BarcodeResult]:
    """Decode supported formats from each unique preprocessing variant."""

    results = []
    seen = set()
    seen_images = set()
    for variant in variants:
        image_hash = hashlib.sha256(variant.data).digest()
        if image_hash in seen_images:
            continue
        seen_images.add(image_hash)
        image = cv2.imdecode(np.frombuffer(variant.data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            continue
        try:
            # zxing-cpp's default try_rotate=True already searches every
            # orientation, so externally rotating the same image is redundant.
            decoded_values = zxingcpp.read_barcodes(image)
        except Exception:
            continue
        for decoded in decoded_values:
            text = str(getattr(decoded, "text", "") or "").strip()
            barcode_format = normalize_barcode_format(getattr(decoded, "format", ""))
            if not text or barcode_format not in SUPPORTED_FORMATS:
                continue
            key = (barcode_format, text)
            if key in seen:
                continue
            seen.add(key)
            gtin, checksum = barcode_gtin(text, barcode_format)
            results.append(
                BarcodeResult(
                    text=text,
                    format=barcode_format,
                    source_variant=variant.name,
                    valid_checksum=checksum,
                    gtin=gtin,
                )
            )
    return results


def barcode_gtin(text: str, barcode_format: str) -> tuple[str | None, bool | None]:
    normalized_format = normalize_barcode_format(barcode_format)
    if normalized_format == "QRCODE":
        candidates = extract_gtin_candidates(text)
        if not candidates:
            return None, None
        candidate = candidates[0]
        return candidate, validate_gtin_check_digit(candidate)

    digits = re.sub(r"\D", "", text)
    if normalized_format == "UPCE":
        if len(digits) != 8:
            return digits or None, False
        expanded = expand_upce(digits)
        return expanded or digits, bool(expanded and validate_gtin_check_digit(expanded))

    expected_lengths = {"UPCA": 12, "EAN8": 8, "EAN13": 13}
    expected = expected_lengths.get(normalized_format)
    if expected is None:
        return None, None
    return digits or None, len(digits) == expected and validate_gtin_check_digit(digits)


def extract_gtin_candidates(value: str) -> list[str]:
    compact = re.sub(r"[\s-]", "", str(value or ""))
    candidates = []
    if compact.isdigit() and len(compact) in {8, 12, 13, 14}:
        candidates.append(compact)
    for match in re.findall(r"(?<!\d)(\d{8}|\d{12,14})(?!\d)", str(value or "")):
        if match not in candidates:
            candidates.append(match)
    return candidates


def validate_gtin_check_digit(value: str) -> bool:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) not in {8, 12, 13, 14}:
        return False
    body = digits[:-1]
    expected = int(digits[-1])
    weighted_sum = sum(
        int(digit) * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(reversed(body))
    )
    return (10 - weighted_sum % 10) % 10 == expected


def expand_upce(value: str) -> str | None:
    """Expand an eight-digit UPC-E into UPC-A so its checksum can be verified."""

    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 8 or digits[0] not in {"0", "1"}:
        return None
    number_system, x1, x2, x3, x4, x5, x6, check = digits
    if x6 in "012":
        body = number_system + x1 + x2 + x6 + "0000" + x3 + x4 + x5
    elif x6 == "3":
        body = number_system + x1 + x2 + x3 + "00000" + x4 + x5
    elif x6 == "4":
        body = number_system + x1 + x2 + x3 + x4 + "00000" + x5
    else:
        body = number_system + x1 + x2 + x3 + x4 + x5 + "0000" + x6
    return body + check


def normalize_barcode_format(value: object) -> str:
    text = str(value or "").upper()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return re.sub(r"[^A-Z0-9]", "", text)
