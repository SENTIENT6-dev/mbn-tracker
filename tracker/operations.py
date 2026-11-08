"""Edits and deletions that touch more than one table.

Every function runs in a transaction so related records never end up half-updated.
`*_impact` functions describe what a deletion will remove or change, for the
confirmation page.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import (PCCF, BuildingRole, ChangeOrder, DataIssue, Invoice, Person, Project, PurchaseOrder,
                     StatusHistory, ValueOfWork)

ZERO = Decimal("0")


def _money(qs, field):
    return qs.aggregate(t=Sum(field))["t"] or ZERO


# ---------------------------------------------------------------- projects

def project_impact(project):
    pos = PurchaseOrder.objects.filter(project=project)
    inv = Invoice.objects.filter(po__project=project)
    return [
        ("PCCF submissions", project.pccfs.count(), None),
        ("Purchase orders", pos.count(), _money(pos, "amount")),
        ("Invoices", inv.count(), _money(inv, "amount")),
        ("Change orders", ChangeOrder.objects.filter(po__project=project).count(), None),
        ("Value-of-work entries", project.vow_entries.count(), None),
        ("Status updates in history", project.status_history.count(), None),
        ("Current MPSR status", 1 if hasattr(project, "current_status") else 0, None),
    ]


@transaction.atomic
def delete_project(project):
    """Remove a project and everything that belongs to it."""
    pos = PurchaseOrder.objects.filter(project=project)
    Invoice.objects.filter(po__in=pos).delete()
    ChangeOrder.objects.filter(po__in=pos).delete()
    pos.delete()
    DataIssue.objects.filter(project=project).update(project=None)
    project.delete()  # cascades PCCFs, status, history, VOW


# ---------------------------------------------------------------- purchase orders

def po_impact(po):
    return [("Invoices", po.invoices.count(), _money(po.invoices.all(), "amount")),
            ("Change orders", po.change_orders.count(), None)]


@transaction.atomic
def delete_po(po):
    po.invoices.all().delete()
    po.change_orders.all().delete()
    po.delete()


# ---------------------------------------------------------------- change orders

@transaction.atomic
def save_change_order(co, old_value=None, old_status=None):
    """Save a change order; if it was already processed into its PO, keep the PO value in step."""
    po = PurchaseOrder.objects.select_for_update().get(pk=co.po_id)
    was = old_value if old_status == ChangeOrder.State.PROCESSED else ZERO
    now = co.value if co.status == ChangeOrder.State.PROCESSED else ZERO
    if was != now:
        po.amount += now - was
        po.save()
        if co.status == ChangeOrder.State.PROCESSED and not co.processed_on:
            co.processed_on = timezone.now()
    co.save()
    return po


@transaction.atomic
def delete_change_order(co, reverse=True):
    """Deleting a processed change order takes its value back out of the PO (unless told not to)."""
    if co.status == ChangeOrder.State.PROCESSED and reverse:
        po = PurchaseOrder.objects.select_for_update().get(pk=co.po_id)
        po.amount -= co.value
        po.save()
    co.delete()


# ---------------------------------------------------------------- companies / vendors

def company_impact(company):
    return [
        ("Purchase orders (as vendor)", company.purchase_orders.count(), _money(company.purchase_orders.all(), "amount")),
        ("Projects (as client)", company.client_projects.count(), None),
        ("Contacts", company.people.count(), None),
    ]


def company_in_use(company):
    return company.purchase_orders.exists() or company.client_projects.exists()


@transaction.atomic
def delete_company(company):
    """Only allowed when no PO or project uses it; its contacts go with it."""
    if company_in_use(company):
        raise ValueError("This company still has POs or client projects. Merge it into another company instead.")
    for person in company.people.all():
        delete_person(person)
    company.delete()


@transaction.atomic
def merge_company(source, target):
    """Move everything from `source` onto `target`, then delete `source` (for duplicates / renamed vendors)."""
    if source.pk == target.pk:
        raise ValueError("Pick a different company to merge into.")
    source.purchase_orders.update(vendor=target)
    source.client_projects.update(client=target)
    source.people.update(company=target)
    for f in ("vendor_number", "acronym", "hst_number", "address", "city", "province", "postal_code", "phone",
              "website"):
        if not getattr(target, f) and getattr(source, f):
            setattr(target, f, getattr(source, f))
    if source.notes:
        target.notes = (target.notes + "\n" if target.notes else "") + f"[merged from {source.name}] {source.notes}"
    target.save()
    source.delete()


# ---------------------------------------------------------------- people

def _name_key(p):
    return (" ".join(p.first_name.split()).lower(), " ".join(p.last_name.split()).lower())


def _richness(p):
    """How much useful information a person record holds (the richest one is kept when merging)."""
    return (p.managed_projects.count() + p.building_roles.count(), "@" in (p.email or ""), bool(p.phone),
            bool(p.position), -p.pk)


@transaction.atomic
def merge_people(keep, others):
    """Fold duplicate person records into `keep`: projects, building roles and any missing details move over."""
    for o in others:
        o.managed_projects.update(project_manager=keep)
        for role in o.building_roles.all():
            if BuildingRole.objects.filter(building=role.building, person=keep, role=role.role).exists():
                role.delete()
            else:
                role.person = keep
                role.save()
        if "@" not in (keep.email or "") and "@" in (o.email or ""):
            keep.email = o.email
        for f in ("phone", "position"):
            if not getattr(keep, f) and getattr(o, f):
                setattr(keep, f, getattr(o, f))
        o.delete()
    if keep.email and "@" not in keep.email:
        keep.email = ""   # junk like "►" left over from Access
    keep.save()
    return keep


def merge_duplicate_people(company):
    """Merge people at `company` who share the same first and last name. Returns [(kept, n_merged)]."""
    groups = {}
    for p in Person.objects.filter(company=company):
        groups.setdefault(_name_key(p), []).append(p)
    done = []
    for people in groups.values():
        if len(people) > 1:
            people.sort(key=_richness, reverse=True)
            done.append((merge_people(people[0], people[1:]), len(people) - 1))
    return done


@transaction.atomic
def set_owner(first_name, last_name, email="", phone="", position="Project Manager", keep_existing=False):
    """Make this tracker belong to the given BGIS project manager. Every project becomes theirs.

    keep_existing: when matching someone already on file (first-run setup), boxes left blank keep what's
    already recorded instead of erasing it.
    """
    from .models import Owner
    bgis = Project.pm_company()
    current = Owner.current()
    person = same_name_person(bgis, first_name, last_name)
    if person is None and current and _name_key(current.person) == (" ".join(first_name.split()).lower(),
                                                                     " ".join(last_name.split()).lower()):
        person = current.person
    if person is None:
        person = Person(company=bgis)
    person.first_name, person.last_name = " ".join(first_name.split()), " ".join(last_name.split())
    if keep_existing and person.pk:
        person.email = email or person.email
        person.phone = phone or person.phone
        person.position = position or person.position or "Project Manager"
    else:
        person.email, person.phone, person.position = email, phone, position or "Project Manager"
    person.company = bgis
    person.save()
    if current:
        if current.person_id != person.pk:
            current.person = person
            current.save()
        owner = current
    else:
        owner = Owner.objects.create(person=person)
    Project.objects.exclude(project_manager=person).update(project_manager=person)
    return owner


def same_name_person(company, first, last, exclude=None):
    key = (" ".join(first.split()).lower(), " ".join(last.split()).lower())
    for p in Person.objects.filter(company=company).exclude(pk=getattr(exclude, "pk", None)):
        if _name_key(p) == key:
            return p
    return None

def person_impact(person):
    return [("Projects managed (PM will be cleared)", person.managed_projects.count(), None),
            ("Building roles", person.building_roles.count(), None)]


@transaction.atomic
def delete_person(person):
    person.delete()  # PM fields set to empty, building roles removed


# ---------------------------------------------------------------- buildings & properties

def building_impact(building):
    return [("Projects (keep the asset # as text)", building.projects.count(), None),
            ("Contacts / roles", building.roles.count(), None)]


@transaction.atomic
def delete_building(building):
    """Projects keep the asset number as text so the history isn't lost."""
    building.projects.update(building=None, building_code=building.asset_number)
    building.delete()


@transaction.atomic
def relink_project_codes(building):
    """After adding or renumbering a building, attach projects that only had its number as text."""
    return Project.objects.filter(building__isnull=True, building_code__iexact=building.asset_number) \
        .update(building=building, building_code="")


def property_impact(prop):
    return [("Buildings (will be left without a property)", prop.buildings.count(), None)]


@transaction.atomic
def delete_property(prop):
    prop.delete()  # buildings keep existing, property cleared
