"""Vendors, contacts, facilities, lookup lists, and edit/delete for every record."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import (Case, Count, DecimalField, IntegerField, OuterRef, ProtectedError, Q, Subquery, Sum,
                              Value, When)
from django.http import JsonResponse
from django.db.models.functions import Coalesce, ExtractYear, TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import forms as F
from . import operations as ops
from .models import (PCCF, Building, BuildingRole, ChangeOrder, Company, CompanyType, HeritageStatus, Invoice,
                     Person, Project, Property, PurchaseOrder, RoleType, StageGate, ValueOfWork)
from .services import financials, invoice_room, po_rows
from .textutil import fiscal_label

ZERO = Decimal("0")
MONEY = DecimalField(max_digits=16, decimal_places=2)


# ------------------------------------------------------------------ shared helpers

def edit_page(request, form, title, back, on_valid, wide=False, extra=None):
    """Render/save a form; `on_valid(form)` saves and returns the URL to go to."""
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                return redirect(on_valid(form))
        except IntegrityError as e:
            form.add_error(None, f"Couldn't save: {e}")
    ctx = {"form": form, "title": title, "back": back, "wide": wide}
    ctx.update(extra or {})
    return render(request, "tracker/form.html", ctx)


def delete_page(request, title, what, impacts, do_delete, back, done_url, confirm_text=None, note=None,
                options=None, blocked=False):
    """Confirmation page listing everything affected; the delete only runs on POST."""
    error = None
    if request.method == "POST":
        if confirm_text and request.POST.get("confirm", "").strip() != confirm_text:
            error = f"Type {confirm_text} exactly to confirm."
        else:
            try:
                do_delete(request.POST)
                messages.success(request, f"Deleted {what}.")
                return redirect(done_url)
            except (ValueError, ProtectedError) as e:
                error = str(e.args[0]) if isinstance(e, ValueError) else \
                    "It's still used by other records, so it can't be deleted."
    return render(request, "tracker/confirm_delete.html", {
        "title": title, "what": what, "impacts": [i for i in impacts if i[1]], "back": back,
        "confirm_text": confirm_text, "error": error, "note": note, "options": options or [], "blocked": blocked})


def _qs(request):
    """Current query string without the page number, for pagination links."""
    g = request.GET.copy()
    g.pop("page", None)
    return g.urlencode()


def _sum_sub(qs, field):
    return Coalesce(Subquery(qs.values("g").annotate(t=Sum(field)).values("t")[:1], output_field=MONEY),
                    Value(ZERO, output_field=MONEY))


# ------------------------------------------------------------------ vendors

SORTS = {"name": "name", "committed": "-committed", "invoiced": "-invoiced", "pos": "-po_count"}


def vendor_list(request):
    q, kind, sort = request.GET.get("q", "").strip(), request.GET.get("type", ""), request.GET.get("sort", "committed")
    pos = PurchaseOrder.objects.filter(vendor=OuterRef("pk")).annotate(g=Value(1))
    inv = Invoice.objects.filter(po__vendor=OuterRef("pk")).annotate(g=Value(1))
    cos = Company.objects.select_related("company_type").annotate(
        po_count=Count("purchase_orders", distinct=True), people_count=Count("people", distinct=True),
        committed=_sum_sub(pos, "amount"), invoiced=_sum_sub(inv, "amount"),
        project_count=Count("purchase_orders__project", distinct=True))
    if q:
        cos = cos.filter(Q(name__icontains=q) | Q(vendor_number__icontains=q) | Q(acronym__icontains=q))
    if kind:
        cos = cos.filter(company_type_id=kind)
    cos = cos.order_by(SORTS.get(sort, "-committed"), "name")
    page = Paginator(cos, 50).get_page(request.GET.get("page"))
    top = max([c.committed for c in page] + [Decimal(1)])
    for c in page:
        c.inv_bar = float(min(c.invoiced, c.committed) / top * 100)
        c.com_bar = float(max(c.committed - c.invoiced, ZERO) / top * 100)
    return render(request, "tracker/vendor_list.html", {
        "page": page, "q": q, "kind": kind, "sort": sort, "types": CompanyType.objects.all(),
        "total": cos.count(), "qs": _qs(request)})


def vendor_detail(request, pk):
    c = get_object_or_404(Company.objects.select_related("company_type"), pk=pk)
    pos = c.purchase_orders.select_related("project").order_by("-number")
    rows = po_rows(pos)
    committed = sum((r["po"].amount for r in rows), ZERO)
    invoiced = sum((r["invoiced"] for r in rows), ZERO)
    by_year = list(Invoice.objects.filter(po__vendor=c).annotate(y=ExtractYear("date")).values("y")
                   .annotate(t=Sum("amount")).order_by("y"))
    top = max([r["t"] for r in by_year] + [Decimal(1)])
    for r in by_year:
        r["pct"] = float(r["t"] / top * 100)
    projects = sorted({r["po"].project for r in rows}, key=lambda p: p.io_number)
    person_form = F.PersonForm(initial={"company": c}, prefix="p")
    merge_form = F.MergeForm(prefix="m", exclude=c)
    if request.method == "POST" and "p-first_name" in request.POST:
        person_form = F.PersonForm(request.POST, prefix="p")
        if person_form.is_valid():
            person_form.save()
            messages.success(request, "Contact added")
            return redirect("vendor", pk=pk)
    return render(request, "tracker/vendor_detail.html", {
        "c": c, "rows": rows, "committed": committed, "invoiced": invoiced, "by_year": by_year,
        "projects": projects, "people": c.people.all(), "client_projects": c.client_projects.all(),
        "person_form": person_form, "merge_form": merge_form, "in_use": ops.company_in_use(c),
        "pending": sum((r["pending"] for r in rows), ZERO)})


def vendor_form(request, pk=None):
    c = get_object_or_404(Company, pk=pk) if pk else None
    form = F.CompanyForm(request.POST or None, instance=c)

    def save(f):
        obj = f.save()
        messages.success(request, f"{obj.name} saved")
        return f"/vendors/{obj.pk}/"
    return edit_page(request, form, f"Edit {c.name}" if c else "New vendor / company",
                     f"/vendors/{pk}/" if pk else "/vendors/", save)


def vendor_delete(request, pk):
    c = get_object_or_404(Company, pk=pk)
    note = None
    if ops.company_in_use(c):
        note = ("This company still has POs or client projects, so it can't simply be deleted. "
                "Use “Merge into another company” on its page to move them first.")
    return delete_page(request, f"Delete {c.name}?", c.name, ops.company_impact(c),
                       lambda post: ops.delete_company(c), f"/vendors/{pk}/", "/vendors/", note=note)


@require_POST
def vendor_merge(request, pk):
    c = get_object_or_404(Company, pk=pk)
    form = F.MergeForm(request.POST, prefix="m", exclude=c)
    if form.is_valid():
        target = form.cleaned_data["target"]
        name = c.name
        ops.merge_company(c, target)
        messages.success(request, f"Merged {name} into {target.name}; its POs, projects and contacts moved over.")
        return redirect("vendor", pk=target.pk)
    messages.error(request, "Pick a company to merge into.")
    return redirect("vendor", pk=pk)


# ------------------------------------------------------------------ people

def person_form(request, pk=None):
    p = get_object_or_404(Person, pk=pk) if pk else None
    initial = {"company": request.GET.get("company")} if not p else {}
    form = F.PersonForm(request.POST or None, instance=p, initial=initial)

    def save(f):
        obj = f.save()
        messages.success(request, f"{obj} saved")
        return f"/vendors/{obj.company_id}/#contacts"
    back = f"/vendors/{p.company_id}/" if p else "/vendors/"
    return edit_page(request, form, f"Edit {p}" if p else "New contact", back, save)


def person_delete(request, pk):
    p = get_object_or_404(Person, pk=pk)
    return delete_page(request, f"Delete {p}?", str(p), ops.person_impact(p), lambda post: ops.delete_person(p),
                       f"/vendors/{p.company_id}/", f"/vendors/{p.company_id}/#contacts")


# ------------------------------------------------------------------ buildings

def building_list(request):
    q, prop = request.GET.get("q", "").strip(), request.GET.get("property", "").strip()
    only = request.GET.get("only", "")
    qs = Building.objects.select_related("property", "heritage_status") \
        .annotate(project_count=Count("projects", distinct=True), role_count=Count("roles", distinct=True))
    if q:
        qs = qs.filter(Q(asset_number__icontains=q) | Q(name__icontains=q) | Q(address__icontains=q) |
                       Q(city__icontains=q) | Q(postal_code__icontains=q))
    if prop:
        qs = qs.filter(property__number=prop)
    if only == "projects":
        qs = qs.filter(project_count__gt=0)
    page = Paginator(qs.order_by("asset_number"), 50).get_page(request.GET.get("page"))
    return render(request, "tracker/building_list.html", {
        "page": page, "q": q, "prop": prop, "only": only, "total": qs.count(), "qs": _qs(request),
        "with_projects": Building.objects.filter(projects__isnull=False).distinct().count()})


def building_detail(request, pk):
    b = get_object_or_404(Building.objects.select_related("property", "heritage_status"), pk=pk)
    role_form = F.BuildingRoleForm(prefix="r", building=b)
    if request.method == "POST":
        role_form = F.BuildingRoleForm(request.POST, prefix="r", building=b)
        if role_form.is_valid():
            r = role_form.save(commit=False)
            r.building = b
            r.save()
            messages.success(request, f"{r.person} added as {r.role}")
            return redirect("building", pk=pk)
    rows = financials(b.projects.select_related("current_status__stage"))
    roles = defaultdict(list)
    for r in b.roles.select_related("person__company", "role"):
        roles[r.role.name].append(r)
    return render(request, "tracker/building_detail.html", {
        "b": b, "rows": rows, "roles": dict(roles), "role_form": role_form,
        "siblings": b.property.buildings.exclude(pk=b.pk)[:30] if b.property else []})


def building_form(request, pk=None):
    b = get_object_or_404(Building, pk=pk) if pk else None
    initial = {"property_number": request.GET.get("property", ""),
               "asset_number": request.GET.get("asset", "")} if not b else {}
    from_project = bool(request.GET.get("asset")) and not b
    form = F.BuildingForm(request.POST or None, instance=b, initial=initial)

    def save(f):
        obj = f.save()
        linked = ops.relink_project_codes(obj)
        messages.success(request, f"Building {obj.asset_number} saved" +
                         (f"; linked {linked} project(s) that referenced it" if linked else "") +
                         (". You can close this tab and save the project." if from_project else ""))
        return f"/facilities/buildings/{obj.pk}/"
    return edit_page(request, form, f"Edit building {b.asset_number}" if b else "New building",
                     f"/facilities/buildings/{pk}/" if pk else "/facilities/", save, wide=True)


def building_search(request):
    """Suggestions for the building box on project forms."""
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": [], "exact": False})
    words = q.split()
    qs = Building.objects.all()
    for w in words:
        qs = qs.filter(Q(asset_number__icontains=w) | Q(name__icontains=w) | Q(address__icontains=w) |
                       Q(city__icontains=w))
    # exact asset numbers first, then ones that start with what was typed
    qs = qs.annotate(rank=Case(When(asset_number__iexact=q, then=Value(0)),
                               When(asset_number__istartswith=q, then=Value(1)), default=Value(2),
                               output_field=IntegerField())).order_by("rank", "asset_number")[:15]
    results = [{"asset": b.asset_number, "name": b.name, "where": ", ".join(x for x in (b.address, b.city) if x)}
               for b in qs]
    return JsonResponse({"results": results,
                         "exact": Building.objects.filter(asset_number__iexact=q).exists()})


def building_delete(request, pk):
    b = get_object_or_404(Building, pk=pk)
    return delete_page(request, f"Delete building {b.asset_number}?", f"building {b.asset_number}",
                       ops.building_impact(b), lambda post: ops.delete_building(b),
                       f"/facilities/buildings/{pk}/", "/facilities/")


@require_POST
def role_delete(request, pk):
    r = get_object_or_404(BuildingRole, pk=pk)
    bid = r.building_id
    r.delete()
    messages.success(request, "Contact removed from building")
    return redirect("building", pk=bid)


# ------------------------------------------------------------------ properties

def property_list(request):
    q = request.GET.get("q", "").strip()
    qs = Property.objects.annotate(building_count=Count("buildings", distinct=True),
                                   project_count=Count("buildings__projects", distinct=True))
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(name__icontains=q))
    page = Paginator(qs.order_by("number"), 50).get_page(request.GET.get("page"))
    return render(request, "tracker/property_list.html", {"page": page, "q": q, "total": qs.count(),
                                                          "qs": _qs(request)})


def property_detail(request, pk):
    p = get_object_or_404(Property, pk=pk)
    buildings = p.buildings.annotate(project_count=Count("projects")).order_by("asset_number")
    rows = financials(Project.objects.filter(building__property=p).select_related("building"))
    return render(request, "tracker/property_detail.html", {"p": p, "buildings": buildings, "rows": rows})


def property_form(request, pk=None):
    p = get_object_or_404(Property, pk=pk) if pk else None
    form = F.PropertyForm(request.POST or None, instance=p)

    def save(f):
        obj = f.save()
        messages.success(request, f"Property {obj.number} saved")
        return f"/facilities/properties/{obj.pk}/"
    return edit_page(request, form, f"Edit property {p.number}" if p else "New property",
                     f"/facilities/properties/{pk}/" if pk else "/facilities/properties/", save)


def property_delete(request, pk):
    p = get_object_or_404(Property, pk=pk)
    return delete_page(request, f"Delete property {p.number}?", f"property {p.number}", ops.property_impact(p),
                       lambda post: ops.delete_property(p), f"/facilities/properties/{pk}/",
                       "/facilities/properties/")


# ------------------------------------------------------------------ project / PO / invoice / CO / PCCF / VOW

def project_delete(request, pk):
    p = get_object_or_404(Project, pk=pk)
    return delete_page(request, f"Delete project {p.io_number}?", f"project {p.io_number}", ops.project_impact(p),
                       lambda post: ops.delete_project(p), f"/projects/{pk}/", "/projects/?show=all",
                       confirm_text=p.io_number,
                       note="This permanently removes the project and everything listed below. "
                            "To keep the record but hide it, set its status to Closed or Cancelled instead.")


def po_delete(request, number):
    po = get_object_or_404(PurchaseOrder, number=number)
    return delete_page(request, f"Delete PO {po.number}?", f"PO {po.number}", ops.po_impact(po),
                       lambda post: ops.delete_po(po), f"/pos/{number}/", f"/projects/{po.project_id}/?tab=pos",
                       confirm_text=str(po.number))


INVOICE_SORTS = {"date": ("-date", "-id"), "amount": ("-amount",), "number": ("number",), "po": ("po__number", "-date")}


def invoice_list(request):
    today = timezone.localdate()
    q = request.GET.get("q", "").strip()
    fy = request.GET.get("fy", "")
    start, end = request.GET.get("from", ""), request.GET.get("to", "")
    active = request.GET.get("active") == "1"
    sort = request.GET.get("sort", "date")

    qs = Invoice.objects.select_related("po__vendor", "po__project")
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(po__number__icontains=q) | Q(po__vendor__name__icontains=q) |
                       Q(po__project__io_number__icontains=q) | Q(po__project__title__icontains=q))
    fy_start = fy_end = None
    if fy.isdigit():  # fiscal year ending e.g. 2025 = Apr 2024 - Mar 2025
        fy_start, fy_end = date(int(fy) - 1, 4, 1), date(int(fy), 3, 31)
        qs = qs.filter(date__gte=fy_start, date__lte=fy_end)
    try:
        if start:
            qs = qs.filter(date__gte=date.fromisoformat(start))
        if end:
            qs = qs.filter(date__lte=date.fromisoformat(end))
    except ValueError:
        messages.error(request, "Dates must look like 2024-04-01.")
    if active:
        qs = qs.filter(po__project__status__in=[Project.Status.ACTIVE, Project.Status.ON_HOLD])

    total = qs.aggregate(t=Sum("amount"), n=Count("id"))
    # Monthly chart for the filtered set: the chosen fiscal year, or 24 months up to the latest invoice shown
    last = fy_end or qs.filter(date__lte=today).order_by("-date").values_list("date", flat=True).first() or today
    first = fy_start or (last.replace(day=1) - timedelta(days=700)).replace(day=1)
    per = {(r["m"].year, r["m"].month): r["t"] for r in
           qs.filter(date__gte=first, date__lte=last).annotate(m=TruncMonth("date")).values("m")
           .annotate(t=Sum("amount"))}
    months, d = [], first.replace(day=1)
    while d <= last:
        months.append({"date": d, "t": per.get((d.year, d.month), ZERO)})
        d = (d + timedelta(days=32)).replace(day=1)
    mtop = max([m["t"] for m in months] + [Decimal(1)])
    for m in months:
        m["pct"] = float(m["t"] / mtop * 100)

    vendors = list(qs.values("po__vendor_id", "po__vendor__name").annotate(t=Sum("amount"), n=Count("id"))
                   .order_by("-t")[:6])
    vtop = max([v["t"] for v in vendors] + [Decimal(1)])
    for v in vendors:
        v["pct"] = float(v["t"] / vtop * 100)

    this_fy = today.year + 1 if today.month >= 4 else today.year
    fy_options = [(y, fiscal_label(y)) for y in range(this_fy, 2015, -1)]
    page = Paginator(qs.order_by(*INVOICE_SORTS.get(sort, INVOICE_SORTS["date"])), 50).get_page(request.GET.get("page"))
    return render(request, "tracker/invoice_list.html", {
        "page": page, "q": q, "fy": fy, "start": start, "end": end, "active": active, "sort": sort,
        "total": total["t"] or ZERO, "count": total["n"], "months": months, "vendors": vendors,
        "fy_options": fy_options, "qs": _qs(request), "today": today,
        "filtered": any([q, fy, start, end, active]),
        "range_label": f"FY {fiscal_label(int(fy))}" if fy.isdigit() else
        (f"{months[0]['date']:%b %Y} – {months[-1]['date']:%b %Y}" if months else ""),
        "fy_label": fiscal_label(int(fy)) if fy.isdigit() else ""})


def invoice_new(request):
    initial = {"date": timezone.localdate()}
    po = PurchaseOrder.objects.filter(number=request.GET.get("po") or "").first()
    if po:
        initial["po"] = po
    form = F.NewInvoiceForm(request.POST or None, initial=initial, prefix="inv")
    if request.method == "POST" and form.is_valid():
        inv = form.save()
        messages.success(request, f"Invoice {inv.number} for ${inv.amount:,.2f} added to PO {inv.po.number}")
        return redirect("/invoices/" if "add_another" not in request.POST else "/invoices/new/")
    rooms = invoice_room(PurchaseOrder.objects.select_related("project"))
    return render(request, "tracker/invoice_new.html", {"form": form, "rooms": room_json(rooms)})


def room_json(rooms):
    """Invoice room per PO id, as plain numbers for the page's live check."""
    return {str(k): {f: (float(v) if isinstance(v, Decimal) else v) for f, v in r.items()} for k, r in rooms.items()}


def invoice_form(request, pk):
    inv = get_object_or_404(Invoice.objects.select_related("po"), pk=pk)
    form = F.InvoiceEditForm(request.POST or None, instance=inv)
    back = _safe_next(request) or f"/pos/{inv.po.number}/"

    def save(f):
        obj = f.save()
        messages.success(request, f"Invoice {obj.number} saved")
        return _safe_next(request) or f"/pos/{obj.po.number}/"
    return edit_page(request, form, f"Edit invoice {inv.number}", back, save)


def _safe_next(request):
    """A `next` return address, only if it's a path inside this app."""
    nxt = request.GET.get("next", "")
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else ""


def invoice_delete(request, pk):
    inv = get_object_or_404(Invoice.objects.select_related("po"), pk=pk)
    return delete_page(request, f"Delete invoice {inv.number}?",
                       f"invoice {inv.number} (${inv.amount:,.2f})", [], lambda post: inv.delete(),
                       _safe_next(request) or f"/pos/{inv.po.number}/",
                       _safe_next(request) or f"/pos/{inv.po.number}/")


def change_order_form(request, pk):
    co = get_object_or_404(ChangeOrder.objects.select_related("po"), pk=pk)
    old_value, old_status = co.value, co.status
    form = F.ChangeOrderEditForm(request.POST or None, instance=co)

    def save(f):
        po = ops.save_change_order(f.save(commit=False), old_value=old_value, old_status=old_status)
        messages.success(request, f"Change order saved · PO {po.number} value is ${po.amount:,.2f}")
        return f"/pos/{po.number}/"
    return edit_page(request, form, "Edit change order", f"/pos/{co.po.number}/", save)


def change_order_delete(request, pk):
    co = get_object_or_404(ChangeOrder.objects.select_related("po"), pk=pk)
    processed = co.status == ChangeOrder.State.PROCESSED
    options = [("reverse", f"Also take ${co.value:,.2f} back out of PO {co.po.number}'s value", True)] \
        if processed else []
    return delete_page(request, "Delete change order?", f"change order “{co.description}”", [],
                       lambda post: ops.delete_change_order(co, reverse=bool(post.get("reverse"))),
                       f"/pos/{co.po.number}/", f"/pos/{co.po.number}/", options=options,
                       note="This change order was already processed into the PO." if processed else None)


def pccf_delete(request, pk, pccf_id):
    pc = get_object_or_404(PCCF, pk=pccf_id, project_id=pk)
    only_one = pc.project.pccfs.count() == 1
    if pc.submission == 0 or only_one:
        # The charter is what the project was approved on; a project always keeps it.
        reason = ("PCCF 0 is the project's charter, so it can't be deleted. Edit it instead, or delete the whole "
                  "project if it shouldn't exist.") if pc.submission == 0 else \
            "This is the project's only PCCF, and a project must keep at least one."

        def refuse(post):
            raise ValueError(reason)
        return delete_page(request, f"Can't delete PCCF {pc.submission}", f"PCCF {pc.submission}", [], refuse,
                           f"/projects/{pk}/?tab=budget", f"/projects/{pk}/?tab=budget", note=reason, blocked=True)
    return delete_page(request, f"Delete PCCF {pc.submission}?", f"PCCF {pc.submission} of {pc.project.io_number}",
                       [], lambda post: pc.delete(), f"/projects/{pk}/?tab=budget", f"/projects/{pk}/?tab=budget",
                       note="If this is the latest submission, the previous one becomes the current budget.")


def vow_form(request, pk):
    v = get_object_or_404(ValueOfWork, pk=pk)
    form = F.VOWEntryForm(request.POST or None, instance=v)

    def save(f):
        f.save()
        messages.success(request, "Value-of-work entry saved")
        return f"/projects/{v.project_id}/?tab=history"
    return edit_page(request, form, f"Edit VOW entry · {v.project.io_number}", f"/projects/{v.project_id}/?tab=history",
                     save)


def vow_delete(request, pk):
    v = get_object_or_404(ValueOfWork, pk=pk)
    return delete_page(request, "Delete value-of-work entry?", f"the {v.reporting_date} VOW entry", [],
                       lambda post: v.delete(), f"/projects/{v.project_id}/?tab=history",
                       f"/projects/{v.project_id}/?tab=history")


