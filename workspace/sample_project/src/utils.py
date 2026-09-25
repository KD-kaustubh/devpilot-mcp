"""Formatting helpers."""


def format_price(value: float) -> str:
    # TODO: support currencies other than USD
    return f"${value:,.2f}"
