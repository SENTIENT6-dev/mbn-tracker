"""Merge the four PCCF text fields into one `notes` field and drop the change categories A-G."""
import re

from django.db import migrations, models

EMPTY = {"", "no change", "no changes", "none", "n/a", "na", "nil", "-", "--", "same", "no"}
PARTS = (("Scope", "scope_change_details"), ("Methodology", "methodology_change"),
         ("Milestones", "milestone_comment"), ("Budget", "budget_notes"))


def _key(s):
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".!? ")


def combine(parts):
    # Kept as a copy here (not imported) so the migration stays the same if the app code changes.
    seen, kept = set(), []
    for label, text in parts:
        text = (text or "").strip()
        if _key(text) in EMPTY:
            continue
        new = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip() and _key(s) not in seen]
        seen.update(_key(s) for s in new)
        if new:
            kept.append((label, " ".join(new)))
    if len(kept) == 1:
        return kept[0][1]
    return "\n\n".join(f"{label}: {text}" for label, text in kept)


def forwards(apps, schema_editor):
    PCCF = apps.get_model("tracker", "PCCF")
    for p in PCCF.objects.all():
        p.notes = combine([(label, getattr(p, field)) for label, field in PARTS])
        p.save(update_fields=["notes"])


CATEGORY_FIELDS = [f"cat_{c}{s}" for c in "abcdefg" for s in ("", "_selected")]


class Migration(migrations.Migration):
    dependencies = [("tracker", "0002_area_label")]

    operations = [
        migrations.AddField(model_name="pccf", name="notes",
                            field=models.TextField(blank=True, help_text="Scope, methodology, milestone and budget changes")),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        *[migrations.RemoveField(model_name="pccf", name=f) for f in
          ("scope_change_details", "methodology_change", "milestone_comment", "budget_notes", *CATEGORY_FIELDS)],
        migrations.AlterField(model_name="pccf", name="fiscal_year",
                              field=models.PositiveSmallIntegerField(
                                  blank=True, null=True,
                                  help_text="Stored as the year the fiscal year ends (2025 = 2024/25)")),
    ]
