"""Discord-facing number formatting."""

from utils.text import to_float


def format_money(value, decimals: int = 2) -> str:
    number = to_float(value)
    return "N/A" if number is None else f"${number:.{decimals}f}"


def format_percent(value) -> str:
    number = to_float(value)
    return "N/A" if number is None else f"{number * 100:.2f}%"
