"""Fiscal-year helpers (fiscal years run April to March and are stored by the year they end)."""


def current_fiscal_year(today=None):
    """The fiscal year we're in, as the year it ends (April 2026 - March 2027 -> 2027)."""
    import datetime as dt
    today = today or dt.date.today()
    return today.year + 1 if today.month >= 4 else today.year


def fiscal_label(year_ending):
    """2025 -> '2024/25' (fiscal years run April to March and are stored by the year they end)."""
    if not year_ending:
        return ""
    y = int(year_ending)
    return f"{y - 1}/{str(y)[-2:]}"
