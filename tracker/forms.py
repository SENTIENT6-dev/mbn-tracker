from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.db.models import Case, When
from django.utils.html import format_html

from .services import invoice_room, room_message, schedule_problems
from .textutil import current_fiscal_year, fiscal_label

from .models import (PCCF, Building, BuildingRole, ChangeOrder, Company, Invoice, Person, Project, ProjectStatus,
                     Property, PurchaseOrder, ValueOfWork)

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class DateFieldsMixin:
    """Render every DateField as a native date picker."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in self.fields.values():
            if isinstance(f, forms.DateField):
                f.widget = DATE


class MoneyInput(forms.TextInput):
    """Shows amounts as 1,234,567.89; the page script keeps them formatted as you type."""

    def __init__(self, attrs=None):
        super().__init__({"inputmode": "decimal", "class": "money", "autocomplete": "off", **(attrs or {})})

    def format_value(self, value):
        if value in (None, ""):
            return ""
        try:
            return f"{Decimal(str(value)):,.2f}"
        except Exception:
            return str(value)   # whatever was typed, shown back with its error


class MoneyField(forms.DecimalField):
    """A dollar amount: accepts '$1,234.56', '1234.56', '(1,000)' for negatives."""
    widget = MoneyInput

    def to_python(self, value):
        if isinstance(value, str):
            v = value.strip().replace("$", "").replace(",", "").replace(" ", "")
            if v.startswith("(") and v.endswith(")"):
                v = "-" + v[1:-1]
            value = v
        return super().to_python(value)

    def widget_attrs(self, widget):
        return {}   # no number-input step/min attributes on a text box


def money_fields_everywhere(form_classes):
    """Swap every 2-decimal amount on the given forms for a MoneyField (same label, help, required)."""
    for cls in form_classes:
        for name, f in list(getattr(cls, "base_fields", {}).items()):
            if isinstance(f, forms.DecimalField) and not isinstance(f, MoneyField) and f.decimal_places == 2:
                cls.base_fields[name] = MoneyField(
                    max_digits=f.max_digits, decimal_places=2, required=f.required, label=f.label,
                    help_text=f.help_text, initial=f.initial)


MONEY_FIELDS = ("design_cost", "design_contingency", "construction_cost", "construction_contingency",
                "fee_percent", "fee", "previous", "q1", "q2", "q3", "q4", "next_year", "future")


class BlankMoneyIsZeroMixin:
    """Money fields on a PCCF may be left empty; empty means $0 (so they aren't shown as required)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in MONEY_FIELDS:
            if f in self.fields:
                self.fields[f].required = False

    def clean(self):
        data = super().clean()
        for f in MONEY_FIELDS:
            if f in self.fields and data.get(f) is None and f not in self.errors:
                data[f] = Decimal(0)
        return data


BUDGET_PARTS = ("design_cost", "design_contingency", "construction_cost", "construction_contingency", "fee")
CASH_PARTS = ("previous", "q1", "q2", "q3", "q4", "next_year", "future")
TOLERANCE = Decimal("1.00")   # rounding allowance


class CashFlowMatchesBudgetMixin:
    """A PCCF's cash flow (previous years + Q1-Q4 + next + future) must add up to its total budget incl. fee."""

    def clean(self):
        data = super().clean()
        if any(f in self.errors for f in BUDGET_PARTS + CASH_PARTS):
            return data
        budget = sum((data.get(f) or Decimal(0)) for f in BUDGET_PARTS)
        cash = sum((data.get(f) or Decimal(0)) for f in CASH_PARTS)
        if abs(budget - cash) > TOLERANCE:
            diff = cash - budget
            raise forms.ValidationError(
                f"The cash flow adds up to ${cash:,.2f} but the total budget (incl. fee) is ${budget:,.2f}. "
                f"The cash flow is {'over' if diff > 0 else 'short'} by ${abs(diff):,.2f}; they must match.")
        return data


class ProjectForm(DateFieldsMixin, forms.ModelForm):
    building_asset = forms.CharField(
        label="Building (asset #)", required=True,
        widget=forms.TextInput(attrs={"placeholder": "Type an asset #, name or address", "data-building-search": "1",
                                      "autocomplete": "off"}),
        help_text="Must be in the buildings list")

    class Meta:
        model = Project
        # client is always IO; the project manager is always the person this tracker belongs to
        fields = ["io_number", "bgis_number", "title", "status", "kind", "phase"]
        widgets = {"io_number": forms.TextInput(attrs={"placeholder": "e.g. 1100123"}),
                   "bgis_number": forms.TextInput(attrs={"placeholder": "e.g. IO_1841870 (optional)"}),
                   "title": forms.TextInput(attrs={"placeholder": "Short description of the work, e.g. Roof replacement"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        p = self.instance
        if p.pk:
            self.fields["building_asset"].initial = p.building.asset_number if p.building else p.building_code

    def clean_building_asset(self):
        code = self.cleaned_data["building_asset"].strip().split(" ")[0]   # "B12345 · name" -> "B12345"
        b = Building.objects.filter(asset_number__iexact=code).first()
        if b:
            return b
        if self.instance.pk and not self.instance.building and code.lower() == self.instance.building_code.lower():
            return code   # older project whose building was never in the list: left as it was
        raise forms.ValidationError(format_html(
            'Building {} isn\'t in the buildings list. <a href="/facilities/buildings/new/?asset={}" '
            'target="_blank" rel="noopener">Add this building</a> (opens in a new tab), then save again.',
            code, code))

    def save(self, commit=True):
        p = super().save(commit=False)
        b = self.cleaned_data.get("building_asset")
        if isinstance(b, Building):
            p.building, p.building_code = b, ""
        else:
            p.building, p.building_code = None, b or ""
        if commit:
            p.save()
        return p


def fiscal_year_choices():
    """'2024/25'-style choices, stored as the year the fiscal year ends."""
    this = date.today().year
    return [("", "—")] + [(y, fiscal_label(y)) for y in range(this + 6, 2009, -1)]


class FiscalYearMixin:
    """Show fiscal_year as a 2024/25-style drop-down instead of a bare number."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "fiscal_year" in self.fields:
            f = self.fields["fiscal_year"]
            self.fields["fiscal_year"] = forms.TypedChoiceField(
                choices=fiscal_year_choices(), coerce=int, empty_value=None, required=False,
                label="Fiscal year", help_text="April to March")
            self.fields["fiscal_year"].initial = f.initial
            # Nothing saved yet -> start on the fiscal year we're in now
            saved = getattr(self.instance, "fiscal_year", None) if getattr(self.instance, "pk", None) else None
            if not saved and not self.initial.get("fiscal_year"):
                self.initial["fiscal_year"] = current_fiscal_year()


PCCF_LABELS = {
    "date": "PCCF date", "final": "Final PCCF",
    "design_cost": "Design", "design_contingency": "Design contingency",
    "construction_cost": "Construction", "construction_contingency": "Construction contingency",
    "fee_percent": "Fee %", "fee": "Fee amount",
    "approval_date": "Charter approval", "design_start": "Design start", "design_end": "Design end",
    "construction_award": "Construction award", "substantial": "Substantial performance", "closeout": "Closeout",
    "previous": "Previous years", "q1": "Q1 · Apr–Jun", "q2": "Q2 · Jul–Sep", "q3": "Q3 · Oct–Dec",
    "q4": "Q4 · Jan–Mar", "next_year": "Next fiscal year", "future": "Future years", "notes": "Notes",
}


class FeeFromPercentMixin:
    """The fee is never typed: it's (design + construction + both contingencies) x fee %, to the cent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "fee" in self.fields:
            f = self.fields["fee"]
            f.required = False
            f.help_text = "Calculated: budget × fee %"
            f.widget.attrs.update({"readonly": "readonly", "tabindex": "-1", "class": "money computed",
                                   "data-computed-fee": "1"})

    def clean(self):
        data = super().clean()
        if "fee" in self.fields and not any(f in self.errors for f in BUDGET_PARTS[:4] + ("fee_percent",)):
            subtotal = sum((data.get(f) or Decimal(0)) for f in BUDGET_PARTS[:4])
            pct = data.get("fee_percent") or Decimal(0)
            data["fee"] = (subtotal * pct / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
            self.errors.pop("fee", None)
        return data


class ScheduleOrderMixin:
    """Approved milestones must not go backwards: approval <= design start <= design end <= award <= SP <= closeout."""

    def clean(self):
        data = super().clean()
        probs = schedule_problems(lambda f: data.get(f))
        for pf, pl, pd, lf, ll, ld in probs:
            if lf in self.fields and lf not in self.errors:
                self.add_error(lf, f"Can't be before {pl} ({pd:%Y-%m-%d}).")
        if probs:
            raise forms.ValidationError(
                "The approved schedule is out of order: " +
                "; ".join(f"{ll} ({ld:%Y-%m-%d}) is before {pl} ({pd:%Y-%m-%d})" for pf, pl, pd, lf, ll, ld in probs)
                + ". Each milestone must be on or after the one before it.")
        return data


class PCCFLayoutMixin:
    """Shared labels for the PCCF and charter forms; the layout itself is in _pccf_sections.html."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, label in PCCF_LABELS.items():
            if name in self.fields and name not in getattr(self.Meta, "labels", {}):
                self.fields[name].label = label


class PCCFForm(PCCFLayoutMixin, ScheduleOrderMixin, CashFlowMatchesBudgetMixin, FeeFromPercentMixin,
               BlankMoneyIsZeroMixin, FiscalYearMixin, DateFieldsMixin,
               forms.ModelForm):
    """The submission number isn't on the form: new PCCFs get the next number, existing ones keep theirs."""

    class Meta:
        model = PCCF
        exclude = ["project", "submission"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 5})}
        help_texts = {"notes": "Scope, methodology, milestone and budget changes"}

    def __init__(self, *args, project=None, **kwargs):
        self.project = project
        super().__init__(*args, **kwargs)


class CharterForm(PCCFLayoutMixin, ScheduleOrderMixin, CashFlowMatchesBudgetMixin, FeeFromPercentMixin,
                  BlankMoneyIsZeroMixin, FiscalYearMixin,
                  DateFieldsMixin, forms.ModelForm):
    """The original PCCF (submission 0) entered together with a new project."""

    class Meta:
        model = PCCF
        exclude = ["project", "submission", "final"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 4})}
        labels = {"notes": "Charter notes", "date": "Charter date"}
        help_texts = {"notes": "Scope and anything else worth recording about the charter"}

    def clean(self):
        data = super().clean()
        budget = sum((data.get(f) or Decimal(0)) for f in
                     ("design_cost", "design_contingency", "construction_cost", "construction_contingency"))
        if budget <= 0:
            raise forms.ValidationError("The charter needs an approved budget (design and/or construction).")
        return data


class PurchaseOrderForm(DateFieldsMixin, forms.ModelForm):
    class Meta:
        model = PurchaseOrder
        fields = ["number", "approved_date", "amount", "project", "vendor"]
        labels = {"number": "PO #", "approved_date": "Approved date", "amount": "PO value"}
        widgets = {"number": forms.TextInput(attrs={"placeholder": "e.g. 99900123", "inputmode": "numeric"})}
        help_texts = {"amount": "Current value, including change orders already processed"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # active projects first, then the rest, newest numbers first
        self.fields["project"].queryset = Project.objects.order_by(
            Case(When(status__in=[Project.Status.ACTIVE, Project.Status.ON_HOLD], then=0),
                 When(io_number__startswith="MISSING-", then=2), default=1), "-io_number")
        self.fields["project"].label_from_instance = \
            lambda p: f"{p.io_number} · {p.title}{'' if p.status in (1, 2) else ' (' + p.get_status_display() + ')'}"
        if self.instance.pk:
            self.fields["project"].help_text = "Moving a PO moves its invoices and change orders with it."


class InvoiceEditForm(DateFieldsMixin, forms.ModelForm):
    class Meta:
        model = Invoice
        fields = ["po", "number", "amount", "date"]
        labels = {"po": "PO"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["po"].queryset = PurchaseOrder.objects.select_related("vendor", "project").order_by("-number")
        self.fields["po"].label_from_instance = \
            lambda p: f"{p.number} · {p.vendor} · {p.project.io_number} {p.project.title[:40]}"

    def clean(self):
        data = super().clean()
        # Check the new amount without counting this invoice's current amount against itself
        old = Invoice.objects.select_related("po").get(pk=self.instance.pk) if self.instance.pk else None
        check_invoice_room(data.get("po"), data.get("amount"), exclude=old)
        return data


class ChangeOrderEditForm(forms.ModelForm):
    class Meta:
        model = ChangeOrder
        fields = ["description", "value", "status", "folder_link", "comment"]
        help_texts = {"status": "Setting it to Processed adds the value to the PO; changing a processed one "
                                "adjusts the PO by the difference."}


class VOWEntryForm(DateFieldsMixin, forms.ModelForm):
    class Meta:
        model = ValueOfWork
        fields = ["reporting_date", "total_no_fees", "previous_years_no_fees", "current_year_funding"]


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ["name", "company_type", "vendor_number", "acronym", "hst_number", "phone", "website",
                  "address", "city", "province", "postal_code", "notes"]
        labels = {"company_type": "Type", "hst_number": "HST #", "vendor_number": "Vendor #"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class OwnerForm(forms.Form):
    """Who this tracker belongs to (asked on first run, editable under Settings)."""
    first_name = forms.CharField(max_length=100)
    last_name = forms.CharField(max_length=100)
    email = forms.EmailField(required=False, widget=forms.EmailInput(attrs={"placeholder": "name@bgis.com"}))
    phone = forms.CharField(max_length=50, required=False)
    position = forms.CharField(max_length=255, required=False, initial="Project Manager", label="Title")


class AccessImportForm(forms.Form):
    source = forms.CharField(label="Access file (MBN.accdb)", max_length=500,
                             widget=forms.TextInput(attrs={"placeholder": r"C:\Users\you\Downloads\MBN.accdb"}),
                             help_text="Full path to the file on this computer. It's only read, never changed.")
    only_mine = forms.BooleanField(required=False, initial=True, label="Only projects where I'm the project manager")
    confirm = forms.CharField(required=False, label="Type REPLACE to confirm",
                              help_text="Everything currently in this tracker is replaced by the Access data.")

    def __init__(self, *args, has_data=False, **kwargs):
        self.has_data = has_data
        super().__init__(*args, **kwargs)
        if not has_data:
            del self.fields["confirm"]

    def clean_source(self):
        from pathlib import Path
        p = self.cleaned_data["source"].strip().strip('"')
        if not Path(p).is_file():
            raise forms.ValidationError("That file doesn't exist. Check the path (copy it from File Explorer).")
        return p

    def clean_confirm(self):
        if self.has_data and self.cleaned_data.get("confirm", "").strip() != "REPLACE":
            raise forms.ValidationError("Type REPLACE to confirm that the current data will be replaced.")
        return "REPLACE"


class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["first_name", "last_name", "company", "position", "email", "phone"]

    def clean(self):
        data = super().clean()
        if data.get("company") and data.get("first_name") and data.get("last_name"):
            from .operations import same_name_person
            twin = same_name_person(data["company"], data["first_name"], data["last_name"], exclude=self.instance)
            if twin:
                raise forms.ValidationError(format_html(
                    '{} is already listed at {}. <a href="/people/{}/edit/">Edit that record</a> instead of adding '
                    'a duplicate.', twin, data["company"], twin.pk))
        return data



class MergeForm(forms.Form):
    target = forms.ModelChoiceField(queryset=Company.objects.all(), label="Merge into")

    def __init__(self, *args, exclude=None, **kwargs):
        super().__init__(*args, **kwargs)
        if exclude:
            self.fields["target"].queryset = Company.objects.exclude(pk=exclude.pk)


class PropertyForm(forms.ModelForm):
    class Meta:
        model = Property
        fields = ["number", "name", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class BuildingForm(forms.ModelForm):
    property_number = forms.CharField(label="Property #", required=False,
                                      help_text="Must already exist under Properties")

    class Meta:
        model = Building
        fields = ["asset_number", "name", "address", "city", "postal_code", "purpose", "floors",
                  "construction_year", "area", "heritage_status", "landlord", "bgis_building", "parent_asset",
                  "latitude", "longitude"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.property:
            self.fields["property_number"].initial = self.instance.property.number

    def clean_property_number(self):
        num = self.cleaned_data.get("property_number", "").strip()
        if not num:
            return None
        prop = Property.objects.filter(number__iexact=num).first()
        if not prop:
            raise forms.ValidationError(f"Property {num} doesn't exist. Add it under Properties first.")
        return prop

    def save(self, commit=True):
        b = super().save(commit=False)
        b.property = self.cleaned_data.get("property_number")
        if commit:
            b.save()
        return b


class BuildingRoleForm(forms.ModelForm):
    class Meta:
        model = BuildingRole
        fields = ["role", "person"]

    def __init__(self, *args, building=None, **kwargs):
        self.building = building
        super().__init__(*args, **kwargs)
        self.fields["person"].queryset = Person.objects.select_related("company")
        self.fields["person"].label_from_instance = lambda p: f"{p} · {p.company}"

    def clean(self):
        data = super().clean()
        if BuildingRole.objects.filter(building=self.building, person=data.get("person"),
                                       role=data.get("role")).exists():
            raise forms.ValidationError("That person already has this role on this building.")
        return data


def check_invoice_room(po, amount, exclude=None):
    """Raise a ValidationError if the invoice is bigger than the PO / project budget allows."""
    if po is None or amount is None or amount <= 0:   # credits are always allowed
        return
    room = invoice_room([po], exclude_invoice=exclude)[po.pk]
    if amount > room["available"]:
        raise forms.ValidationError(room_message(room, amount))


class InvoiceForm(DateFieldsMixin, forms.ModelForm):
    confirm_duplicate = forms.BooleanField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Invoice
        fields = ["number", "amount", "date"]

    def __init__(self, *args, po=None, **kwargs):
        self.po = po
        super().__init__(*args, **kwargs)
        self.fields["date"].required = True

    def clean(self):
        data = super().clean()
        num, amount = data.get("number"), data.get("amount")
        if num and amount is not None and self.po is not None:
            if Invoice.objects.filter(po=self.po, number=num).exists() and not data.get("confirm_duplicate"):
                self.fields["confirm_duplicate"].widget = forms.CheckboxInput()
                self.fields["confirm_duplicate"].label = "Yes, this is a separate invoice line"
                raise forms.ValidationError(
                    f"Invoice {num} is already recorded on PO {self.po.number}. Tick the box to add it anyway.")
            check_invoice_room(self.po, amount)
        return data


class NewInvoiceForm(InvoiceForm):
    """Add an invoice from the Invoices tab: pick the PO, then the same checks as the PO page."""
    po = forms.ModelChoiceField(queryset=PurchaseOrder.objects.none(), label="PO")

    class Meta(InvoiceForm.Meta):
        fields = ["po", "number", "amount", "date"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, po=None, **kwargs)
        self.fields["po"].queryset = PurchaseOrder.objects.select_related("vendor", "project").order_by("-number")
        self.fields["po"].label_from_instance = \
            lambda p: f"{p.number} · {p.vendor} · {p.project.io_number} {p.project.title[:40]}"
        self.order_fields(["po", "number", "amount", "date"])

    def clean(self):
        self.po = self.cleaned_data.get("po")
        return super().clean()


class ChangeOrderForm(forms.ModelForm):
    class Meta:
        model = ChangeOrder
        fields = ["description", "value", "status", "folder_link", "comment"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [c for c in ChangeOrder.State.choices
                                         if c[0] != ChangeOrder.State.PROCESSED]


class CashFlowForm(FiscalYearMixin, forms.ModelForm):
    class Meta:
        model = ProjectStatus
        fields = ["fiscal_year", "previous", "q1", "q2", "q3", "q4", "next_year", "future"]


class ForecastDatesForm(DateFieldsMixin, forms.ModelForm):
    class Meta:
        model = ProjectStatus
        fields = ["design_end", "construction_award", "substantial", "closeout"]


class ActualsForm(DateFieldsMixin, forms.ModelForm):
    class Meta:
        model = ProjectStatus
        fields = ["stage", "actual_design_end", "actual_tender", "actual_construction_award", "actual_substantial",
                  "actual_closeout", "final_mpsr"]
        labels = {"actual_tender": "Tender issued"}
        help_texts = {"actual_tender": "Due within 30 days of the actual design end"}

    def clean(self):
        data = super().clean()
        design, tender = data.get("actual_design_end"), data.get("actual_tender")
        if tender and not design:
            self.add_error("actual_tender", "Enter the actual design end first; tendering follows it.")
        elif tender and design and tender < design:
            self.add_error("actual_tender", f"Can't be before the actual design end ({design}).")
        return data


class CommentForm(forms.Form):
    text = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), label="")


class MPSRTextForm(forms.Form):
    """Status or risk comment in MPSR mode; starts with the current text so it can be edited."""
    text = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), label="", required=False)


class VOWForm(DateFieldsMixin, forms.Form):
    """Same inputs as the MPSR form: previous-years VOW (with fees) and this year's VOW (no fees)."""
    reporting_date = forms.DateField()
    previous_years_with_fees = forms.DecimalField(max_digits=14, decimal_places=2,
                                                  help_text="Previous fiscal years' VOW including fees")
    current_year_no_fees = forms.DecimalField(max_digits=14, decimal_places=2,
                                              help_text="This fiscal year's VOW, excluding fees")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reporting_date"].widget = DATE

    def to_entry(self, project, fee_percent, current_year_funding):
        prev = (self.cleaned_data["previous_years_with_fees"] / (1 + fee_percent / 100)).quantize(Decimal("0.01"))
        return dict(project=project, reporting_date=self.cleaned_data["reporting_date"],
                    previous_years_no_fees=prev, total_no_fees=prev + self.cleaned_data["current_year_no_fees"],
                    current_year_funding=current_year_funding)


class IssueForm(forms.Form):
    note = forms.CharField(required=False, max_length=500)


# Every form marks its required fields: label gets class="required", shown with a red asterisk.
_ALL_FORMS = [v for v in list(globals().values()) if isinstance(v, type) and issubclass(v, forms.BaseForm)]
for _form in _ALL_FORMS:
    _form.required_css_class = "required"
# Every dollar amount on every form is shown and accepted in financial format ($1,234.56)
money_fields_everywhere(_ALL_FORMS)

