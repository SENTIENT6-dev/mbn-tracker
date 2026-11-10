"""Fiscal-year rollover of the MPSR cash-flow forecast.

Forecasts are kept per fiscal year (April to March). When a new fiscal year starts, each project's forecast
moves forward one year:

    Previous years  <- previous years + last year's Q1-Q4
    Q1-Q4           <- last year's "next year", split evenly (cents left over go to Q4)
    Next year       <- last year's "future"
    Future          <- 0

The total stays the same. A forecast more than one year behind is moved forward one year at a time.
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Project, ProjectStatus, StatusHistory
from .textutil import current_fiscal_year, fiscal_label

ZERO = Decimal("0")
CENT = Decimal("0.01")
FIELDS = ["previous", "q1", "q2", "q3", "q4", "next_year", "future"]
REPORTING = [Project.Status.ACTIVE, Project.Status.ON_HOLD]


def roll_one_year(values):
    """values: dict of FIELDS -> Decimal. Returns the same shape, one fiscal year later."""
    v = {k: values[k] or ZERO for k in FIELDS}
    quarter = (v["next_year"] / 4).quantize(CENT)
    return {
        "previous": v["previous"] + v["q1"] + v["q2"] + v["q3"] + v["q4"],
        "q1": quarter, "q2": quarter, "q3": quarter,
        "q4": v["next_year"] - quarter * 3,
        "next_year": v["future"],
        "future": ZERO,
    }


@dataclass
class Plan:
    status: ProjectStatus
    from_year: int
    to_year: int
    before: dict
    after: dict

    @property
    def project(self):
        return self.status.project

    @property
    def years(self):
        return self.to_year - self.from_year

    @property
    def from_label(self):
        return fiscal_label(self.from_year)

    @property
    def to_label(self):
        return fiscal_label(self.to_year)

    @property
    def before_year(self):
        return sum((self.before[q] for q in ("q1", "q2", "q3", "q4")), ZERO)

    @property
    def after_year(self):
        return sum((self.after[q] for q in ("q1", "q2", "q3", "q4")), ZERO)


def stale_statuses(to_year=None):
    """Forecasts of active / on-hold projects that are set to an earlier fiscal year than `to_year`."""
    to_year = to_year or current_fiscal_year(timezone.localdate())
    return (ProjectStatus.objects.select_related("project")
            .filter(project__status__in=REPORTING, fiscal_year__lt=to_year)
            .order_by("project__io_number"))


def plan(statuses, to_year=None):
    to_year = to_year or current_fiscal_year(timezone.localdate())
    out = []
    for st in statuses:
        if st.fiscal_year is None or st.fiscal_year >= to_year:
            continue
        before = {k: getattr(st, k) for k in FIELDS}
        after = before
        for _ in range(to_year - st.fiscal_year):
            after = roll_one_year(after)
        out.append(Plan(st, st.fiscal_year, to_year, before, after))
    return out


def backup_database(label="rollover"):
    """Copy the database into the backups folder before the rollover (see tracker/backup.py)."""
    from .backup import backup_database as backup
    return backup(label)


def apply(plans):
    """Write the rolled-forward forecasts and log each one in the project's history. Returns how many moved.

    Each forecast is worked out again from the saved record, locked, so an edit made after the preview is
    rolled forward rather than overwritten.
    """
    now = timezone.now()
    done = 0
    with transaction.atomic():
        for preview in plans:
            st = ProjectStatus.objects.select_for_update().select_related("project").get(pk=preview.status.pk)
            fresh = plan([st], preview.to_year)
            if not fresh:                          # already moved forward since the preview
                continue
            p = fresh[0]
            done += 1
            for k, v in p.after.items():
                setattr(st, k, v)
            st.fiscal_year = p.to_year
            st.save()
            StatusHistory.objects.create(
                project=st.project, kind=StatusHistory.Kind.CASH_FLOW, changed_at=now,
                text=f"Fiscal-year rollover: forecast moved from FY {p.from_label} to FY {p.to_label}",
                data={"fiscal_year": p.to_year, **{k: float(v) for k, v in p.after.items()},
                      "rolled_from": {"fiscal_year": p.from_year, **{k: float(v) for k, v in p.before.items()}}})
    return done
