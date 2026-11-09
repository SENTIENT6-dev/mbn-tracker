"""MPSR mode: the monthly pass through every active project (what the Access MPSR form was for).

The PM opens each project in turn, reviews the figures, updates comments, risk, forecast cash flow,
dates, stage and value of work, and moves on. Each project is marked as done for the month.
"""
import datetime as dt
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import forms as F
from .models import MPSRReview, Project, ProjectStatus, StatusHistory, ValueOfWork
from .textutil import current_fiscal_year, fiscal_label
from .services import (PCCF_DATES, TENDER_DAYS, approved_cash_for_year, approved_for_year, current_year_funding, financials, flagged_keys, forecast_is_stale, milestones,
                       pccf_flag_message, project_financials)

ZERO = Decimal("0")
REPORTING = [Project.Status.ACTIVE, Project.Status.ON_HOLD]


def _period(request):
    """Reporting month from ?period=YYYY-MM, else this month (as its first day)."""
    try:
        y, m = (int(x) for x in request.GET.get("period", "").split("-"))
        return dt.date(y, m, 1)
    except ValueError:
        return timezone.localdate().replace(day=1)


def _projects():
    return list(Project.objects.filter(status__in=REPORTING).select_related("current_status__stage", "building")
                .order_by("io_number"))


def mpsr_list(request):
    period = _period(request)
    projects = _projects()
    done = dict(MPSRReview.objects.filter(period=period, project__in=projects)
                .values_list("project_id", "reviewed_at"))
    rows = financials(projects)
    today = timezone.localdate()
    events = milestones(projects)
    overdue = {}
    for e in events:
        if e.overdue(today):
            overdue[e.project.pk] = overdue.get(e.project.pk, 0) + 1
    last_comment = {}
    for h in StatusHistory.objects.filter(project__in=projects, kind=StatusHistory.Kind.COMMENT).order_by("changed_at"):
        last_comment[h.project_id] = h.changed_at
    for f in rows:
        f.reviewed_at = done.get(f.project.pk)
        f.overdue = overdue.get(f.project.pk, 0)
        f.last_comment = last_comment.get(f.project.pk)
    pending = [f for f in rows if not f.reviewed_at]
    prev_p = (period - dt.timedelta(days=1)).replace(day=1)
    next_p = (period + dt.timedelta(days=32)).replace(day=1)
    return render(request, "tracker/mpsr_list.html", {
        "rows": rows, "period": period, "done_count": len(rows) - len(pending), "total": len(rows),
        "pct": (len(rows) - len(pending)) / len(rows) * 100 if rows else 0,
        "first_pending": pending[0].project if pending else None,
        "prev": prev_p.strftime("%Y-%m"), "next": next_p.strftime("%Y-%m"),
        "is_current": period == today.replace(day=1), "pccf_count": sum(f.pccf_required for f in rows),
        "stale_count": sum(f.forecast_stale for f in rows), "fy_label": fiscal_label(current_fiscal_year(today))})


def _forms(status, fin, data=None):
    vow_initial = {"reporting_date": timezone.localdate(),
                   "previous_years_with_fees": (fin.vow_previous * (1 + fin.fee_percent / 100)).quantize(Decimal("0.01")),
                   "current_year_no_fees": fin.vow_current_year}
    return {
        "comment": F.MPSRTextForm(data, prefix="comment", initial={"text": status.comments}),
        "risk": F.MPSRTextForm(data, prefix="risk", initial={"text": status.risk}),
        "act": F.ActualsForm(data, instance=status, prefix="act"),
        "dates": F.ForecastDatesForm(data, instance=status, prefix="dates"),
        "cash": F.CashFlowForm(data, instance=status, prefix="cash"),
        "vow": F.VOWForm(data, prefix="vow", initial=vow_initial),
    }


def _date_fields(form, fin):
    """Each forecast date box with its approved (PCCF) date. Design end and substantial are the KPI target dates
    that need a PCCF when the forecast moves; `kpi` = that target is already recorded as a KPI failure."""
    targets = {key for key, _ in PCCF_DATES}
    failed = {(k.milestone, k.target) for k in fin.kpi_failures}
    out = []
    for bound in form:
        approved = getattr(fin.pccf, bound.name, None) if fin.pccf else None
        out.append({"field": bound, "approved": approved, "target": bound.name in targets,
                    "kpi": (bound.name, approved) in failed,
                    # design end: going to tender (or award) by this date means the target was met
                    "clear_by": approved + dt.timedelta(days=TENDER_DAYS) if approved else None})
    return out


def _jsonable(form):
    out = {}
    for k, v in form.cleaned_data.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif hasattr(v, "pk"):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _save(project, status, fin, forms, period):
    """Save what changed, log it in the history, and mark the project done for the month."""
    now, K, changed = timezone.now(), StatusHistory.Kind, []
    with transaction.atomic():
        for key, kind, label in (("act", K.ACTUALS, "stage/actual dates"), ("dates", K.DATES, "forecast dates"),
                                 ("cash", K.CASH_FLOW, "cash flow")):
            f = forms[key]
            if f.has_changed():
                f.save(commit=False)
                StatusHistory.objects.create(project=project, kind=kind, changed_at=now, data=_jsonable(f))
                changed.append(label)
        for key, kind, attr in (("comment", K.COMMENT, "comments"), ("risk", K.RISK, "risk")):
            text = forms[key].cleaned_data["text"].strip()
            if text != (getattr(status, attr) or "").strip():
                setattr(status, attr, text)
                StatusHistory.objects.create(project=project, kind=kind, changed_at=now, text=text)
                changed.append("status comment" if key == "comment" else "risk")
        status.save()

        v = forms["vow"]
        entry = v.to_entry(project, fin.fee_percent, current_year_funding(fin.pccf, status))
        last = fin.vow
        # only a real change creates a new entry (converting with/without fees can shift a few cents)
        if not last or abs(entry["total_no_fees"] - last.total_no_fees) > Decimal("1") or \
                abs(entry["previous_years_no_fees"] - last.previous_years_no_fees) > Decimal("1"):
            ValueOfWork.objects.update_or_create(
                project=project, reporting_date=entry["reporting_date"],
                defaults={k: entry[k] for k in ("total_no_fees", "previous_years_no_fees", "current_year_funding")})
            changed.append("value of work")
        MPSRReview.objects.update_or_create(project=project, period=period)
    return changed


def rollover(request):
    """Preview, then move forecasts set to an earlier fiscal year forward to the current one."""
    from . import rollover as R
    to_year = current_fiscal_year(timezone.localdate())
    statuses = R.stale_statuses(to_year)
    only = request.GET.get("project")
    if only:
        statuses = statuses.filter(project_id=only)
    plans = R.plan(statuses, to_year)
    if request.method == "POST":
        chosen = {int(x) for x in request.POST.getlist("project") if x.isdigit()}
        picked = [p for p in plans if p.project.pk in chosen]
        if not picked:
            messages.error(request, "Nothing rolled forward: no projects were ticked.")
            return redirect(request.get_full_path())
        backup = R.backup_database("rollover")
        R.apply(picked)
        messages.success(request, f"{len(picked)} forecast{'s' if len(picked) != 1 else ''} moved to "
                                  f"FY {fiscal_label(to_year)}." +
                                  (f" A backup was saved first: backups\\{backup.name}" if backup else ""))
        nxt = request.POST.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/mpsr/")
    return render(request, "tracker/rollover.html", {
        "plans": plans, "to_label": fiscal_label(to_year), "only": only,
        "next": request.GET.get("next", "")})


def mpsr_project(request, pk):
    period = _period(request)
    project = get_object_or_404(Project.objects.select_related("building"), pk=pk)
    status, _ = ProjectStatus.objects.get_or_create(project=project)
    fin = project_financials(project)
    forms = _forms(status, fin, request.POST if request.method == "POST" else None)

    order = [p.pk for p in _projects()]
    idx = order.index(project.pk) if project.pk in order else None
    done = set(MPSRReview.objects.filter(period=period).values_list("project_id", flat=True))
    q = f"?period={period:%Y-%m}"

    if request.method == "POST":
        if all(f.is_valid() for f in forms.values()):
            changed = _save(project, status, fin, forms, period)
            messages.success(request, f"{project.io_number} done for {period:%B %Y}" +
                             (f" · updated {', '.join(changed)}" if changed else " · no changes"))
            flag = pccf_flag_message(fin, project_financials(project))
            if flag:
                getattr(messages, flag[0])(request, flag[1])
            if "save_next" in request.POST:
                done.add(project.pk)
                nxt = next((i for i in order[(idx or 0) + 1:] + order[:idx or 0] if i not in done), None)
                return redirect(f"/mpsr/{nxt}/{q}" if nxt else f"/mpsr/{q}")
            return redirect(f"/mpsr/{project.pk}/{q}")
        messages.error(request, "Some entries need fixing before this can be saved; see the red notes below.")

    today = timezone.localdate()
    events = milestones([project])
    for e in events:
        e.css = e.state(today)
    history = project.status_history.filter(kind__in=["comment", "risk"])[:6]
    forecast_total = sum((getattr(status, f) for f in ("previous", "q1", "q2", "q3", "q4", "next_year", "future")),
                         ZERO)
    return render(request, "tracker/mpsr_project.html", {
        "project": project, "status": status, "fin": fin, "f": forms, "events": events, "history": history,
        "period": period, "q": q, "pos": (idx + 1) if idx is not None else None, "count": len(order),
        "prev_id": order[idx - 1] if idx else None,
        "next_id": order[idx + 1] if idx is not None and idx + 1 < len(order) else None,
        "reviewed": MPSRReview.objects.filter(project=project, period=period).first(),
        "forecast_total": forecast_total, "forecast_variance": forecast_total - fin.budget_with_fee,
        "flagged": flagged_keys(fin), "current_fy": current_fiscal_year(today),
        "ap": approved_for_year(fin.pccf, status.fiscal_year),
        "date_fields": _date_fields(forms["dates"], fin), "today": today,
        "approved_year": approved_cash_for_year(fin.pccf, status.fiscal_year)
        if fin.pccf and not forecast_is_stale(status) else None,
        "done_count": len(done & set(order))})
