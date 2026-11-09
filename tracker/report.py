"""Monthly portfolio report: one summary of the month for the PM's manager.

`monthly_report(period)` gathers everything once; the report page and the Excel download both use it, so they
always show the same figures.
"""
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO

from django.utils import timezone

from .models import KPIFailure, MPSRReview, Owner, Project, StatusHistory
from .services import financials, milestones, spend_by_quarter

ZERO = Decimal("0")
REPORTING = [Project.Status.ACTIVE, Project.Status.ON_HOLD]


def month_bounds(period):
    """First and last day of the month that `period` (any date in it) falls in."""
    first = period.replace(day=1)
    last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    return first, last


@dataclass
class Row:
    """One project in the report."""
    fin: object
    mpsr_done: bool = False
    overdue: int = 0
    comment: str = ""
    comment_date: object = None
    underspent: bool = False

    @property
    def project(self):
        return self.fin.project

    @property
    def stage(self):
        st = self.fin.status
        return st.stage.name if st and st.stage_id else ""


@dataclass
class Issue:
    """One thing to act on, shown as a short tag that links to where it's fixed."""
    label: str
    url: str
    css: str        # pill style
    tip: str = ""


def issues_for(row, approaching):
    """Short, linked issues for one project, most pressing first."""
    p = row.project.pk
    mpsr = f"/mpsr/{p}/"
    out = []
    for i in row.fin.kpi_issues:
        out.append(Issue(f"⚑ KPI issue: {i.short}", f"{mpsr}#kpi-issues", "over", i.text))
    for r in row.fin.pccf_reasons:
        what = "cash flow" if r.is_cash else r.label.replace(" performance", "")
        out.append(Issue(f"⚠ PCCF: {what}", f"{mpsr}#{'mpsr-cash' if r.is_cash else 'mpsr-dates'}", "warn", r.text))
    for w in approaching:
        when = "today" if w.days == 0 else f"in {w.days} day{'s' if w.days != 1 else ''}"
        out.append(Issue(f"⏱ {w.label.replace(' performance', '')} {when}", f"{mpsr}#mpsr-dates", "blue", w.text))
    if row.overdue:
        out.append(Issue(f"✗ {row.overdue} overdue", f"{mpsr}#mpsr-actuals", "overdue",
                         "Milestones whose forecast date passed with no actual date"))
    if row.fin.budget_health == "over":
        out.append(Issue("▲ Over budget", f"/projects/{p}/?tab=budget", "over",
                         f"Committed + pending exceed the budget by ${-row.fin.remaining_budget:,.2f}"))
    if row.underspent:
        out.append(Issue("▼ Under-spent", f"/projects/{p}/#cash-flow", "warn",
                         "Spend so far this year trails the forecast"))
    return out


@dataclass
class StageChange:
    project: Project
    when: object
    before: str
    after: str


@dataclass
class MonthlyReport:
    period: dt.date
    first: dt.date
    last: dt.date
    owner: object
    rows: list
    stage_changes: list
    kpi_failures_this_month: list
    approaching: list
    generated: object = field(default_factory=timezone.now)

    def _sum(self, attr):
        return sum((getattr(r.fin, attr) for r in self.rows), ZERO)

    @property
    def totals(self):
        return {k: self._sum(k) for k in ("budget_with_fee", "budget", "commitments", "pending", "invoiced",
                                          "remaining_budget", "vow_total")}

    @property
    def mpsr_done(self):
        return sum(r.mpsr_done for r in self.rows)

    @property
    def pccf_rows(self):
        return [r for r in self.rows if r.fin.pccf_required]

    @property
    def kpi_issue_rows(self):
        return [r for r in self.rows if r.fin.kpi_issues]

    @property
    def kpi_issue_count(self):
        return sum(len(r.fin.kpi_issues) for r in self.rows)

    @property
    def kpi_failure_count(self):
        return sum(len(r.fin.kpi_failures) for r in self.rows)

    @property
    def over_budget_rows(self):
        return [r for r in self.rows if r.fin.budget_health == "over"]

    @property
    def underspent_rows(self):
        return [r for r in self.rows if r.underspent]

    @property
    def overdue_count(self):
        return sum(r.overdue for r in self.rows)

    @property
    def action_rows(self):
        """[(row, [Issue])] for every project with something to act on, most issues first."""
        near = {}
        for w, f in self.approaching:
            near.setdefault(f.project.pk, []).append(w)
        out = [(r, issues_for(r, near.get(r.project.pk, []))) for r in self.rows]
        return sorted([x for x in out if x[1]], key=lambda x: (-len(x[1]), x[0].project.io_number))


def monthly_report(period=None):
    today = timezone.localdate()
    first, last = month_bounds(period or today)
    projects = list(Project.objects.filter(status__in=REPORTING).select_related("current_status__stage")
                    .order_by("io_number"))
    fins = financials(projects)
    done = set(MPSRReview.objects.filter(period=first, project__in=projects).values_list("project_id", flat=True))

    overdue = {}
    for e in milestones(projects):
        if e.overdue(today):
            overdue[e.project.pk] = overdue.get(e.project.pk, 0) + 1

    rows = []
    for f in fins:
        st = f.status
        last_comment = (StatusHistory.objects.filter(project=f.project, kind=StatusHistory.Kind.COMMENT,
                                                     changed_at__date__lte=last).order_by("-changed_at").first())
        spend = spend_by_quarter(f.project, st, f.pccf, today)
        rows.append(Row(fin=f, mpsr_done=f.project.pk in done, overdue=overdue.get(f.project.pk, 0),
                        comment=(last_comment.text if last_comment else (st.comments if st else "")) or "",
                        comment_date=last_comment.changed_at.date() if last_comment else None,
                        underspent=spend["underspent"]))

    return MonthlyReport(
        period=first, first=first, last=last, owner=Owner.current(), rows=rows,
        stage_changes=_stage_changes(projects, first, last),
        kpi_failures_this_month=list(KPIFailure.objects.filter(project__in=projects, marked_at__date__gte=first,
                                                               marked_at__date__lte=last)
                                     .select_related("project").order_by("project__io_number")),
        approaching=sorted(((w, f) for f in fins for w in f.approaching), key=lambda x: x[0].days))


def _stage_changes(projects, first, last):
    """Stage changes recorded during the month, from the MPSR history (each entry stores the stage name)."""
    out = []
    history = (StatusHistory.objects.filter(project__in=projects, kind=StatusHistory.Kind.ACTUALS,
                                            changed_at__date__lte=last)
               .select_related("project").order_by("project_id", "changed_at"))
    previous = {}
    for h in history:
        stage = (h.data or {}).get("stage")
        if not stage:
            continue
        before = previous.get(h.project_id)
        if h.changed_at.date() >= first and before is not None and stage != before:
            out.append(StageChange(h.project, h.changed_at.date(), before, stage))
        previous[h.project_id] = stage
    return sorted(out, key=lambda c: (c.when, c.project.io_number))


# ---------------------------------------------------------------- Excel

def report_workbook(report):
    """The report as an .xlsx file (bytes): Summary, Projects, Actions and Stage changes sheets."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    money = '#,##0.00;(#,##0.00)'
    bold, head_fill = Font(bold=True), PatternFill("solid", fgColor="E6EFFB")
    wb = Workbook()

    def sheet(ws, headers, rows, widths, money_cols=()):
        ws.append(headers)
        for c in ws[1]:
            c.font, c.fill = bold, head_fill
        for r in rows:
            ws.append(r)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        for col in money_cols:
            for cell in ws[get_column_letter(col)][1:]:
                cell.number_format = money
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=isinstance(cell.value, str) and len(cell.value) > 40)
        ws.freeze_panes = "A2"

    # Summary
    ws = wb.active
    ws.title = "Summary"
    t = report.totals
    who = str(report.owner.person) if report.owner else ""
    ws.append([f"Monthly report · {report.first:%B %Y}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"{who} · generated {timezone.localtime(report.generated):%Y-%m-%d %H:%M}"])
    ws.append([])
    for label, value, is_money in [
        ("Active and on-hold projects", len(report.rows), False),
        ("Total budget incl. fee", t["budget_with_fee"], True), ("Budget (no fee)", t["budget"], True),
        ("Committed", t["commitments"], True), ("Pending change orders", t["pending"], True),
        ("Invoiced", t["invoiced"], True), ("Uncommitted", t["remaining_budget"], True),
        ("Value of work", t["vow_total"], True),
        ("MPSR done this month", f"{report.mpsr_done} of {len(report.rows)}", False),
        ("PCCF required", len(report.pccf_rows), False), ("KPI issues", report.kpi_issue_count, False),
        ("KPI failures recorded", report.kpi_failure_count, False),
        ("Approaching targets (15 days)", len(report.approaching), False),
        ("Overdue milestones", report.overdue_count, False), ("Over budget", len(report.over_budget_rows), False),
        ("Under-spent so far this year", len(report.underspent_rows), False),
        ("Stage changes this month", len(report.stage_changes), False),
    ]:
        ws.append([label, value])
        if is_money:
            ws.cell(ws.max_row, 2).number_format = money
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 34, 20

    # Projects
    sheet(wb.create_sheet("Projects"),
          ["IO #", "BGIS #", "Title", "Stage", "Budget incl. fee", "Committed", "Invoiced", "Uncommitted",
           "Budget health", "PCCF required", "KPI issues", "KPI failures", "Overdue milestones", "MPSR done",
           "Last comment", "Comment"],
          [[r.project.io_number, r.project.bgis_number, r.project.title, r.stage, r.fin.budget_with_fee,
            r.fin.commitments, r.fin.invoiced, r.fin.remaining_budget, _health(r.fin.budget_health),
            "Yes" if r.fin.pccf_required else "", len(r.fin.kpi_issues) or "", len(r.fin.kpi_failures) or "",
            r.overdue or "", "Yes" if r.mpsr_done else "No", r.comment_date, r.comment]
           for r in report.rows],
          [11, 11, 38, 14, 16, 16, 16, 16, 15, 9, 9, 9, 9, 9, 12, 70], money_cols=(5, 6, 7, 8))

    # Actions: everything that needs a decision, one line each
    actions = []
    for r in report.pccf_rows:
        actions += [[r.project.io_number, r.project.title, "PCCF required", x.text] for x in r.fin.pccf_reasons]
    for r in report.kpi_issue_rows:
        actions += [[r.project.io_number, r.project.title, "KPI issue", i.text] for i in r.fin.kpi_issues]
    for w, f in report.approaching:
        actions.append([f.project.io_number, f.project.title, "Approaching target", w.text])
    for r in report.over_budget_rows:
        actions.append([r.project.io_number, r.project.title, "Over budget",
                        f"Committed + pending exceed the budget by ${-r.fin.remaining_budget:,.2f}"])
    for r in report.underspent_rows:
        actions.append([r.project.io_number, r.project.title, "Under-spent", "Spend so far this year trails the forecast"])
    for k in report.kpi_failures_this_month:
        actions.append([k.project.io_number, k.project.title, "KPI failure recorded",
                        f"{k.get_milestone_display()} target {k.target}: {k.note}"])
    sheet(wb.create_sheet("Actions"), ["IO #", "Title", "Type", "Detail"], actions, [11, 38, 20, 90])

    # Stage changes
    sheet(wb.create_sheet("Stage changes"), ["IO #", "Title", "Date", "From", "To"],
          [[c.project.io_number, c.project.title, c.when, c.before, c.after] for c in report.stage_changes],
          [11, 38, 12, 16, 16])

    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def _health(h):
    return {"ok": "On budget", "tight": "Tight", "full": "Fully committed", "over": "Over budget",
            "none": "No budget"}.get(h, h)
