from types import SimpleNamespace

from services.label_preprocessing import ImageVariant
from services.vision_label_extractor import (
    VisionLabelExtraction,
    VisibleSize,
    extract_visible_label,
    normalize_style_code,
    select_vision_variants,
)


class FakeResponses:
    def __init__(self, parsed):
        self.parsed = parsed
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(output_parsed=self.parsed)


def sample_extraction():
    return VisionLabelExtraction(
        brand="Aether",
        product_name="W Aether Meridian Pace 3",
        raw_style_code="QX1002 300",
        normalized_style_code="QX1002-300",
        upc_candidates=["0196604444156"],
        sizes=[
            VisibleSize(system="US_W", value="9"),
            VisibleSize(system="US_M", value="7.5"),
            VisibleSize(system="UK", value="6.5"),
        ],
        raw_visible_text="W AETHER MERIDIAN ... QX1002 300",
        warnings=[],
        unreadable_fields=[],
    )


def test_brand_style_code_normalization_preserves_raw_separation():
    assert normalize_style_code("QX1002 300") == "QX1002-300"
    assert normalize_style_code(" qx1002-300 ") == "QX1002-300"


def test_responses_api_uses_image_input_and_structured_output():
    parsed = sample_extraction()
    responses = FakeResponses(parsed)
    client = SimpleNamespace(responses=responses)
    variants = [
        ImageVariant("original", b"jpeg-one", "image/jpeg", 100, 50),
        ImageVariant("perspective_corrected", b"jpeg-two", "image/jpeg", 90, 40),
        ImageVariant("enhanced", b"jpeg-three", "image/jpeg", 90, 40),
    ]

    result = extract_visible_label(variants, client=client, model="vision-test-model")

    assert result == parsed
    assert responses.kwargs["model"] == "vision-test-model"
    assert responses.kwargs["text_format"] is VisionLabelExtraction
    user_content = responses.kwargs["input"][1]["content"]
    images = [entry for entry in user_content if entry["type"] == "input_image"]
    assert len(images) == 3
    assert all(entry["image_url"].startswith("data:image/jpeg;base64,") for entry in images)
    assert all(entry["detail"] == "high" for entry in images)


def test_select_vision_variants_deduplicates_identical_images():
    variants = [
        ImageVariant("original", b"same", "image/jpeg", 100, 50),
        ImageVariant("perspective_corrected", b"same", "image/jpeg", 100, 50),
        ImageVariant("enhanced", b"different", "image/jpeg", 100, 50),
    ]

    selected = select_vision_variants(variants)

    assert [variant.name for variant in selected] == ["original", "enhanced"]
