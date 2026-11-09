"""Monthly MPSR snapshots: a record of what was reported each month, and what changed since.

When a project's MPSR is saved for a month, `take(project, period)` stores the figures as they stand
(stage, cash-flow forecast, forecast and actual dates, comment, risk, value of work, budget and flags).
`changes(project, period)` compares today's figures with the latest snapshot from an earlier month.
"""
from dataclasses import dataclass
from decimal import Decimal

from .models import MPSRSnapshot, ProjectStatus
from .services import project_financials

# key, label, kind (money / date / text / count / flag)
FIELDS = [
    ("stage", "Stage", "text"),
    ("fiscal_year", "Forecast fiscal year", "text"),
    ("previous", "Forecast: previous years", "money"),
    ("q1", "Forecast: Q1", "money"), ("q2", "Forecast: Q2", "money"),
    ("q3", "Forecast: Q3", "money"), ("q4", "Forecast: Q4", "money"),
    ("next_year", "Forecast: next year", "money"), ("future", "Forecast: future", "money"),
    ("design_end", "Forecast design end", "date"), ("construction_award", "Forecast construction award", "date"),
    ("substantial", "Forecast substantial", "date"), ("closeout", "Forecast closeout", "date"),
    ("actual_design_end", "Actual design end", "date"), ("actual_tender", "Tender issued", "date"),
    ("actual_construction_award", "Actual construction award", "date"),
    ("actual_substantial", "Actual substantial", "date"), ("actual_closeout", "Actual closeout", "date"),
    ("comments", "Status comment", "text"), ("risk", "Risk", "text"),
    ("vow_total", "Value of work (no fees)", "money"),
    ("pccf", "Current PCCF", "text"), ("budget_with_fee", "Budget incl. fee", "money"),
    ("committed", "Committed", "money"), ("invoiced", "Invoiced", "money"),
    ("pccf_required", "PCCF required", "flag"), ("kpi_issues", "KPI issues", "count"),
    ("kpi_failures", "KPI failures", "count"),
]
LABELS = {k: (label, kind) for k, label, kind in FIELDS}


def _plain(v):
    """JSON-friendly: money as text with cents (exact), dates as ISO text."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return str(v.quantize(Decimal("0.01")))
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def current(project):
    """Today's figures for the project, in snapshot form."""
    st = ProjectStatus.objects.select_related("stage").filter(project=project).first()
    fin = project_financials(project)
    data = {}
    for key in ("fiscal_year", "previous", "q1", "q2", "q3", "q4", "next_year", "future",
                "design_end", "construction_award", "substantial", "closeout",
                "actual_design_end", "actual_tender", "actual_construction_award", "actual_substantial",
                "actual_closeout", "comments", "risk"):
        data[key] = _plain(getattr(st, key, None)) if st else None
    if st and st.fiscal_year:
        data["fiscal_year"] = st.fiscal_year_label
    data["stage"] = st.stage.name if st and st.stage_id else None
    data.update({
        "vow_total": _plain(fin.vow_total),
        "pccf": f"PCCF {fin.pccf.submission}" if fin.pccf else None,
        "budget_with_fee": _plain(fin.budget_with_fee), "committed": _plain(fin.commitments),
        "invoiced": _plain(fin.invoiced), "pccf_required": fin.pccf_required,
        "kpi_issues": len(fin.kpi_issues), "kpi_failures": len(fin.kpi_failures),
    })
    return data


def take(project, period):
    """Record (or replace) this month's snapshot for the project."""
    snap, _ = MPSRSnapshot.objects.update_or_create(project=project, period=period,
                                                    defaults={"data": current(project)})
    return snap


def previous(project, period):
    """The latest snapshot from a month before `period`, or None."""
    return MPSRSnapshot.objects.filter(project=project, period__lt=period).order_by("-period").first()


@dataclass
class Change:
    key: str
    label: str
    kind: str
    before: object
    after: object

    @property
    def is_long_text(self):
        return self.kind == "text" and self.key in ("comments", "risk")

    def show(self, value):
        if value in (None, ""):
            return "–"
        if self.kind == "money":
            d = Decimal(str(value))
            text = f"${abs(d):,.2f}"
            return f"({text})" if d < 0 else text
        if self.kind == "flag":
            return "Yes" if value else "No"
        return str(value)

    @property
    def before_text(self):
        return self.show(self.before)

    @property
    def after_text(self):
        return self.show(self.after)

    @property
    def difference(self):
        """Money only: after - before, shown with a sign."""
        if self.kind != "money" or self.before in (None, "") or self.after in (None, ""):
            return ""
        d = Decimal(str(self.after)) - Decimal(str(self.before))
        return ("+" if d > 0 else "−") + f"${abs(d):,.2f}"


def compare(before, after):
    """[Change] for every field that differs, in FIELDS order."""
    out = []
    for key, label, kind in FIELDS:
        b, a = before.get(key), after.get(key)
        if kind == "money" and b not in (None, "") and a not in (None, ""):
            if Decimal(str(a)) == Decimal(str(b)):
                continue
        elif (b or None) == (a or None):
            continue
        out.append(Change(key, label, kind, b, a))
    return out


def changes(project, period):
    """(previous snapshot, [Change]) comparing it with today's figures; (None, []) if none was saved yet."""
    prev = previous(project, period)
    if prev is None:
        return None, []
    return prev, compare(prev.data, current(project))
