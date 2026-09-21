"""In-memory validation and preprocessing for Discord shoe-label photos."""

from __future__ import annotations

from dataclasses import dataclass
import os

import cv2
import numpy as np

from utils.performance import timed


DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES_ENV_VAR = "PHOTO_MAX_IMAGE_BYTES"
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ImageValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ImageVariant:
    name: str
    data: bytes
    mime_type: str
    width: int
    height: int


def configured_max_image_bytes() -> int:
    value = os.getenv(MAX_IMAGE_BYTES_ENV_VAR, str(DEFAULT_MAX_IMAGE_BYTES))
    try:
        maximum = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{MAX_IMAGE_BYTES_ENV_VAR} must be a whole number") from exc
    if maximum <= 0:
        raise ValueError(f"{MAX_IMAGE_BYTES_ENV_VAR} must be greater than zero")
    return maximum


def validate_image_bytes(
    image_bytes: bytes,
    *,
    content_type: str | None = None,
    filename: str | None = None,
    max_bytes: int | None = None,
) -> str:
    if not image_bytes:
        raise ImageValidationError("The attached image is empty.")

    maximum = max_bytes if max_bytes is not None else configured_max_image_bytes()
    if len(image_bytes) > maximum:
        raise ImageValidationError(
            f"The image is too large. Maximum size is {maximum / (1024 * 1024):.1f} MB."
        )

    detected_type = detect_image_type(image_bytes)
    if detected_type is None:
        name = f" `{filename}`" if filename else ""
        raise ImageValidationError(
            f"Unsupported image{name}. Attach a JPEG, PNG, or WebP image."
        )

    declared_type = str(content_type or "").split(";", 1)[0].strip().lower()
    if declared_type == "image/jpg":
        declared_type = "image/jpeg"
    if declared_type and declared_type not in SUPPORTED_IMAGE_TYPES:
        raise ImageValidationError("Attach a JPEG, PNG, or WebP image.")
    return detected_type


def detect_image_type(image_bytes: bytes) -> str | None:
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


@timed("label_image_preprocess")
def preprocess_label_image(
    image_bytes: bytes,
    *,
    content_type: str | None = None,
    filename: str | None = None,
    max_bytes: int | None = None,
) -> list[ImageVariant]:
    """Correct EXIF orientation and return useful transient image variants."""

    validate_image_bytes(
        image_bytes,
        content_type=content_type,
        filename=filename,
        max_bytes=max_bytes,
    )
    image = decode_oriented_image(image_bytes)

    quadrilateral = find_label_quadrilateral(image)
    if quadrilateral is None:
        quadrilateral = infer_label_from_qr(image)
    if quadrilateral is not None:
        cropped = crop_to_points(image, quadrilateral)
        perspective = perspective_correct(image, quadrilateral)
    else:
        cropped = image.copy()
        perspective = image.copy()

    grayscale = cv2.cvtColor(perspective, cv2.COLOR_BGR2GRAY)
    enhanced = enhance_label(grayscale)
    sources = [
        ("original", image),
        ("cropped_label", cropped),
        ("perspective_corrected", perspective),
        ("grayscale", grayscale),
        ("enhanced", enhanced),
    ]
    return [encode_variant(name, value) for name, value in sources]


def decode_oriented_image(image_bytes: bytes) -> np.ndarray:
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    # OpenCV applies JPEG EXIF orientation unless IMREAD_IGNORE_ORIENTATION is set.
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is None or image.size == 0:
        raise ImageValidationError("The attached image could not be decoded.")
    return image


def rotate_image(image: np.ndarray, quarter_turns_clockwise: int) -> np.ndarray:
    turns = quarter_turns_clockwise % 4
    if turns == 0:
        return image.copy()
    if turns == 1:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if turns == 2:
        return cv2.rotate(image, cv2.ROTATE_180)
    return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)


def find_label_quadrilateral(image: np.ndarray) -> np.ndarray | None:
    """Locate the label as the largest convex, wide four-sided contour in the photo."""

    height, width = image.shape[:2]
    image_area = float(height * width)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 140)
    edges = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (9, 5)),
        iterations=2,
    )
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    best_points = None
    best_score = 0.0
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        approximation = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            continue
        points = approximation.reshape(4, 2).astype(np.float32)
        area = abs(cv2.contourArea(points))
        if not image_area * 0.025 <= area <= image_area * 0.95:
            continue
        ordered = order_points(points)
        top_width = np.linalg.norm(ordered[1] - ordered[0])
        bottom_width = np.linalg.norm(ordered[2] - ordered[3])
        left_height = np.linalg.norm(ordered[3] - ordered[0])
        right_height = np.linalg.norm(ordered[2] - ordered[1])
        avg_width = (top_width + bottom_width) / 2
        avg_height = (left_height + right_height) / 2
        if avg_height <= 0 or avg_width / avg_height < 1.35:
            continue
        bounding_area = max(avg_width * avg_height, 1.0)
        rectangularity = min(area / bounding_area, 1.0)
        score = area * rectangularity
        if score > best_score:
            best_score = score
            best_points = points
    return best_points


def infer_label_from_qr(image: np.ndarray) -> np.ndarray | None:
    """Use a visible label QR as a crop anchor when the outer border is faint."""

    try:
        detected, points = cv2.QRCodeDetector().detect(image)
    except Exception:
        return None
    if not detected or points is None:
        return None

    qr_points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    x, y, width, height = cv2.boundingRect(qr_points.astype(np.int32))
    if width < 2 or height < 2:
        return None
    image_height, image_width = image.shape[:2]
    left = max(round(x - width * 9.5), 0)
    right = min(round(x + width * 2.2), image_width - 1)
    bottom = min(round(y + height * 2.0), image_height - 1)
    inferred_height = max(round((right - left) / 2.5), height * 3)
    top = max(bottom - inferred_height, 0)
    if right - left < width * 3 or bottom - top < height * 2:
        return None
    return np.array(
        [[left, top], [right, top], [right, bottom], [left, bottom]],
        dtype=np.float32,
    )


def crop_to_points(image: np.ndarray, points: np.ndarray) -> np.ndarray:
    x, y, width, height = cv2.boundingRect(points.astype(np.int32))
    padding = max(round(min(width, height) * 0.03), 2)
    x0 = max(x - padding, 0)
    y0 = max(y - padding, 0)
    x1 = min(x + width + padding, image.shape[1])
    y1 = min(y + height + padding, image.shape[0])
    return image[y0:y1, x0:x1].copy()


def perspective_correct(image: np.ndarray, points: np.ndarray) -> np.ndarray:
    ordered = order_points(np.asarray(points, dtype=np.float32))
    top_left, top_right, bottom_right, bottom_left = ordered
    width = max(
        int(round(np.linalg.norm(bottom_right - bottom_left))),
        int(round(np.linalg.norm(top_right - top_left))),
    )
    height = max(
        int(round(np.linalg.norm(top_right - bottom_right))),
        int(round(np.linalg.norm(top_left - bottom_left))),
    )
    if width < 2 or height < 2:
        return crop_to_points(image, ordered)

    destination = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    transform = cv2.getPerspectiveTransform(ordered, destination)
    return cv2.warpPerspective(image, transform, (width, height))


def order_points(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    ordered = np.zeros((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[0] = points[np.argmin(sums)]
    ordered[2] = points[np.argmax(sums)]
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered


def enhance_label(grayscale: np.ndarray) -> np.ndarray:
    if len(grayscale.shape) == 3:
        grayscale = cv2.cvtColor(grayscale, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    contrast = clahe.apply(grayscale)
    blurred = cv2.GaussianBlur(contrast, (0, 0), 2.0)
    return cv2.addWeighted(contrast, 1.8, blurred, -0.8, 0)


def encode_variant(name: str, image: np.ndarray) -> ImageVariant:
    success, encoded = cv2.imencode(
        ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 92]
    )
    if not success:
        raise ImageValidationError(f"Could not encode the {name} image variant.")
    height, width = image.shape[:2]
    return ImageVariant(
        name=name,
        data=encoded.tobytes(),
        mime_type="image/jpeg",
        width=width,
        height=height,
    )
