from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(value, decimals=0):
    """$1,234 / ($1,234) for negatives / – for empty."""
    if value is None or value == "":
        return "–"
    v = Decimal(value)
    if round(v, int(decimals)) == 0:
        v = Decimal(0)   # e.g. -0.30 shown to the dollar is just $0, not ($0)
    text = f"${abs(v):,.{int(decimals)}f}"
    return f"({text})" if v < 0 else text


@register.filter(name="abs")
def abs_(value):
    return abs(Decimal(value)) if value not in (None, "") else value


@register.filter
def fy(value):
    """Fiscal year ending 2025 -> '2024/25'."""
    from tracker.textutil import fiscal_label
    try:
        return fiscal_label(value)
    except (TypeError, ValueError):
        return value


@register.filter
def sub(value, arg):
    return Decimal(value or 0) - Decimal(arg or 0)


@register.filter
def neg(value):
    try:
        return Decimal(value) <= Decimal("-0.5")   # a few cents below zero isn't worth flagging red
    except Exception:
        return False


@register.filter
def pct(value):
    return "–" if value is None else f"{Decimal(value):.0f}%"
