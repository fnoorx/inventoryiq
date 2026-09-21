"""Strict OpenAI Responses API extraction for visible shoe-label evidence."""

from __future__ import annotations

import base64
import os
import re

from pydantic import BaseModel, ConfigDict

from services.label_preprocessing import ImageVariant
from utils.performance import record_event, timed


VISION_MODEL_ENV_VAR = "OPENAI_VISION_MODEL"
DEFAULT_VISION_MODEL = "gpt-4.1-mini"


class VisibleSize(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str | None
    value: str | None


class VisionLabelExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    brand: str | None
    product_name: str | None
    raw_style_code: str | None
    normalized_style_code: str | None
    upc_candidates: list[str]
    sizes: list[VisibleSize]
    raw_visible_text: str
    warnings: list[str]
    unreadable_fields: list[str]


class VisionExtractionError(RuntimeError):
    pass


VISION_PROMPT = """
Extract only text visibly printed on this shoe-box label. Never infer or invent
missing characters, digits, product names, or sizes. Use null for an unreadable
scalar field and list it in unreadable_fields. Preserve raw text for audit.

For sizes, return one entry per printed system and keep US women's (US_W), US
men's (US_M), US youth (US_Y), UK, EU, CM, and BR distinct. A women's US 9 and
men's US 7.5 are two separate entries, not a single chosen number. UPC candidates
must contain only digits you can actually read. For footwear style codes, preserve
raw_style_code and normalize spacing such as "QX1002 300" to "QX1002-300".
Do not claim confidence. Put ambiguity, glare, cropping, and disagreements among
the supplied image variants in warnings.
""".strip()


@timed("openai_vision_extract")
def extract_visible_label(
    variants: list[ImageVariant],
    *,
    client=None,
    model: str | None = None,
) -> VisionLabelExtraction:
    """Ask the vision model for structured label text, validated by Pydantic.

    Up to three preprocessing variants are sent as alternate views. The prompt
    forbids guessing, so unreadable fields come back null rather than invented.
    """

    if not variants:
        raise VisionExtractionError("No image variants were available for vision extraction.")

    if client is None:
        try:
            from openai import OpenAI

            client = OpenAI()
        except Exception as exc:
            raise VisionExtractionError(f"OpenAI vision is unavailable: {exc}") from exc

    selected = select_vision_variants(variants)
    image_content = [
        {
            "type": "input_image",
            "image_url": image_data_url(variant),
            "detail": "high",
        }
        for variant in selected
    ]
    try:
        response = client.responses.parse(
            model=(model or os.getenv(VISION_MODEL_ENV_VAR) or DEFAULT_VISION_MODEL).strip(),
            input=[
                {"role": "system", "content": VISION_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Transcribe and structure the visible shoe-label evidence. "
                                "The images are alternate views of the same attachment."
                            ),
                        },
                        *image_content,
                    ],
                },
            ],
            text_format=VisionLabelExtraction,
        )
    except Exception as exc:
        raise VisionExtractionError(f"OpenAI vision extraction failed: {exc}") from exc

    usage = getattr(response, "usage", None)
    record_event(
        "openai_vision_usage",
        images=len(selected),
        input_tokens=usage_field(usage, "input_tokens"),
        output_tokens=usage_field(usage, "output_tokens"),
        total_tokens=usage_field(usage, "total_tokens"),
    )
    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        raise VisionExtractionError("OpenAI vision returned no structured extraction.")
    if not isinstance(parsed, VisionLabelExtraction):
        try:
            parsed = VisionLabelExtraction.model_validate(parsed)
        except Exception as exc:
            raise VisionExtractionError(
                f"OpenAI vision returned an invalid structured extraction: {exc}"
            ) from exc
    return parsed


def select_vision_variants(variants: list[ImageVariant]) -> list[ImageVariant]:
    by_name = {variant.name: variant for variant in variants}
    selected = [
        by_name[name]
        for name in ("original", "perspective_corrected", "enhanced")
        if name in by_name
    ]
    unique = []
    seen_data = set()
    for variant in selected or variants[:3]:
        if variant.data in seen_data:
            continue
        seen_data.add(variant.data)
        unique.append(variant)
    return unique


def usage_field(usage, name: str):
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None) if usage is not None else None


def image_data_url(variant: ImageVariant) -> str:
    encoded = base64.b64encode(variant.data).decode("ascii")
    return f"data:{variant.mime_type};base64,{encoded}"


def normalize_style_code(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text)
    match = re.search(
        r"\b((?=[A-Z0-9]{5,7}\b)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,7})[\s-]+(\d{3})\b",
        compact,
    )
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = re.fullmatch(
        r"((?=[A-Z0-9]{5,7}$)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{5,7})(\d{3})",
        re.sub(r"[^A-Z0-9]", "", compact),
    )
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return compact.replace(" ", "-")


def canonical_size_system(value: object) -> str:
    text = re.sub(r"[^A-Z0-9]", "_", str(value or "").strip().upper()).strip("_")
    aliases = {
        "W": "US_W",
        "WOMEN": "US_W",
        "WOMENS": "US_W",
        "WOMEN_S": "US_W",
        "USW": "US_W",
        "US_WOMENS": "US_W",
        "M": "US_M",
        "MEN": "US_M",
        "MENS": "US_M",
        "MEN_S": "US_M",
        "USM": "US_M",
        "US_MENS": "US_M",
        "Y": "US_Y",
        "YOUTH": "US_Y",
        "USY": "US_Y",
    }
    return aliases.get(text, text or "UNKNOWN")
