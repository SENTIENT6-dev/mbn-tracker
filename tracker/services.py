"""Financial figures, computed in one place (replaces the chained Access queries:
Commitments, ApprovedSpend, BudgetWithoutFee, SumOfPendingCO, CurrentVOW,
Project_Financials, MPSR, Contingencies, KahuaVoW, MPSR_Form_Query)."""
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from .models import PCCF, ChangeOrder, Invoice, Project, PurchaseOrder, ValueOfWork

ZERO = Decimal("0")
CONSULTANT, CONTRACTOR = "Consultant", "Contractor"


@dataclass(eq=False)
class Financials:
    project: Project
    pccf: PCCF = None
    vow: ValueOfWork = None
    commitments: Decimal = ZERO
    consultant_commitments: Decimal = ZERO
    contractor_commitments: Decimal = ZERO
    pending: Decimal = ZERO
    invoiced: Decimal = ZERO

    @property
    def budget_with_fee(self):
        return self.pccf.total_budget if self.pccf else ZERO

    @property
    def budget(self):
        return self.pccf.budget_without_fee if self.pccf else ZERO

    @property
    def fee_percent(self):
        return self.pccf.fee_percent if self.pccf else ZERO

    @property
    def remaining_budget(self):
        return self.budget - self.commitments - self.pending

    @property
    def unspent(self):
        return self.commitments + self.pending - self.invoiced

    @property
    def vow_total(self):
        return self.vow.total_no_fees if self.vow else ZERO

    @property
    def vow_previous(self):
        return self.vow.previous_years_no_fees if self.vow else ZERO

    @property
    def vow_current_year(self):
        return self.vow_total - self.vow_previous

    @property
    def vow_fees(self):
        return (self.vow_total * self.fee_percent / 100).quantize(Decimal("0.01"))

    @property
    def to_be_billed(self):
        return self.vow_total - self.invoiced

    @property
    def used_design_contingency(self):
        if not self.pccf:
            return ZERO
        return max(self.consultant_commitments - self.pccf.design_cost, ZERO)

    @property
    def used_construction_contingency(self):
        if not self.pccf:
            return ZERO
        return max(self.contractor_commitments - self.pccf.construction_cost, ZERO)

    @property
    def spent_percent(self):
        return (self.invoiced / self.budget * 100) if self.budget else None

    # ---- shares of the (no-fee) budget, for progress bars. Capped so bars never overflow.
    def _share(self, value):
        if not self.budget:
            return 0
        return float(max(min(value / self.scale * 100, 100), 0))

    @property
    def scale(self):
        """Bar length = the larger of budget and total committed, so overruns still fit."""
        return max(self.budget, self.commitments + self.pending, Decimal("1"))

    # The bar is the budget. Invoiced, committed-not-invoiced and pending fill it in that order; anything
    # committed beyond the budget is a separate red "over" segment, so the bar never needs a budget tick.
    def _segments(self):
        budget = self.budget
        inv = min(max(self.invoiced, ZERO), budget)
        com = min(max(self.commitments - self.invoiced, ZERO), budget - inv)
        pen = min(max(self.pending, ZERO), budget - inv - com)
        over = max(self.commitments + self.pending - budget, ZERO)
        if over < 1:   # cents of rounding aren't an overrun
            over = ZERO
        return inv, com, pen, over

    @property
    def bar_invoiced(self):
        return self._share(self._segments()[0])

    @property
    def bar_committed(self):
        return self._share(self._segments()[1])

    @property
    def bar_pending(self):
        return self._share(self._segments()[2])

    @property
    def bar_over(self):
        return self._share(self._segments()[3])

    @property
    def committed_pct(self):
        return float((self.commitments + self.pending) / self.budget * 100) if self.budget else None

    @property
    def invoiced_pct(self):
        return float(self.invoiced / self.budget * 100) if self.budget else None

    @property
    def over_amount(self):
        return self._segments()[3]

    @property
    def bar_tip(self):
        def m(v):
            return f"${v:,.0f}" if v >= 0 else f"(${-v:,.0f})"
        lines = [f"{self.project.io_number} · budget {m(self.budget)} (no fee)",
                 f"Invoiced: {m(self.invoiced)}",
                 f"Committed, not yet invoiced: {m(max(self.commitments - self.invoiced, ZERO))}"]
        if self.pending:
            lines.append(f"Pending change orders: {m(self.pending)}")
        lines.append(("Over budget by " if self.remaining_budget < 0 else "Uncommitted: ")
                     + m(abs(self.remaining_budget)))
        return "\n".join(lines)

    @property
    def budget_health(self):
        """ok / tight / over, based on budget left after commitments and pending COs."""
        if not self.budget:
            return "none"
        if self.remaining_budget <= -1:   # more than a dollar over (cents are rounding)
            return "over"
        if self.remaining_budget < self.budget * Decimal("0.05"):
            return "tight"
        return "ok"


def _sum_by(qs, key, value):
    return {r[key]: r["t"] or ZERO for r in qs.values(key).annotate(t=Sum(value))}


def financials(projects):
    """Financials for many projects using a handful of grouped queries."""
    projects = list(projects)
    ids = [p.id for p in projects]

    latest = {}
    for pc in PCCF.objects.filter(project_id__in=ids).order_by("project_id", "submission"):
        latest[pc.project_id] = pc
    vows = {}
    for v in ValueOfWork.objects.filter(project_id__in=ids).order_by("project_id", "reporting_date"):
        vows[v.project_id] = v

    pos = PurchaseOrder.objects.filter(project_id__in=ids)
    commit = _sum_by(pos, "project_id", "amount")
    consult = _sum_by(pos.filter(vendor__company_type__name=CONSULTANT), "project_id", "amount")
    contract = _sum_by(pos.filter(vendor__company_type__name=CONTRACTOR), "project_id", "amount")
    pending = _sum_by(ChangeOrder.objects.filter(po__project_id__in=ids, status=ChangeOrder.State.APPROVED),
                      "po__project_id", "value")
    invoiced = _sum_by(Invoice.objects.filter(po__project_id__in=ids), "po__project_id", "amount")

    return [Financials(project=p, pccf=latest.get(p.id), vow=vows.get(p.id),
                       commitments=commit.get(p.id, ZERO), consultant_commitments=consult.get(p.id, ZERO),
                       contractor_commitments=contract.get(p.id, ZERO), pending=pending.get(p.id, ZERO),
                       invoiced=invoiced.get(p.id, ZERO))
            for p in projects]


def project_financials(project):
    return financials([project])[0]


def po_rows(pos):
    """Purchase orders with invoiced / pending / remaining (was PO_Remaining_Amount)."""
    pos = list(pos)
    ids = [p.pk for p in pos]
    inv = _sum_by(Invoice.objects.filter(po_id__in=ids), "po_id", "amount")
    pend = _sum_by(ChangeOrder.objects.filter(po_id__in=ids, status=ChangeOrder.State.APPROVED), "po_id", "value")
    rows = []
    for p in pos:
        i, c = inv.get(p.pk, ZERO), pend.get(p.pk, ZERO)
        total = p.amount + c
        pct = float(i / total * 100) if total else 0.0
        rows.append({"po": p, "invoiced": i, "pending": c, "total": total, "remaining": total - i,
                     "pct": pct, "bar": min(max(pct, 0), 100)})
    return rows


SCHEDULE_ORDER = [("approval_date", "Charter approval"), ("design_start", "Design start"),
                  ("design_end", "Design end"), ("construction_award", "Construction award"),
                  ("substantial", "Substantial performance"), ("closeout", "Closeout")]


def schedule_problems(get):
    """Milestones that come before an earlier one. `get(field)` returns the date or None; blanks are skipped.

    Returns [(earlier_field, earlier_label, earlier_date, later_field, later_label, later_date), ...].
    """
    filled = [(f, label, get(f)) for f, label in SCHEDULE_ORDER if get(f)]
    out = []
    for i, (f, label, d) in enumerate(filled):
        prev = [p for p in filled[:i] if p[2] > d]
        if prev:
            pf, pl, pd = max(prev, key=lambda p: p[2])   # the latest earlier milestone it clashes with
            out.append((pf, pl, pd, f, label, d))
    return out


def flag_out_of_order_schedules():
    """Raise a data issue for each project whose current PCCF has milestone dates out of order."""
    from .models import DataIssue
    latest = {}
    for pc in PCCF.objects.select_related("project").order_by("project_id", "submission"):
        latest[pc.project_id] = pc
    made = 0
    for pc in latest.values():
        probs = schedule_problems(lambda f: getattr(pc, f))
        if not probs or DataIssue.objects.filter(category="Schedule dates out of order", resolved=False,
                                                 detail__pccf_id=pc.pk).exists():
            continue
        text = "; ".join(f"{ll} {ld} is before {pl} {pd}" for pf, pl, pd, lf, ll, ld in probs)
        DataIssue.objects.create(
            category="Schedule dates out of order", severity=DataIssue.Severity.MEDIUM, project=pc.project,
            summary=f"{pc.project.io_number} PCCF {pc.submission} (current budget): {text}. "
                    f"Edit the PCCF to correct the dates.",
            detail={"pccf_id": pc.pk})
        made += 1
    return made


def resolve_schedule_issue(pc):
    """Close the out-of-order flag for a PCCF once its dates are in order."""
    from .models import DataIssue
    if not schedule_problems(lambda f: getattr(pc, f)):
        DataIssue.objects.filter(category="Schedule dates out of order", resolved=False,
                                 detail__pccf_id=pc.pk).update(resolved=True,
                                                               resolution_note="Dates corrected on the PCCF.")


def invoice_room(pos, exclude_invoice=None):
    """How much can still be invoiced on each PO, for the add/edit invoice checks.

    Two limits apply, and the smaller one wins:
      * the PO: current value + approved (not yet processed) change orders - already invoiced
      * the project's budget after fees: latest PCCF budget without the fee - already invoiced on the project
    `exclude_invoice` leaves one invoice out of "already invoiced" (when editing it).
    Returns {po.pk: {...}}.
    """
    pos = list(pos)
    rows = {r["po"].pk: r for r in po_rows(pos)}
    fins = {f.project.pk: f for f in financials({p.project for p in pos})}
    ex = exclude_invoice
    out = {}
    for p in pos:
        r, f = rows[p.pk], fins[p.project_id]
        po_inv = r["invoiced"] - (ex.amount if ex and ex.po_id == p.pk else ZERO)
        proj_inv = f.invoiced - (ex.amount if ex and ex.po.project_id == p.project_id else ZERO)
        po_left = r["total"] - po_inv
        has_budget = f.pccf is not None
        proj_left = f.budget - proj_inv if has_budget else None
        avail = po_left if proj_left is None else min(po_left, proj_left)
        out[p.pk] = {
            "po": p.number, "project": p.project.io_number, "po_value": r["po"].amount, "po_pending": r["pending"],
            "po_invoiced": po_inv, "po_left": po_left, "has_budget": has_budget, "budget": f.budget,
            "budget_with_fee": f.budget_with_fee, "proj_invoiced": proj_inv, "proj_left": proj_left,
            "available": avail, "limit": "project" if proj_left is not None and proj_left < po_left else "po",
        }
    return out


def room_message(room, amount):
    """Explanation shown when an invoice is larger than what's available."""
    if room["limit"] == "project":
        return (f"Not enough budget for approval: this invoice is ${amount:,.2f} but project {room['project']} has "
                f"only ${room['proj_left']:,.2f} of budget left after fees. Get a revised PCCF approved first.")
    return (f"Not enough budget for approval: this invoice is ${amount:,.2f} but PO {room['po']} has only "
            f"${room['po_left']:,.2f} left. Get a change order approved for this PO first.")


MILESTONES = [  # key, label, forecast field, actual field, approved (PCCF) field
    ("design", "Design end", "design_end", "actual_design_end", "design_end"),
    ("award", "Construction award", "construction_award", "actual_construction_award", "construction_award"),
    ("substantial", "Substantial performance", "substantial", "actual_substantial", "substantial"),
    ("closeout", "Closeout", "closeout", "actual_closeout", "closeout"),
]


TENDER_DAYS = 30   # once design is actually finished, the project must go out to tender within this many days


def tender_deadline(status):
    """Deadline to go out to tender, always worked out from the current actual design end date."""
    if status is not None and status.actual_design_end:
        return status.actual_design_end + timedelta(days=TENDER_DAYS)
    return None


@dataclass
class Milestone:
    project: Project
    key: str
    label: str
    date: object            # date shown on the calendar: actual if done, else forecast, else approved
    forecast: object = None
    actual: object = None
    approved: object = None
    awarded: object = None  # tender only: construction award date, which means the tender already happened

    @property
    def is_deadline(self):
        """Tender's date is a deadline (worked out from design end), not a forecast someone entered."""
        return self.key == "tender"

    @property
    def late_days(self):
        """Tender only: days the tender went out (or, if still open, today) is past its deadline."""
        if not self.is_deadline or (self.awarded and not self.actual):
            return None
        days = ((self.actual or timezone.localdate()) - self.forecast).days
        return days if days > 0 else None

    @property
    def done(self):
        return self.actual is not None or self.awarded is not None

    def state(self, today):
        if self.done:
            return "done"
        return "upcoming" if self.date >= today else "missed"  # missed = date passed, no actual date

    def overdue(self, today):
        """Missed on a project that's still running, i.e. someone needs to act on it."""
        return self.state(today) == "missed" and self.project.status in (Project.Status.ACTIVE,
                                                                         Project.Status.ON_HOLD)

    @property
    def slip_days(self):
        """Days the forecast (or actual) is behind the approved PCCF date; negative = ahead."""
        target = self.actual or self.forecast
        if target and self.approved:
            return (target - self.approved).days
        return None


def milestones(projects):
    """Every milestone of the given projects, from the MPSR status and latest PCCF."""
    events = []
    for f in financials(projects):
        st = getattr(f.project, "current_status", None)
        for key, label, fc, ac, ap in MILESTONES:
            forecast = getattr(st, fc, None) if st else None
            actual = getattr(st, ac, None) if st else None
            approved = getattr(f.pccf, ap, None) if f.pccf else None
            date = actual or forecast or approved
            if date:
                events.append(Milestone(f.project, key, label, date, forecast, actual, approved))
        due = tender_deadline(st)
        if due:
            events.append(Milestone(f.project, "tender", "Go to tender", st.actual_tender or due,
                                    forecast=due, actual=st.actual_tender, awarded=st.actual_construction_award))
    return sorted(events, key=lambda e: (e.date, e.project.io_number))


def schedule_health(events, today):
    """Per project: overdue milestone count and next open milestone."""
    out = {}
    for e in events:
        h = out.setdefault(e.project.pk, {"overdue": 0, "next": None})
        if e.overdue(today):
            h["overdue"] += 1
        elif not e.done and h["next"] is None:
            h["next"] = e
    return out
