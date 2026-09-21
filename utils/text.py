"""Small text helpers shared by services and Discord cogs."""


def clean_text(value) -> str:
    return "" if value is None else str(value).strip()


def is_blank(value) -> bool:
    return clean_text(value) == ""


def to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
