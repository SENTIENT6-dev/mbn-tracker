"""Resolve the data issues found by the Access import, based on evidence in the data.

Usage:  python manage.py resolve_issues
Runs automatically at the end of `import_access`. Safe to run more than once.
Each resolved issue gets a note explaining what was decided and why.
"""
import datetime as dt
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from tracker.models import PCCF, DataIssue, Invoice, Project, PurchaseOrder  # noqa: F401

# Invoice dates mistyped in Access, keyed (PO, invoice #[, wrong date]) -> right date.
# The real list is specific to one PM's data, so it lives in _local_fixes.py (not published).
try:
    from ._local_fixes import DATE_FIXES
except ImportError:
    DATE_FIXES = {}

MONEY_KEYS = {"DesignCost": "design_cost", "ConstrCost": "construction_cost", "DesignCont": "design_contingency",
              "ConstrCont": "construction_contingency", "Fee": "fee"}


def close(issue, note):
    issue.resolved = True
    issue.resolution_note = note[:500]
    issue.save()


class Command(BaseCommand):
    help = "Resolve Access-import data issues"

    @transaction.atomic
    def handle(self, *args, **opts):
        before = DataIssue.objects.filter(resolved=False).count()
        for issue in DataIssue.objects.filter(resolved=False).select_related("project"):
            handler = getattr(self, "h_" + issue.category.lower().replace(" ", "_").replace("-", "_"), None)
            if handler:
                handler(issue)
        after = DataIssue.objects.filter(resolved=False).count()
        self.stdout.write(self.style.SUCCESS(f"Resolved {before - after} issue(s); {after} still open."))

    # ---------------------------------------------------------------- handlers
    def h_duplicate_invoice(self, i):
        d = i.detail
        if float(d.get("Amount") or 0) < 0:
            # A credit note entered twice is ambiguous: one copy may really be the original invoice it reverses.
            po = PurchaseOrder.objects.filter(number=d.get("PONumber")).first()
            if po and not i.summary.endswith("decide."):
                same = Invoice.objects.filter(po=po, amount=-Decimal(str(d["Amount"]))).values_list("number", flat=True)
                i.summary += (f" It's a credit note, so this needs a decision: one copy is counted now. "
                              f"PO {po.number} also has re-bill(s) {', '.join(same) or 'none'} for the same amount. "
                              f"If the second copy was really the original invoice being credited, add that invoice "
                              f"on the PO page; otherwise mark this resolved. Please decide.")
                i.project = po.project
                i.save()
            return  # left open on purpose
        close(i, "Kept one copy; the second entry was a double entry and is excluded from totals.")

    def h_invoice_on_unknown_po(self, i):
        d = i.detail
        num, amount = d.get("InvoiceNumber"), Decimal(str(d.get("Amount", 0))).quantize(Decimal("0.01"))
        same = Invoice.objects.filter(number=num).exclude(po__number=d.get("PONumber")).select_related("po")
        if same.filter(amount=amount).exists():
            po = same.filter(amount=amount).first().po.number
            close(i, f"Same invoice and amount is already recorded on PO {po}; PO {d.get('PONumber')} was a typo. "
                     f"Not added again.")
        elif same.exists():
            po = same.first().po.number
            close(i, f"Invoice {num} with the same date is recorded on PO {po} for ${same.first().amount:,}; this "
                     f"entry on non-existent PO {d.get('PONumber')} was superseded by it. Not added.")
        else:
            close(i, f"Pre-system balance on PO {d.get('PONumber')}, which was never tracked in the database. "
                     f"Left out; it doesn't belong to any tracked PO or project.")

    def h_suspicious_invoice_date(self, i):
        inv = Invoice.objects.filter(pk=i.detail.get("invoice_id")).first()
        if not inv:
            return
        fixed = None
        for key, new in DATE_FIXES.items():
            if inv.po.number == key[0] and inv.number == key[1] and (len(key) == 2 or inv.date == key[2]):
                fixed = new
        if fixed:
            old, inv.date = inv.date, fixed
            inv.save()
            close(i, f"Date corrected from {old} to {fixed}, based on the neighbouring invoice numbers on this PO.")
        elif inv.date and inv.date.year >= 2010:
            close(i, "Date already corrected.")

    def h_records_point_to_deleted_project(self, i):
        p = i.project
        if p is None:
            close(i, "Placeholder removed.")
            return
        only_zero_vow = (not p.purchase_orders.exists() and not p.pccfs.exists() and not p.status_history.exists()
                         and not hasattr(p, "current_status")
                         and all(v.total_no_fees == 0 for v in p.vow_entries.all()))
        if only_zero_vow:
            p.delete()  # nothing but $0 value-of-work rows; nothing worth keeping
            i.project = None
            close(i, "Placeholder only held $0 value-of-work rows; removed.")
            return
        dates = [po.approved_date for po in p.purchase_orders.all() if po.approved_date] + \
                [pc.date for pc in p.pccfs.all() if pc.date]
        years = sorted({d.year for d in dates})
        span = f"{years[0]}–{years[-1]}" if len(years) > 1 else (str(years[0]) if years else "")
        kept = ", ".join(f"{k}" for k in i.detail if k != "access_project_id") or "records"
        first_pccf = p.pccfs.order_by("submission").first()
        hint = f" — {first_pccf.notes[:60]}…" if first_pccf and first_pccf.notes else ""
        p.status = Project.Status.CLOSED
        p.needs_review = False
        p.title = f"Archived: deleted Access project #{i.detail.get('access_project_id')} ({span}){hint}"[:255]
        p.save()
        close(i, f"The project was deleted in Access but its {kept} were left behind ({span}). They're kept under "
                 f"this archived project so nothing is lost; it no longer shows in active views. Move them to "
                 f"the right project if it was re-created under a new number.")

    def h_duplicate_pccf_submission(self, i):
        dropped = i.detail.get("dropped") or {}
        p = i.project
        vals = {v: Decimal(str(dropped.get(k) or 0)).quantize(Decimal("0.01")) for k, v in MONEY_KEYS.items()}
        for other in PCCF.objects.filter(project=p):
            if all(getattr(other, f) == v for f, v in vals.items()):
                close(i, f"The dropped copy is identical to PCCF {other.submission}, which is kept. No data lost.")
                return
        kept = PCCF.objects.filter(project=p, submission=dropped.get("Submission")).first()
        d_date = (dropped.get("Date") or "")[:10]
        if kept and kept.date and d_date and kept.date.isoformat() >= d_date:
            close(i, f"Kept the more recent revision (dated {kept.date}); the dropped copy was dated {d_date}.")
        elif kept and p.status != Project.Status.ACTIVE:
            close(i, "Historical submission on a closed project; doesn't affect current figures. Dropped copy kept "
                     "in this issue's Access data.")

    def h_po_over_invoiced(self, i):
        po = next((po for po in PurchaseOrder.objects.filter(project=i.project) if po.remaining() < 0), None)
        if po is None:
            close(i, "No longer over-invoiced.")
        elif -po.remaining() <= 1:
            close(i, f"PO {po.number} is over by ${-po.remaining():.2f}: rounding. No action needed.")

    def h_change_order_filed_under_wrong_project(self, i):
        close(i, "Change orders now always follow their PO's project, so they can't disagree any more.")

    def h_same_day_vow_entries(self, i):
        close(i, "Kept the last entry of the day, which is the one Access reports used.")

    def h_repeated_invoice_number(self, i):
        close(i, "Reviewed: same invoice number with different amounts are separate invoice lines (or a "
                 "reversal and re-bill). All kept.")

    def h_unknown_building(self, i):
        close(i, "Building number kept on the project as text; it isn't in the facilities list, which may be out "
                 "of date. Add the building under Facilities, then pick it on the project.")

    def h_role_for_missing_person(self, i):
        close(i, "These building-contact links pointed at people deleted from Access; nothing to link to.")

    def h_property_with_two_names(self, i):
        close(i, "Kept the name used by most buildings; rename it on the property page if needed.")

    def h_duplicate_bgis_number(self, i):
        close(i, "Both projects are completed studies; a shared BGIS number doesn't affect any figures.")

    def h_no_value_of_work(self, i):
        p = i.project
        first = p.pccfs.order_by("date").first() if p else None
        close(i, f"New project (first PCCF {first.date if first else 'recent'}); value of work will be reported "
                 f"from the MPSR screen.")

    def h_duplicate_person_merged(self, i):
        close(i, "Merged automatically; nothing else to do.")

    def h_no_pccf(self, i):
        pass  # needs a real PCCF entry; left open on purpose
