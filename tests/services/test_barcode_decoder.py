from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from services import barcode_decoder
from services.barcode_decoder import (
    decode_barcodes,
    expand_upce,
    validate_gtin_check_digit,
)
from services.label_preprocessing import (
    ImageValidationError,
    ImageVariant,
    perspective_correct,
    preprocess_label_image,
    rotate_image,
    validate_image_bytes,
)


def test_decode_supported_barcode_and_deduplicates_rotations(monkeypatch):
    decoded = SimpleNamespace(text="0196604444156", format="EAN-13")
    calls = []

    def read_barcodes(image):
        calls.append(image)
        return [decoded]

    monkeypatch.setattr(
        barcode_decoder.zxingcpp, "read_barcodes", read_barcodes
    )
    image = np.full((40, 80, 3), 255, dtype=np.uint8)
    success, encoded = cv2.imencode(".jpg", image)
    assert success

    results = decode_barcodes(
        [ImageVariant("original", encoded.tobytes(), "image/jpeg", 80, 40)]
    )

    assert len(results) == 1
    assert len(calls) == 1
    assert results[0].gtin == "0196604444156"
    assert results[0].valid_checksum is True


def test_decode_barcodes_skips_byte_identical_preprocessing_variants(monkeypatch):
    decoded = SimpleNamespace(text="0196604444156", format="EAN-13")
    calls = []
    monkeypatch.setattr(
        barcode_decoder.zxingcpp,
        "read_barcodes",
        lambda image: calls.append(image) or [decoded],
    )
    image = np.full((40, 80, 3), 255, dtype=np.uint8)
    success, encoded = cv2.imencode(".jpg", image)
    assert success
    data = encoded.tobytes()

    results = decode_barcodes(
        [
            ImageVariant("original", data, "image/jpeg", 80, 40),
            ImageVariant("perspective_corrected", data, "image/jpeg", 80, 40),
        ]
    )

    assert len(calls) == 1
    assert len(results) == 1
    assert results[0].source_variant == "original"


def test_invalid_barcode_check_digit_is_rejected():
    assert validate_gtin_check_digit("0196604444156") is True
    assert validate_gtin_check_digit("0196604444157") is False
    assert validate_gtin_check_digit("123") is False


def test_upce_is_expanded_before_checksum_validation():
    assert expand_upce("04210007") == "042000001007"
    assert validate_gtin_check_digit(expand_upce("04210007")) is True


def test_rotation_and_perspective_helpers_preserve_useful_geometry():
    image = np.zeros((100, 200, 3), dtype=np.uint8)
    rotated = rotate_image(image, 1)
    corrected = perspective_correct(
        image,
        np.array([[20, 20], [180, 10], [170, 80], [30, 90]], dtype=np.float32),
    )

    assert rotated.shape[:2] == (200, 100)
    assert corrected.shape[1] > corrected.shape[0]
    assert corrected.size > 0


@pytest.mark.parametrize("transformation", ["blurred", "rotated", "cropped", "low_contrast"])
def test_synthetic_degraded_images_generate_all_variants(transformation):
    image = np.full((500, 800, 3), 35, dtype=np.uint8)
    cv2.rectangle(image, (120, 150), (700, 380), (235, 235, 235), -1)
    cv2.putText(
        image,
        "QX1002 300   W 9   M 7.5",
        (160, 270),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (20, 20, 20),
        2,
    )
    if transformation == "blurred":
        image = cv2.GaussianBlur(image, (15, 15), 4)
    elif transformation == "rotated":
        matrix = cv2.getRotationMatrix2D((400, 250), 12, 1)
        image = cv2.warpAffine(image, matrix, (800, 500))
    elif transformation == "cropped":
        image = image[80:440, 60:750]
    elif transformation == "low_contrast":
        image = cv2.convertScaleAbs(image, alpha=0.25, beta=110)
    success, encoded = cv2.imencode(".jpg", image)
    assert success

    variants = preprocess_label_image(
        encoded.tobytes(), content_type="image/jpeg", filename="synthetic.jpg"
    )

    assert len(variants) == 5
    assert all(variant.data and variant.width > 0 and variant.height > 0 for variant in variants)


def test_invalid_type_and_oversized_image_are_rejected():
    with pytest.raises(ImageValidationError, match="JPEG, PNG, or WebP"):
        validate_image_bytes(b"not an image", filename="label.txt")
    with pytest.raises(ImageValidationError, match="too large"):
        validate_image_bytes(b"\xff\xd8\xff" + b"0" * 20, max_bytes=10)
