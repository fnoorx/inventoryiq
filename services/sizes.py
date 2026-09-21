"""Normalize shoe sizes from StockX, Google Sheets and label text to one form."""

import re

from utils.text import clean_text


def normalize_size(value) -> str:
    """Return sizes like ``10``, ``9.5W`` or ``5Y`` from ``US M 10.0``, ``W 9.5``..."""

    text = clean_text(value).upper().replace("US ", "").replace("MEN'S", "MENS")
    text = re.sub(r"^MENS\s+", "", text)
    if not text:
        return ""

    women = re.fullmatch(r"W\s*(\d+(?:\.\d+)?)", text)
    if women:
        return trim_decimal_zero(women.group(1)) + "W"
    men = re.fullmatch(r"M\s*(\d+(?:\.\d+)?)", text)
    if men:
        return trim_decimal_zero(men.group(1))
    return trim_decimal_zero(re.sub(r"\s+", "", text))


def trim_decimal_zero(value: str) -> str:
    return re.sub(r"\.0($|[A-Z]+$)", r"\1", value)
