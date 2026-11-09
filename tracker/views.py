import calendar as pycal
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import forms as F
from .textutil import current_fiscal_year, fiscal_label
from .models import (PCCF, ChangeOrder, Invoice, KPIFailure, MPSRReview, Project, ProjectStatus,
                     PurchaseOrder, StageGate, StatusHistory, ValueOfWork)
from .operations import save_change_order
from .services import (spend_by_quarter, approved_cash_for_year, approved_for_year, current_year_funding, financials, flagged_keys, forecast_is_stale, invoice_room, milestones, pccf_flag_message, po_rows, project_financials,
                       schedule_health)

ZERO = Decimal("0")
STATUS_FILTERS = {
    "active": [Project.Status.ACTIVE, Project.Status.ON_HOLD],
    "review": None,
    "all": None,
}


def _totals(rows, *attrs):
    return {a: sum((getattr(r, a) for r in rows), ZERO) for a in attrs}


# ------------------------------------------------------------------ overview

def overview(request):
    today = timezone.localdate()
    projects = Project.objects.filter(status__in=STATUS_FILTERS["active"]) \
        .select_related("project_manager", "current_status__stage")
    rows = financials(projects)
    events = milestones(projects)
    health = schedule_health(events, today)
    for f in rows:
        f.schedule = health.get(f.project.pk, {"overdue": 0, "next": None})
    totals = _totals(rows, "budget_with_fee", "budget", "commitments", "pending", "invoiced", "vow_total",
                     "remaining_budget", "to_be_billed")

    # Active projects per stage gate (bar chart)
    counts = {}
    for f in rows:
        st = getattr(f.project, "current_status", None)
        counts[st.stage_id if st else None] = counts.get(st.stage_id if st else None, 0) + 1
    stages = [{"name": s.name, "n": counts.get(s.pk, 0)} for s in StageGate.objects.all()]
    if counts.get(None):
        stages.append({"name": "Not set", "n": counts[None]})
    max_stage = max([s["n"] for s in stages] + [1])
    for s in stages:
        s["pct"] = s["n"] / max_stage * 100

    upcoming = [e for e in events if not e.done and today <= e.date <= today + timedelta(days=90)]
    overdue = [e for e in events if e.overdue(today)]
    attention = sorted(
        [f for f in rows if f.budget_health in ("over", "tight") or f.schedule["overdue"]],
        key=lambda f: (f.budget_health != "over", -f.schedule["overdue"], f.remaining_budget))

    committed_pct = totals["commitments"] / totals["budget"] * 100 if totals["budget"] else 0
    invoiced_pct = totals["invoiced"] / totals["budget"] * 100 if totals["budget"] else 0

    # Invoicing per month: the 24 months up to the latest invoice (all projects)
    latest = Invoice.objects.filter(date__lte=today).order_by("-date").values_list("date", flat=True).first() or today
    start = (latest.replace(day=1) - timedelta(days=700)).replace(day=1)
    per_month = {(r["m"].year, r["m"].month): r["t"] for r in
                 Invoice.objects.filter(date__gte=start, date__lte=latest).annotate(m=TruncMonth("date"))
                 .values("m").annotate(t=Sum("amount"))}
    months, d = [], start
    while d <= latest:
        months.append({"date": d, "t": per_month.get((d.year, d.month), ZERO)})
        d = (d + timedelta(days=32)).replace(day=1)
    mtop = max([m["t"] for m in months] + [Decimal(1)])
    for m in months:
        m["pct"] = float(m["t"] / mtop * 100)
    last12 = sum((m["t"] for m in months[-12:]), ZERO)

    # Portfolio cash flow this fiscal year: approved (latest PCCF) vs current forecast
    # Approved quarters only exist for PCCFs written for the same fiscal year as the forecast; projects whose
    # latest PCCF is from an earlier year have no approved quarterly split, so they're left out of both bars.
    split = [f for f in rows if f.approved and f.approved["q1"] is not None and f.status]
    cash = []
    for label, field in (("Q1", "q1"), ("Q2", "q2"), ("Q3", "q3"), ("Q4", "q4")):
        appr = sum((f.approved[field] for f in split), ZERO)
        fc = sum((getattr(f.status, field) for f in split), ZERO)
        cash.append({"label": label, "approved": appr, "forecast": fc})
    ctop = max([c["approved"] for c in cash] + [c["forecast"] for c in cash] + [Decimal(1)])
    for c in cash:
        c["a_pct"], c["f_pct"] = float(c["approved"] / ctop * 100), float(c["forecast"] / ctop * 100)

    # Top vendors by committed value on active projects
    vend = list(PurchaseOrder.objects.filter(project__in=projects).values("vendor_id", "vendor__name")
                .annotate(t=Sum("amount"), n=Count("id")).order_by("-t")[:8])
    vtop = max([v["t"] for v in vend] + [Decimal(1)])
    for v in vend:
        v["pct"] = float(v["t"] / vtop * 100)

    return render(request, "tracker/overview.html", {
        "months": months, "last12": last12, "cash": cash, "cash_projects": len(split), "vendors": vend, "latest_invoice": latest,
        "rows": sorted(rows, key=lambda f: -f.budget), "totals": totals, "stages": stages,
        "upcoming": upcoming[:12], "upcoming_count": len(upcoming), "overdue_count": len(overdue),
        "attention": attention[:8], "today": today, "pccf_count": sum(f.pccf_required for f in rows),
        "pccf_rows": [f for f in sorted(rows, key=lambda f: f.project.io_number) if f.pccf_required][:6],
        "approaching": sorted(((w, f) for f in rows for w in f.approaching), key=lambda x: x[0].days),
        "kpi_count": sum(len(f.kpi_failures) for f in rows),
        "kpi_issue_count": sum(len(f.kpi_issues) for f in rows),
        "stale_count": sum(f.forecast_stale for f in rows), "fy_label": fiscal_label(current_fiscal_year(today)),
        "committed_pct": committed_pct, "invoiced_pct": invoiced_pct,
        "open_cos": ChangeOrder.objects.filter(status__in=["requested", "approved"]).count(),
        "mpsr_done": MPSRReview.objects.filter(period=today.replace(day=1), project__in=projects).count(),
    })


# ------------------------------------------------------------------ calendar

MILESTONE_KEYS = {"design": "Design end", "tender": "Go to tender", "award": "Construction award",
                  "substantial": "Substantial", "closeout": "Closeout"}


def calendar_view(request):
    today = timezone.localdate()
    try:
        y, m = (int(x) for x in request.GET.get("m", "").split("-"))
        first = date(y, m, 1)
    except ValueError:
        first = today.replace(day=1)
    scope = request.GET.get("scope", "all")
    kinds = [k for k in request.GET.getlist("type") if k in MILESTONE_KEYS] or list(MILESTONE_KEYS)

    projects = Project.objects.select_related("current_status")
    if scope == "active":
        projects = projects.filter(status__in=STATUS_FILTERS["active"])
    events = [e for e in milestones(projects) if e.key in kinds]
    for e in events:
        e.css = e.state(today)

    cal = pycal.Calendar(firstweekday=6)  # weeks start on Sunday
    by_day = {}
    for e in events:
        by_day.setdefault(e.date, []).append(e)
    weeks = [[{"date": d, "in_month": d.month == first.month, "today": d == today, "events": by_day.get(d, [])}
              for d in week] for week in cal.monthdatescalendar(first.year, first.month)]

    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = (first + timedelta(days=32)).replace(day=1)
    overdue = [e for e in events if e.overdue(today)]
    upcoming = [e for e in events if e.css == "upcoming" and e.date <= today + timedelta(days=120)]
    month_events = [e for w in weeks for d in w if d["in_month"] for e in d["events"]]
    qs = "&".join([f"scope={scope}"] + [f"type={k}" for k in kinds])
    return render(request, "tracker/calendar.html", {
        "weeks": weeks, "first": first, "today": today, "prev": prev_m.strftime("%Y-%m"),
        "next": next_m.strftime("%Y-%m"), "this": today.strftime("%Y-%m"), "kinds": MILESTONE_KEYS,
        "selected": kinds, "scope": scope, "qs": qs, "overdue": overdue, "upcoming": upcoming,
        "month_count": len(month_events),
        "month_done": sum(e.css == "done" for e in month_events),
        "month_missed": sum(e.css == "missed" for e in month_events),
        "month_upcoming": sum(e.css == "upcoming" for e in month_events),
    })


# ------------------------------------------------------------------ project list

def dashboard(request):
    show = request.GET.get("show", "active")
    q = request.GET.get("q", "").strip()
    projects = Project.objects.select_related("project_manager", "current_status__stage")
    if show in ("active", "pccf", "approaching", "kpi_issue", "kpi"):
        projects = projects.filter(status__in=STATUS_FILTERS["active"])
    elif show == "review":
        projects = projects.filter(needs_review=True)
    if q:
        projects = projects.filter(Q(io_number__icontains=q) | Q(title__icontains=q) | Q(bgis_number__icontains=q))
    rows = financials(projects)
    on_active = show in ("active", "pccf", "approaching", "kpi_issue", "kpi")
    counts = {"pccf": sum(f.pccf_required for f in rows), "approaching": sum(bool(f.approaching) for f in rows),
              "kpi_issue": sum(bool(f.kpi_issues) for f in rows),
              "kpi": sum(bool(f.kpi_failures) for f in rows)} if on_active else {}
    keep = {"pccf": lambda f: f.pccf_required, "approaching": lambda f: f.approaching,
            "kpi_issue": lambda f: f.kpi_issues, "kpi": lambda f: f.kpi_failures}.get(show)
    if keep:
        rows = [f for f in rows if keep(f)]
    health = schedule_health(milestones(projects), timezone.localdate())
    for f in rows:
        f.schedule = health.get(f.project.pk, {"overdue": 0, "next": None})
    totals = _totals(rows, "budget_with_fee", "budget", "commitments", "pending", "invoiced", "remaining_budget",
                     "vow_total", "to_be_billed")
    return render(request, "tracker/projects.html", {"rows": rows, "totals": totals, "show": show, "q": q,
                                                     "counts": counts})


# ------------------------------------------------------------------ projects

def project_detail(request, pk):
    project = get_object_or_404(Project.objects.select_related("project_manager", "client", "building"), pk=pk)
    status, _ = ProjectStatus.objects.get_or_create(project=project)
    fin = project_financials(project)
    pos = po_rows(project.purchase_orders.select_related("vendor"))
    tab = request.GET.get("tab", "mpsr")
    today = timezone.localdate()

    # Stage stepper
    gates = list(StageGate.objects.all())
    cur = status.stage.order if status.stage else None
    stepper = [{"name": g.name, "state": "none" if cur is None else
                ("done" if g.order < cur else "current" if g.order == cur else "todo")} for g in gates]

    # Milestone timeline: position every date on one shared time axis
    events = [e for e in milestones([project])]
    for e in events:
        e.css = e.state(today)
    all_dates = [d for e in events for d in (e.approved, e.forecast, e.actual) if d] + [today]
    lo, hi = min(all_dates), max(all_dates)
    lo, hi = lo - timedelta(days=30), hi + timedelta(days=30)
    span = (hi - lo).days or 1

    def at(d):
        return round((d - lo).days / span * 100, 2) if d else None

    timeline = []
    for e in events:
        row = {"e": e, "approved": at(e.approved), "forecast": at(e.forecast), "actual": at(e.actual)}
        pts = [p for p in (row["approved"], row["actual"] or row["forecast"]) if p is not None]
        if len(pts) == 2:  # connector shows slip between approved and forecast/actual
            row["link_left"], row["link_width"] = min(pts), abs(pts[0] - pts[1])
        timeline.append(row)

    pccfs = list(project.pccfs.order_by("submission"))
    for prev, cur in zip([None] + pccfs, pccfs):
        cur.change = cur.total_budget - prev.total_budget if prev else None
    pccfs.reverse()
    years = [{"label": y, "pos": at(date(y, 1, 1))} for y in range(lo.year + 1, hi.year + 1)]

    # Cash flow: approved (PCCF) vs current forecast per quarter
    # (approved lined up with the forecast's fiscal year: a PCCF from last year has no quarters for this one)
    ap = approved_for_year(fin.pccf, status.fiscal_year)
    spend = spend_by_quarter(project, status, fin.pccf, today)       # actual = invoices in each quarter + fee
    quarters = spend["quarters"]
    for q in quarters:
        q["approved"] = ap[q["label"].lower()] if ap else None
    top = max([q["approved"] or ZERO for q in quarters] + [q["forecast"] for q in quarters]
              + [q["actual"] for q in quarters] + [Decimal("1")])
    for q in quarters:
        q["a_pct"] = float((q["approved"] or ZERO) / top * 100)
        q["f_pct"] = float(q["forecast"] / top * 100)
        q["x_pct"] = float(max(q["actual"], ZERO) / top * 100)

    ctx = {
        "project": project, "status": status, "fin": fin, "pos": pos, "tab": tab,
        "stepper": stepper, "timeline": timeline, "years": years, "today_pos": at(today),
        "quarters": quarters, "cash_top": top, "flagged": flagged_keys(fin), "ap": ap, "spend": spend,
        "approved_year": approved_cash_for_year(fin.pccf, status.fiscal_year)
        if fin.pccf and not forecast_is_stale(status) else None,
        "pccfs": pccfs, "has_charter": any(p.submission == 0 for p in pccfs),
        "change_orders": ChangeOrder.objects.filter(po__project=project).select_related("po__vendor"),
        "history": project.status_history.all()[:100],
        "vows": project.vow_entries.all()[:12],
        # forecasts, dates, comments, risk and value of work are edited on the MPSR page only (one set of rules)
        "reported": _reported(project) if tab == "history" else [],
    }
    return render(request, "tracker/project_detail.html", ctx)


def _reported(project):
    """The monthly MPSR snapshots, newest first, with the figures the History tab shows."""
    def dec(v):
        return Decimal(str(v)) if v not in (None, "") else None
    out = []
    for s in project.mpsr_snapshots.all()[:36]:
        d = s.data
        out.append({"period": s.period, "saved_at": s.saved_at, "d": d,
                    "fy_total": sum((dec(d.get(q)) or ZERO for q in ("q1", "q2", "q3", "q4")), ZERO),
                    "budget": dec(d.get("budget_with_fee")), "committed": dec(d.get("committed")),
                    "invoiced": dec(d.get("invoiced")), "vow": dec(d.get("vow_total"))})
    return out


def _log(project, kind, text="", data=None):
    StatusHistory.objects.create(project=project, kind=kind, changed_at=timezone.now(), text=text, data=data or {})


def _jsonable(form):
    """Same shape as the imported Access history: numbers as numbers, dates as ISO text."""
    out = {}
    for k, v in form.cleaned_data.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif v is None or isinstance(v, (bool, int, float, str)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


@require_POST
def project_update(request, pk, section):
    """Every MPSR update writes the new value and a history entry in one transaction."""
    project = get_object_or_404(Project, pk=pk)
    status, _ = ProjectStatus.objects.get_or_create(project=project)
    K = StatusHistory.Kind
    sections = {
        "cash": (F.CashFlowForm, K.CASH_FLOW, "Cash flow updated"),
        "dates": (F.ForecastDatesForm, K.DATES, "Forecast dates updated"),
        "act": (F.ActualsForm, K.ACTUALS, "Actual dates and stage updated"),
    }
    with transaction.atomic():
        if section in sections:
            form_cls, kind, msg = sections[section]
            form = form_cls(request.POST, instance=status, prefix=section)
            if form.is_valid():
                before = project_financials(project)
                form.save()
                data = _jsonable(form)
                if "stage" in data and form.cleaned_data.get("stage"):
                    data["stage"] = form.cleaned_data["stage"].name
                _log(project, kind, data=data)
                messages.success(request, msg)
                flag = pccf_flag_message(before, project_financials(project))
                if flag:
                    getattr(messages, flag[0])(request, flag[1])
            else:
                messages.error(request, f"Not saved: {form.errors.as_text()}")
        elif section in ("comment", "risk"):
            form = F.CommentForm(request.POST, prefix=section)
            if form.is_valid():
                text = form.cleaned_data["text"]
                if section == "comment":
                    status.comments = text
                else:
                    status.risk = text
                status.save()
                _log(project, K.COMMENT if section == "comment" else K.RISK, text=text)
                messages.success(request, f"{section.title()} updated")
        elif section == "vow":
            form = F.VOWForm(request.POST, prefix="vow")
            fin = project_financials(project)
            if form.is_valid():
                entry = form.to_entry(project, fin.fee_percent, current_year_funding(fin.pccf, status))
                ValueOfWork.objects.update_or_create(project=project, reporting_date=entry.pop("reporting_date"),
                                                     defaults={k: v for k, v in entry.items() if k != "project"})
                messages.success(request, "Value of work recorded")
            else:
                messages.error(request, f"Not saved: {form.errors.as_text()}")
    return redirect(f"/projects/{pk}/?tab=mpsr")


@require_POST
def tender_tick(request, pk):
    """Tick (or untick) the 'Go to tender' milestone. Ticking records today's date; edit it under actual dates."""
    project = get_object_or_404(Project, pk=pk)
    status, _ = ProjectStatus.objects.get_or_create(project=project)
    nxt = request.POST.get("next", "")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = f"/projects/{pk}/?tab=mpsr"
    if request.POST.get("action") == "undo":
        if status.actual_tender:
            with transaction.atomic():
                status.actual_tender = None
                status.save(update_fields=["actual_tender"])
                _log(project, StatusHistory.Kind.ACTUALS, text="Tender un-ticked", data={"actual_tender": None})
            messages.success(request, f"{project.io_number}: tender marked as not issued")
        return redirect(nxt)
    if not status.actual_design_end:
        messages.error(request, "Record the actual design end before ticking the tender.")
        return redirect(nxt)
    today = timezone.localdate()
    issued = max(today, status.actual_design_end)
    if request.POST.get("date"):                      # date picked in the confirm box
        try:
            issued = date.fromisoformat(request.POST["date"])
        except ValueError:
            messages.error(request, "Not saved: that tender date isn't a valid date.")
            return redirect(nxt)
        if issued < status.actual_design_end or issued > today:
            messages.error(request, f"Not saved: the tender date must be between the design end "
                                    f"({status.actual_design_end}) and today.")
            return redirect(nxt)
    with transaction.atomic():
        status.actual_tender = issued
        status.save(update_fields=["actual_tender"])
        _log(project, StatusHistory.Kind.ACTUALS, text=f"Tender issued {issued}",
             data={"actual_tender": issued.isoformat()})
    # the message at the top of the next page gets an Undo button (see base.html)
    messages.success(request, f"{project.io_number}: tender marked as issued {issued}",
                     extra_tags=f"undo-tender-{project.pk}")
    return redirect(nxt)


def _next(request, pk):
    nxt = request.POST.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else f"/projects/{pk}/"


@require_POST
def kpi_mark(request, pk, key):
    """Officially record a KPI issue (a missed target date) as a KPI failure, with the reason."""
    project = get_object_or_404(Project, pk=pk)
    nxt = _next(request, pk)
    issue = next((i for i in project_financials(project).kpi_issues if i.key == key), None)
    if issue is None:
        messages.error(request, "Not saved: only a missed target date (a KPI issue) can be marked as a KPI failure.")
        return redirect(nxt)
    note = request.POST.get("note", "").strip()[:500]
    if not note:
        messages.error(request, "Not saved: give the reason the target was missed.")
        return redirect(nxt)
    with transaction.atomic():
        k = KPIFailure.objects.create(project=project, milestone=key, target=issue.target,
                                      forecast=issue.actual or issue.forecast, note=note)
        _log(project, StatusHistory.Kind.ACTUALS, text=f"KPI failure: {issue.text}. Reason: {note}",
             data={"kpi_failure": key, "target": issue.target.isoformat()})
    messages.success(request, f"{project.io_number}: {issue.label} recorded as a KPI failure "
                              f"(target {issue.target}).",
                     extra_tags=f"undo:/projects/{pk}/kpi/{k.pk}/undo/")
    return redirect(nxt)


@require_POST
def kpi_undo(request, pk, kpi_id):
    k = get_object_or_404(KPIFailure, pk=kpi_id, project_id=pk)
    with transaction.atomic():
        k.delete()
        _log(k.project, StatusHistory.Kind.ACTUALS,
             text=f"KPI failure removed: {k.get_milestone_display()} target {k.target}",
             data={"kpi_failure_removed": k.milestone, "target": k.target.isoformat()})
    messages.success(request, f"{k.project.io_number}: {k.get_milestone_display()} KPI failure removed.")
    return redirect(_next(request, pk))


def project_form(request, pk=None):
    if pk is None:
        return project_new(request)
    project = get_object_or_404(Project, pk=pk)
    form = F.ProjectForm(request.POST or None, instance=project)
    if request.method == "POST" and form.is_valid():
        p = form.save()
        ProjectStatus.objects.get_or_create(project=p)
        messages.success(request, f"Project {p.io_number} saved")
        return redirect("project", pk=p.pk)
    return render(request, "tracker/project_edit.html", {"form": form})


def project_new(request):
    """A project is only created together with its charter (PCCF submission 0)."""
    form = F.ProjectForm(request.POST or None, prefix="p")
    charter = F.CharterForm(request.POST or None, prefix="c", initial={"date": timezone.localdate()})
    if request.method == "POST" and form.is_valid() and charter.is_valid():
        with transaction.atomic():
            p = form.save()
            c = charter.save(commit=False)
            c.project, c.submission = p, 0
            c.save()
            # Start the MPSR forecast from the approved charter, as the Access PIF import did
            ProjectStatus.objects.create(
                project=p, fiscal_year=c.fiscal_year, previous=c.previous, q1=c.q1, q2=c.q2, q3=c.q3, q4=c.q4,
                next_year=c.next_year, future=c.future, design_end=c.design_end,
                construction_award=c.construction_award, substantial=c.substantial, closeout=c.closeout)
        messages.success(request, f"Project {p.io_number} created with its charter "
                                  f"(budget ${c.total_budget:,.2f} incl. fee)")
        return redirect("project", pk=p.pk)
    return render(request, "tracker/project_new.html", {"form": form, "charter": charter})


FORECAST_TO_PCCF = ["fiscal_year", "previous", "q1", "q2", "q3", "q4", "next_year", "future",
                    "design_end", "construction_award", "substantial", "closeout"]


def pccf_form(request, pk, pccf_id=None):
    project = get_object_or_404(Project, pk=pk)
    instance = get_object_or_404(PCCF, pk=pccf_id, project=project) if pccf_id else None
    adding_charter = bool(request.GET.get("charter")) and not project.pccfs.filter(submission=0).exists()
    initial = {}
    if not instance:
        last = project.pccfs.order_by("-submission").first()
        if last and not adding_charter:  # start from the previous submission, like a PCCF does
            initial = {f.name: getattr(last, f.name) for f in PCCF._meta.fields
                       if f.name not in ("id", "project", "submission", "date", "final", "fiscal_year")}
        status = ProjectStatus.objects.filter(project=project).first()
        from_forecast = request.GET.get("from") == "forecast" and status is not None and not adding_charter
        if from_forecast:   # the new PCCF approves the current MPSR forecast: cash flow and dates
            for name in FORECAST_TO_PCCF:
                value = getattr(status, name)
                if value is not None:
                    initial[name] = value
        initial["date"] = timezone.localdate()
    form = F.PCCFForm(request.POST or None, instance=instance, initial=initial, project=project)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            obj.project = project
            if not instance:   # the number is generated: 0 for a missing charter, otherwise the next one
                top = PCCF.objects.select_for_update().filter(project=project).order_by("-submission") \
                    .values_list("submission", flat=True).first()
                obj.submission = 0 if adding_charter or top is None else top + 1
            obj.save()
        messages.success(request, f"PCCF {obj.submission} saved (total budget ${obj.total_budget:,.2f})")
        return redirect(f"/projects/{pk}/?tab=budget")
    last = project.pccfs.order_by("-submission").first()
    if instance:
        what = "charter (PCCF 0)" if instance.submission == 0 else f"PCCF {instance.submission}"
        title, heading = f"{project.io_number}: edit {what}", f"Edit {what}"
    else:
        nxt = 0 if adding_charter or last is None else last.submission + 1
        what = "charter (PCCF 0)" if nxt == 0 else f"PCCF {nxt}"
        title, heading = f"{project.io_number}: new {what}", f"New {what}"
    return render(request, "tracker/pccf_form.html", {
        "form": form, "title": title, "heading": heading, "project": project, "is_new": not instance,
        "from_forecast": not instance and request.GET.get("from") == "forecast",
        "prefilled": str(last.submission) if (last and not instance and not adding_charter) else "",
        "back": f"/projects/{pk}/?tab=budget"})


# ------------------------------------------------------------------ purchase orders

def po_list(request):
    q = request.GET.get("q", "").strip()
    pos = PurchaseOrder.objects.select_related("project", "vendor")
    if q:
        pos = pos.filter(Q(number__icontains=q) | Q(vendor__name__icontains=q) | Q(project__io_number__icontains=q))
    if request.GET.get("open"):
        pos = pos.filter(project__status__in=STATUS_FILTERS["active"])
    return render(request, "tracker/po_list.html", {"rows": po_rows(pos[:500]), "q": q,
                                                    "open": request.GET.get("open")})


def po_detail(request, number):
    po = get_object_or_404(PurchaseOrder.objects.select_related("project", "vendor"), number=number)
    inv_form = F.InvoiceForm(prefix="inv", po=po)
    co_form = F.ChangeOrderForm(prefix="co")
    if request.method == "POST":
        if "inv-number" in request.POST:
            inv_form = F.InvoiceForm(request.POST, prefix="inv", po=po)
            if inv_form.is_valid():
                inv = inv_form.save(commit=False)
                inv.po = po
                inv.save()
                messages.success(request, f"Invoice {inv.number} for ${inv.amount:,.2f} added")
                return redirect("po", number=number)
        elif "co-description" in request.POST:
            co_form = F.ChangeOrderForm(request.POST, prefix="co")
            if co_form.is_valid():
                co = co_form.save(commit=False)
                co.po = po
                co.save()
                messages.success(request, "Change order added")
                return redirect("po", number=number)
    row = po_rows([po])[0]
    from .views_manage import room_json
    return render(request, "tracker/po_detail.html", {
        "po": po, "row": row, "invoices": po.invoices.all(), "change_orders": po.change_orders.all(),
        "inv_form": inv_form, "co_form": co_form, "rooms": room_json(invoice_room([po]))})


@require_POST
def change_order_action(request, co_id, action):
    co = get_object_or_404(ChangeOrder.objects.select_related("po"), pk=co_id)
    S = ChangeOrder.State
    with transaction.atomic():
        if action == "process" and co.status == S.APPROVED:
            co.status = S.PROCESSED
            po = save_change_order(co, old_value=co.value, old_status=S.APPROVED)
            messages.success(request, f"PO {po.number} revised to ${po.amount:,.2f}")
        elif action == "approve" and co.status == S.REQUESTED:
            co.status = S.APPROVED
            co.save()
        elif action == "reject" and co.status in (S.REQUESTED, S.APPROVED):
            co.status = S.REJECTED
            co.save()
    nxt = request.POST.get("next")
    return redirect(nxt) if nxt else redirect("po", number=co.po.number)


def po_form(request, number=None):
    po = get_object_or_404(PurchaseOrder, number=number) if number else None
    initial = {"project": request.GET.get("project")} if not po else {}
    form = F.PurchaseOrderForm(request.POST or None, instance=po, initial=initial)
    if request.method == "POST" and form.is_valid():
        po = form.save()
        messages.success(request, f"PO {po.number} saved")
        return redirect("po", number=po.number)
    back = f"/pos/{number}/" if po else (f"/projects/{request.GET['project']}/?tab=pos"
                                          if request.GET.get("project", "").isdigit() else "/pos/")
    return render(request, "tracker/po_form.html", {"form": form, "title": f"Edit PO {number}" if po else "New PO",
                                                    "back": back})


def pending_changes(request):
    cos = ChangeOrder.objects.filter(status__in=[ChangeOrder.State.REQUESTED, ChangeOrder.State.APPROVED]) \
        .select_related("po__project", "po__vendor")
    return render(request, "tracker/pending.html", {"change_orders": cos})


