"""Text helpers shared by the import and the database migration."""
import re

# Answers that carry no information on a PCCF
EMPTY_ANSWERS = {"", "no change", "no changes", "none", "n/a", "na", "nil", "-", "--", "same", "no"}

PCCF_NOTE_PARTS = (("Scope", "scope_change_details"), ("Methodology", "methodology_change"),
                   ("Milestones", "milestone_comment"), ("Budget", "budget_notes"))


def _key(sentence):
    return re.sub(r"\s+", " ", sentence).strip().lower().rstrip(".!? ")


def _sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


def combine_notes(parts):
    """Merge labelled PCCF texts into one note.

    `parts` is a list of (label, text). "No change"-type answers are dropped, and any
    sentence already written in an earlier part is not repeated; a part with nothing
    new left is skipped.
    """
    seen, kept = set(), []
    for label, text in parts:
        text = (text or "").strip()
        if _key(text) in EMPTY_ANSWERS:
            continue
        new = [s for s in _sentences(text) if _key(s) not in seen]
        seen.update(_key(s) for s in new)
        if new:
            kept.append((label, " ".join(new)))
    if len(kept) == 1:
        return kept[0][1]
    return "\n\n".join(f"{label}: {text}" for label, text in kept)


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
