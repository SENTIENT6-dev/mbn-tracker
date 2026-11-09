"""Data model for IO PM Tracker (replacement for MBN.accdb).

Compared with the Access file, every link between tables is enforced and the
keys that must be unique (project numbers, PCCF submission numbers, PO numbers,
VOW reporting dates) are enforced by the database.
"""
from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum

from .textutil import fiscal_label

ZERO = Decimal("0")
MONEY = dict(max_digits=14, decimal_places=2, default=ZERO)


def money(value):
    return value if value is not None else ZERO


# ---------------------------------------------------------------- lookups

class CompanyType(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Company(models.Model):
    name = models.CharField(max_length=255, unique=True)
    company_type = models.ForeignKey(CompanyType, on_delete=models.PROTECT, null=True, blank=True)
    vendor_number = models.CharField(max_length=50, blank=True)
    acronym = models.CharField(max_length=50, blank=True)
    hst_number = models.CharField(max_length=50, blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    province = models.CharField(max_length=50, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    website = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "companies"

    def __str__(self):
        return self.name


class Person(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="people")
    position = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        verbose_name_plural = "people"

    def __str__(self):
        return f"{self.first_name} {self.last_name}"


class Property(models.Model):
    """A site / facility that groups one or more buildings (Access: Property_Number)."""
    number = models.CharField("property #", max_length=50, unique=True)
    name = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["number"]
        verbose_name_plural = "properties"

    def __str__(self):
        return f"{self.number} {self.name}".strip()


class HeritageStatus(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "heritage statuses"

    def __str__(self):
        return self.name


class Building(models.Model):
    """A building / asset (Access: Facilities). Projects point at one."""
    asset_number = models.CharField("asset #", max_length=50, unique=True)
    name = models.CharField(max_length=255, blank=True)
    property = models.ForeignKey(Property, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="buildings")
    parent_asset = models.CharField(max_length=50, blank=True)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    purpose = models.CharField(max_length=255, blank=True)
    floors = models.IntegerField(null=True, blank=True)
    construction_year = models.IntegerField(null=True, blank=True)
    heritage_status = models.ForeignKey(HeritageStatus, on_delete=models.SET_NULL, null=True, blank=True)
    landlord = models.CharField(max_length=255, blank=True)
    bgis_building = models.CharField("BGIS building #", max_length=50, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    area = models.FloatField(null=True, blank=True, help_text="As recorded in Access (unit not specified there)")

    class Meta:
        ordering = ["asset_number"]

    def __str__(self):
        return f"{self.asset_number} {self.name}".strip()


class RoleType(models.Model):
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class BuildingRole(models.Model):
    """Who does what for a building (facility manager, PREM, regional FM...)."""
    building = models.ForeignKey(Building, on_delete=models.CASCADE, related_name="roles")
    person = models.ForeignKey("Person", on_delete=models.CASCADE, related_name="building_roles")
    role = models.ForeignKey(RoleType, on_delete=models.PROTECT)

    class Meta:
        ordering = ["role__name"]
        constraints = [models.UniqueConstraint(fields=["building", "person", "role"], name="one_role_per_person")]


class StageGate(models.Model):
    name = models.CharField(max_length=100, unique=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------- projects

class Project(models.Model):
    class Status(models.IntegerChoices):
        ACTIVE = 1, "Active"
        ON_HOLD = 2, "On Hold"
        CANCELLED = 3, "Cancelled"
        COMPLETED = 4, "Completed"
        UNKNOWN = 5, "Unknown"
        CLOSED = 6, "Closed"

    class Kind(models.IntegerChoices):
        MINISTRY = 1, "Ministry"
        CAPITAL = 2, "Capital"

    class Phase(models.IntegerChoices):
        PLANNING_DESIGN = 1, "Planning/Design"
        RFP_AWARD = 2, "RFP/Award"
        CONSTRUCTION = 3, "Construction"
        FINANCIAL_CLOSE = 4, "Financial Close"
        STUDY_ONLY = 5, "Study or Design Only"
        TENDER_AWARD = 6, "Tender/Award"
        CHARTER = 7, "Charter"

    io_number = models.CharField("IO project #", max_length=50, unique=True)
    bgis_number = models.CharField("BGIS project #", max_length=50, blank=True)
    title = models.CharField(max_length=255)
    building = models.ForeignKey(Building, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="projects")
    building_code = models.CharField(max_length=50, blank=True,
                                     help_text="Building number when it is not in the buildings list")
    project_manager = models.ForeignKey(Person, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="managed_projects")
    client = models.ForeignKey(Company, on_delete=models.PROTECT, null=True, blank=True,
                               related_name="client_projects")
    kind = models.PositiveSmallIntegerField(choices=Kind.choices, default=Kind.MINISTRY)
    phase = models.PositiveSmallIntegerField(choices=Phase.choices, null=True, blank=True)
    status = models.PositiveSmallIntegerField(choices=Status.choices, default=Status.ACTIVE)
    needs_review = models.BooleanField(default=False,
                                       help_text="Created during migration to hold records whose project was missing")

    class Meta:
        ordering = ["io_number"]

    def __str__(self):
        return f"{self.io_number} – {self.title}"

    CLIENT_NAME = "Infrastructure Ontario"
    PM_COMPANY = "BGIS"   # project managers are always BGIS staff

    @classmethod
    def pm_company(cls):
        return Company.objects.get_or_create(name=cls.PM_COMPANY)[0]

    def save(self, *args, **kwargs):
        # The client is always Infrastructure Ontario
        if self.client_id is None:
            self.client, _ = Company.objects.get_or_create(name=self.CLIENT_NAME, defaults={"acronym": "IO"})
        # The project manager is always the person this tracker belongs to
        if self.project_manager_id is None:
            owner = Owner.current()
            if owner:
                self.project_manager = owner.person
        super().save(*args, **kwargs)

    # ---- figures used by financial screens (one place, instead of 10 chained queries)
    def latest_pccf(self):
        return self.pccfs.order_by("-submission").first()

    def commitments(self):
        return money(self.purchase_orders.aggregate(t=Sum("amount"))["t"])

    def invoiced(self):
        return money(Invoice.objects.filter(po__project=self).aggregate(t=Sum("amount"))["t"])

    def pending_changes(self):
        return money(ChangeOrder.objects.filter(po__project=self, status=ChangeOrder.State.APPROVED)
                     .aggregate(t=Sum("value"))["t"])

    def latest_vow(self):
        return self.vow_entries.order_by("-reporting_date").first()


class PCCF(models.Model):
    """One PCF/PCCF submission (budget + schedule + approved cash flow)."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="pccfs")
    submission = models.PositiveSmallIntegerField()
    date = models.DateField(null=True, blank=True)
    final = models.BooleanField(default=False)

    notes = models.TextField(blank=True, help_text="Scope, methodology, milestone and budget changes")

    approval_date = models.DateField(null=True, blank=True)
    design_start = models.DateField(null=True, blank=True)
    design_end = models.DateField(null=True, blank=True)
    construction_award = models.DateField(null=True, blank=True)
    substantial = models.DateField(null=True, blank=True)
    closeout = models.DateField(null=True, blank=True)

    design_cost = models.DecimalField(**MONEY)
    construction_cost = models.DecimalField(**MONEY)
    design_contingency = models.DecimalField(**MONEY)
    construction_contingency = models.DecimalField(**MONEY)
    fee_percent = models.DecimalField(max_digits=7, decimal_places=4, default=ZERO)
    fee = models.DecimalField(**MONEY)

    fiscal_year = models.PositiveSmallIntegerField(null=True, blank=True,
                                                   help_text="Stored as the year the fiscal year ends (2025 = 2024/25)")
    previous = models.DecimalField(**MONEY)
    q1 = models.DecimalField(**MONEY)
    q2 = models.DecimalField(**MONEY)
    q3 = models.DecimalField(**MONEY)
    q4 = models.DecimalField(**MONEY)
    next_year = models.DecimalField(**MONEY)
    future = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["project", "submission"]
        constraints = [models.UniqueConstraint(fields=["project", "submission"], name="one_pccf_per_submission")]
        verbose_name = "PCCF submission"

    def __str__(self):
        return f"{self.project.io_number} PCCF {self.submission}"

    @property
    def budget_without_fee(self):
        return self.design_cost + self.construction_cost + self.design_contingency + self.construction_contingency

    @property
    def contingencies(self):
        return self.design_contingency + self.construction_contingency

    @property
    def total_budget(self):
        return self.budget_without_fee + self.fee

    @property
    def fiscal_year_label(self):
        return fiscal_label(self.fiscal_year)

    @property
    def current_year_cash_flow(self):
        return self.q1 + self.q2 + self.q3 + self.q4


# ---------------------------------------------------------------- procurement

def po_label(number):
    """PO numbers as shown in the tracker: Access's 999-prefixed POs are written with ION instead (99900123 -> ION00123)."""
    s = str(number).strip().upper()
    return "ION" + s[3:] if s.startswith("999") and len(s) > 3 else s


class PurchaseOrder(models.Model):
    number = models.CharField("PO #", max_length=30, unique=True)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="purchase_orders")
    vendor = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="purchase_orders")
    approved_date = models.DateField(null=True, blank=True)
    amount = models.DecimalField("current PO value", **MONEY)

    class Meta:
        ordering = ["-number"]
        verbose_name = "purchase order"

    def __str__(self):
        return f"PO {self.number}"

    def invoiced(self):
        return money(self.invoices.aggregate(t=Sum("amount"))["t"])

    def pending_changes(self):
        return money(self.change_orders.filter(status=ChangeOrder.State.APPROVED).aggregate(t=Sum("value"))["t"])

    def remaining(self):
        return self.amount + self.pending_changes() - self.invoiced()


class Invoice(models.Model):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="invoices")
    number = models.CharField("invoice #", max_length=100)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-date", "number"]
        constraints = [models.UniqueConstraint(fields=["po", "number", "amount", "date"],
                                               name="no_identical_invoice")]

    def __str__(self):
        return f"Invoice {self.number} (PO {self.po.number})"


class ChangeOrder(models.Model):
    """A change to a PO. The project always comes from the PO, so they can't disagree."""

    class State(models.TextChoices):
        REQUESTED = "requested", "Requested (not approved)"
        APPROVED = "approved", "Approved, PO not yet revised"
        PROCESSED = "processed", "Processed into PO"
        REJECTED = "rejected", "Rejected"

    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="change_orders")
    description = models.CharField(max_length=255)
    value = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(max_length=20, choices=State.choices, default=State.APPROVED)
    folder_link = models.CharField(max_length=500, blank=True)
    comment = models.CharField(max_length=500, blank=True)
    created = models.DateTimeField(auto_now_add=True)
    processed_on = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created"]

    def __str__(self):
        return f"CO on PO {self.po.number}: {self.description}"

    @property
    def project(self):
        return self.po.project


# ---------------------------------------------------------------- MPSR status

class ProjectStatus(models.Model):
    """Current forecast for a project (was Current_Project_Status)."""
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name="current_status")
    fiscal_year = models.PositiveSmallIntegerField(null=True, blank=True)
    previous = models.DecimalField(**MONEY)
    q1 = models.DecimalField(**MONEY)
    q2 = models.DecimalField(**MONEY)
    q3 = models.DecimalField(**MONEY)
    q4 = models.DecimalField(**MONEY)
    next_year = models.DecimalField(**MONEY)
    future = models.DecimalField(**MONEY)

    design_end = models.DateField(null=True, blank=True)
    construction_award = models.DateField(null=True, blank=True)
    substantial = models.DateField(null=True, blank=True)
    closeout = models.DateField(null=True, blank=True)
    actual_design_end = models.DateField(null=True, blank=True)
    actual_construction_award = models.DateField(null=True, blank=True)
    actual_substantial = models.DateField(null=True, blank=True)
    actual_closeout = models.DateField(null=True, blank=True)
    # Going out to tender is due 30 days after the actual design end (see services.TENDER_DAYS)
    actual_tender = models.DateField("tender issued", null=True, blank=True)

    stage =models.ForeignKey(StageGate, on_delete=models.PROTECT, null=True, blank=True)
    comments = models.TextField(blank=True)
    risk = models.TextField(blank=True)
    final_mpsr = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "project statuses"

    def __str__(self):
        return f"Status of {self.project.io_number}"

    @property
    def current_year_forecast(self):
        return self.q1 + self.q2 + self.q3 + self.q4

    @property
    def fiscal_year_label(self):
        return fiscal_label(self.fiscal_year)


class StatusHistory(models.Model):
    """Audit trail of every MPSR update (replaces the four *_History tables)."""

    class Kind(models.TextChoices):
        CASH_FLOW = "cash_flow", "Cash flow"
        DATES = "dates", "Forecast dates"
        ACTUALS = "actuals", "Actual dates / stage"
        COMMENT = "comment", "Comment"
        RISK = "risk", "Risk"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="status_history")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    changed_at = models.DateTimeField()
    text = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-changed_at"]
        verbose_name_plural = "status history"


class MPSRReview(models.Model):
    """A project's monthly MPSR has been gone through for this period (month)."""
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="mpsr_reviews")
    period = models.DateField(help_text="First day of the reporting month")
    reviewed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-period"]
        constraints = [models.UniqueConstraint(fields=["project", "period"], name="one_review_per_month")]


class ValueOfWork(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="vow_entries")
    reporting_date = models.DateField()
    total_no_fees = models.DecimalField("total VOW (no fees)", **MONEY)
    previous_years_no_fees = models.DecimalField("previous years VOW (no fees)", **MONEY)
    current_year_funding = models.DecimalField(**MONEY)

    class Meta:
        ordering = ["-reporting_date"]
        constraints = [models.UniqueConstraint(fields=["project", "reporting_date"], name="one_vow_per_day")]
        verbose_name = "value of work entry"
        verbose_name_plural = "value of work"

    @property
    def current_year_no_fees(self):
        return self.total_no_fees - self.previous_years_no_fees


# ---------------------------------------------------------------- migration review

class Owner(models.Model):
    """The project manager this copy of the tracker belongs to (one per installation).

    Every project in this tracker is theirs; it's set up on first run and changed under Settings.
    """
    person = models.OneToOneField(Person, on_delete=models.PROTECT, related_name="owner_of")
    access_source = models.CharField("Access file", max_length=500, blank=True,
                                     help_text="Where this PM's MBN.accdb was last imported from")
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.person)

    @classmethod
    def current(cls):
        return cls.objects.select_related("person__company").first()


class DataIssue(models.Model):
    """Something the Access migration could not import cleanly and needs a decision."""

    class Severity(models.TextChoices):
        HIGH = "high", "Affects totals"
        MEDIUM = "medium", "Needs review"
        LOW = "low", "Information"

    category = models.CharField(max_length=100)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    summary = models.CharField(max_length=500)
    detail = models.JSONField(default=dict, blank=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True)
    resolved = models.BooleanField(default=False)
    resolution_note = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["resolved", "severity", "category"]

    def __str__(self):
        return self.summary
