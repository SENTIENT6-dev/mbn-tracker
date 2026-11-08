"""Import MBN.accdb into the new database.

Usage:  python manage.py import_access [--source PATH] [--reset]

The Access file is copied to a temp folder and read from the copy, so the
original is never opened or locked. Anything that cannot be imported cleanly
(duplicates, records pointing at deleted projects/POs, odd dates) is recorded
as a DataIssue for review on the "Data issues" page.
"""
import datetime as dt
import shutil
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

try:
    import pyodbc
except ImportError:          # hosted (Linux) copies have no Access driver
    pyodbc = None
from django.conf import settings
from tracker.textutil import combine_notes
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tracker.models import (PCCF, Building, BuildingRole, ChangeOrder, Company, CompanyType, DataIssue,
                            HeritageStatus, Invoice, Owner, Person, Project, ProjectStatus, Property, PurchaseOrder,
                            RoleType, StageGate, StatusHistory, ValueOfWork, po_label)

ZERO = Decimal("0")
H, M, L = DataIssue.Severity.HIGH, DataIssue.Severity.MEDIUM, DataIssue.Severity.LOW


def s(v):
    return "" if v is None else str(v).strip()


def d(v):
    if v is None:
        return None
    return v.date() if isinstance(v, dt.datetime) else v


def n(v):
    if v is None:
        return ZERO
    return Decimal(str(v)).quantize(Decimal("0.01"))


def jsonable(row):
    out = {}
    for k, v in row.items():
        if isinstance(v, (dt.date, dt.datetime)):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def aware(v):
    if v is None:
        return timezone.now()
    return timezone.make_aware(v) if timezone.is_naive(v) else v


class Command(BaseCommand):
    help = "Import data from the MBN Access database"

    def add_arguments(self, parser):
        parser.add_argument("--source", default=getattr(settings, "ACCESS_SOURCE", ""))
        parser.add_argument("--reset", action="store_true", help="Delete existing tracker data first")
        parser.add_argument("--pm", default=None,
                            help='Only import this project manager\'s projects, e.g. "Jane Smith".'
                                 "Defaults to the person this tracker belongs to.")
        parser.add_argument("--all-pms", action="store_true", help="Import every project, whoever the PM is")

    def handle(self, *args, source, reset, pm=None, all_pms=False, **opts):
        src = Path(source)
        if not src.exists():
            raise CommandError(f"Access file not found: {src}")
        if Project.objects.exists() and not reset:
            raise CommandError("The database already has data. Re-run with --reset to replace it.")
        if pyodbc is None:
            raise CommandError("Importing from Access needs Windows with pyodbc and the Microsoft Access driver.")

        # This tracker belongs to one PM: remember who, so a reload keeps them and (by default) only their projects
        owner = Owner.current()
        self.keep_owner = None if owner is None else dict(
            first_name=owner.person.first_name, last_name=owner.person.last_name, email=owner.person.email,
            phone=owner.person.phone, position=owner.person.position)
        if not all_pms and not pm and owner:
            pm = f"{owner.person.first_name} {owner.person.last_name}"
        self.pm_key = None if (all_pms or not pm) else " ".join(pm.split()).lower()
        self.source = str(src)

        tmp = Path(tempfile.mkdtemp()) / "MBN_import.accdb"
        shutil.copy2(src, tmp)
        conn = pyodbc.connect(f"Driver={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={tmp};ReadOnly=1;")
        self.cur = conn.cursor()
        self.issues = []
        try:
            with transaction.atomic():
                if reset:
                    self.wipe()
                self.run()
                DataIssue.objects.bulk_create(self.issues)
                if self.keep_owner:
                    from tracker.operations import set_owner
                    o = set_owner(**self.keep_owner)
                    o.access_source = self.source
                    o.save()
                if self.pm_key and not Project.objects.exists():
                    raise CommandError(f"No projects in this Access file have “{pm}” as project manager. "
                                       f"Nothing was changed. Check the name, or import every project.")
        finally:
            conn.close()
            shutil.rmtree(tmp.parent, ignore_errors=True)

        counts = {m.__name__: m.objects.count() for m in
                  (Company, Person, Property, Building, BuildingRole, Project, PCCF, PurchaseOrder, Invoice, ChangeOrder,
                   ProjectStatus, StatusHistory, ValueOfWork, DataIssue)}
        for k, v in counts.items():
            self.stdout.write(f"  {k:<15} {v}")
        if self.pm_key:
            self.stdout.write(f"  Only projects managed by {pm} (skipped {len(self.skipped)} others).")
        self.stdout.write(self.style.SUCCESS("Import finished."))
        call_command("resolve_issues", stdout=self.stdout)

    # ------------------------------------------------------------ helpers
    def rows(self, sql):
        self.cur.execute(sql)
        cols = [c[0] for c in self.cur.description]
        return [dict(zip(cols, r)) for r in self.cur.fetchall()]

    def issue(self, category, severity, summary, detail=None, project=None):
        self.issues.append(DataIssue(category=category, severity=severity, summary=summary,
                                     detail=detail or {}, project=project))

    def wipe(self):
        for m in (Owner, DataIssue, StatusHistory, ValueOfWork, ProjectStatus, ChangeOrder, Invoice, PurchaseOrder,
                  PCCF, Project, StageGate, BuildingRole, RoleType, Building, HeritageStatus, Property, Person,
                  Company, CompanyType):
            m.objects.all().delete()

    # ------------------------------------------------------------ import steps
    def run(self):
        self.lookups()
        self.projects()
        self.pccfs()
        self.purchase_orders()
        self.invoices()
        self.change_orders()
        self.statuses()
        self.vow()
        self.report_missing()
        self.checks()

    def lookups(self):
        self.ctype = {}
        for r in self.rows("SELECT ID, Type FROM CompanyType"):
            self.ctype[r["ID"]] = CompanyType.objects.create(name=s(r["Type"]) or f"Type {r['ID']}")

        self.company = {}
        for r in self.rows("SELECT * FROM Company"):
            self.company[r["ID"]] = Company.objects.create(
                name=s(r["Name"]) or f"Company {r['ID']}", company_type=self.ctype.get(r["Type"]),
                vendor_number=s(r["VendorNum"]), acronym=s(r["Acronym"]), hst_number=s(r["HST_Num"]),
                address=s(r["Address"]), city=s(r["City"]), province=s(r["Province"]),
                postal_code=s(r["Postal_Code"]), phone=s(r["Phone"]))

        self.person = {}
        for r in self.rows("SELECT * FROM Personnel"):
            self.person[r["ID"]] = Person.objects.create(
                first_name=s(r["First_Name"]), last_name=s(r["Last_Name"]), company=self.company[r["Company"]],
                position=s(r["Position"]), email=s(r["Email_Address"]), phone=s(r["Direct_Phone"]))

        self.buildings_and_roles()

        self.stage = {}
        for r in self.rows("SELECT * FROM Stage_Gates"):
            self.stage[r["ID"]] = StageGate.objects.create(name=s(r["Stage_Name"]), order=r["List_Value"] or 0)

    def buildings_and_roles(self):
        rows = self.rows("SELECT * FROM Facilities")

        # Properties: one per property number, named by the most common name among its buildings
        names = defaultdict(Counter)
        for r in rows:
            if s(r["Property_Number"]):
                names[s(r["Property_Number"])][s(r["Property_Name"])] += 1
        Property.objects.bulk_create([Property(number=num, name=c.most_common(1)[0][0]) for num, c in names.items()],
                                     batch_size=1000)
        props = {p.number: p for p in Property.objects.all()}
        for num, c in names.items():
            if len(c) > 1:
                self.issue("Property with two names", L,
                           f"Property {num} was named {' / '.join(repr(n) for n in c)} on different buildings; "
                           f"kept '{c.most_common(1)[0][0]}'.")

        heritage = {r["ID"]: HeritageStatus.objects.create(name=s(r["Status"]) or f"Status {r['ID']}")
                    for r in self.rows("SELECT * FROM HeritageStatus")}

        def num(v):
            return v if v not in (None, 0) else None

        Building.objects.bulk_create([
            Building(asset_number=s(r["Asset_Number"]), name=s(r["Asset_name"]),
                     property=props.get(s(r["Property_Number"])), parent_asset=s(r["Parent_Asset"]),
                     address=s(r["Address"]), city=s(r["City"]), postal_code=s(r["Postal_Code"]),
                     purpose=s(r["Purpose"]), floors=num(r["Floors"]), construction_year=num(r["Const_Year"]),
                     heritage_status=heritage.get(r["Heritage_Status"]), landlord=s(r["Landlord"]),
                     bgis_building=s(r["BGIS_BLDG"]), latitude=r["Latitude"], longitude=r["Longitude"],
                     area=num(r["Area"]))
            for r in rows], batch_size=1000)
        self.building = {b.asset_number: b for b in Building.objects.all()}

        role_types = {r["ID"]: RoleType.objects.create(name=s(r["Role_Name"]))
                      for r in self.rows("SELECT * FROM Roles_Categories")}
        roles, missing, seen = [], 0, set()
        for r in self.rows("SELECT Personnel_ID, BUILDING_ID, Role_ID FROM Roles"):
            person, bldg = self.person.get(r["Personnel_ID"]), self.building.get(s(r["BUILDING_ID"]))
            if not person or not bldg or r["Role_ID"] not in role_types:
                missing += 1
                continue
            key = (bldg.pk, person.pk, r["Role_ID"])
            if key not in seen:
                seen.add(key)
                roles.append(BuildingRole(building=bldg, person=person, role=role_types[r["Role_ID"]]))
        BuildingRole.objects.bulk_create(roles, batch_size=2000)
        if missing:
            self.issue("Role for missing person", L,
                       f"{missing:,} building-contact links pointed at people or buildings that no longer exist; skipped.")

    def projects(self):
        self.project = {}
        self.skipped = set()       # Access project ids left out (another PM's projects)
        self.skipped_pos = set()   # their PO numbers, so invoices / change orders on them are skipped quietly
        for r in self.rows("SELECT * FROM Projects"):
            if self.pm_key:
                pm = self.person.get(r["Project_Manager"])
                if not pm or f"{pm.first_name} {pm.last_name}".lower().split() != self.pm_key.split():
                    self.skipped.add(r["ID"])
                    continue
            bldg = s(r["Building"])
            p = Project.objects.create(
                io_number=s(r["IO_ProjNum"]), bgis_number=s(r["BGIS_ProjNum"]), title=s(r["Proj_Title"]),
                building=self.building.get(bldg),
                building_code="" if bldg in self.building else bldg,
                project_manager=self.person.get(r["Project_Manager"]), client=self.company.get(r["Client"]),
                kind=r["Type"] or 1, phase=r["Phase"], status=r["Status"] or Project.Status.UNKNOWN)
            self.project[r["ID"]] = p
            if bldg and bldg not in self.building:
                self.issue("Unknown building", L, f"{p.io_number}: building '{bldg}' is not in the buildings list",
                           project=p)

        dup = defaultdict(list)
        for p in self.project.values():
            if p.bgis_number:
                dup[p.bgis_number].append(p.io_number)
        for num, ios in dup.items():
            if len(ios) > 1:
                self.issue("Duplicate BGIS number", L, f"BGIS number {num} is used by projects {', '.join(ios)}")

        self.missing = defaultdict(lambda: defaultdict(int))  # access project id -> {record kind: count}

    def proj(self, access_id, kind):
        """Project for an Access ID; records pointing at a deleted project go under a placeholder.

        Returns None for records that belong to another PM's project (when importing one PM's projects);
        records of deleted projects are left out too then, since there's no way to tell whose they were.
        """
        if access_id in self.skipped:
            return None
        if access_id not in self.project and self.pm_key:
            self.skipped.add(access_id)
            return None
        if access_id not in self.project:
            self.project[access_id] = Project.objects.create(
                io_number=f"MISSING-{access_id}",
                title=f"Deleted Access project #{access_id} – reassign these records",
                status=Project.Status.UNKNOWN, needs_review=True)
        if self.project[access_id].needs_review:
            self.missing[access_id][kind] += 1
        return self.project[access_id]

    def report_missing(self):
        for access_id, kinds in self.missing.items():
            p = self.project[access_id]
            po_total = sum(po.amount for po in p.purchase_orders.all())
            what = ", ".join(f"{c} {k}" for k, c in kinds.items())
            sev = H if po_total else M
            self.issue("Records point to deleted project", sev,
                       f"Access project #{access_id} no longer exists but still had {what}"
                       + (f" (POs worth ${po_total:,})" if po_total else "")
                       + ". Access hid these from its reports. Move them to the right project, or delete them.",
                       {"access_project_id": access_id, **kinds}, p)

    def pccfs(self):
        groups = defaultdict(list)
        for r in self.rows("SELECT * FROM PCCF ORDER BY ID"):
            groups[(r["Project"], r["Submission"] or 0)].append(r)
        latest = defaultdict(int)
        for (proj, sub) in groups:
            latest[proj] = max(latest[proj], sub)

        money_keys = ("DesignCost", "ConstrCost", "DesignCont", "ConstrCont", "Fee", "Q1", "Q2", "Q3", "Q4", "Next",
                      "Future")
        for (proj, sub), rs in groups.items():
            keep = rs[-1]  # the most recently entered row
            project = self.proj(proj, "PCCF submissions")
            if project is None:
                continue
            obj = PCCF.objects.create(
                project=project, submission=sub, date=d(keep["Date"]), final=bool(keep["FinalPCCF"]),
                notes=combine_notes([("Scope", s(keep["ScopeChangeDetails"])), ("Methodology", s(keep["MethChange"])),
                                     ("Milestones", s(keep["MilestoneComment"])), ("Budget", s(keep["BudgetNotes"]))]),
                approval_date=d(keep["ApprovalDate"]), design_start=d(keep["DesignStartDate"]),
                design_end=d(keep["DesignEndDate"]), construction_award=d(keep["ConstAwardDate"]),
                substantial=d(keep["SubsPerfDate"]), closeout=d(keep["CloseOutDate"]),
                design_cost=n(keep["DesignCost"]), construction_cost=n(keep["ConstrCost"]),
                design_contingency=n(keep["DesignCont"]), construction_contingency=n(keep["ConstrCont"]),
                fee_percent=Decimal(str(keep["FeePercent"] or 0)).quantize(Decimal("0.0001")), fee=n(keep["Fee"]),
                fiscal_year=keep["FiscalYear"], previous=n(keep["Previous"]), q1=n(keep["Q1"]), q2=n(keep["Q2"]),
                q3=n(keep["Q3"]), q4=n(keep["Q4"]), next_year=n(keep["Next"]), future=n(keep["Future"]))

            if abs(obj.total_budget - n(keep["TotalBudget"])) > 1:
                self.issue("Budget total mismatch", M,
                           f"{project.io_number} PCCF {sub}: parts add up to ${obj.total_budget:,} but Access stored "
                           f"${n(keep['TotalBudget']):,}", project=project)

            for extra in rs[:-1]:
                same = all(n(extra[k]) == n(keep[k]) for k in money_keys)
                if same:
                    sev, what = L, "an identical copy was dropped"
                elif sub == latest[proj] and project.status == Project.Status.ACTIVE:
                    sev, what = H, "the copies differ and this is the current budget of an active project"
                else:
                    sev, what = M, "the copies differ"
                self.issue("Duplicate PCCF submission", sev,
                           f"{project.io_number} had two PCCF {sub} entries; {what}. Kept the later one "
                           f"(total ${obj.total_budget:,}); the dropped one totalled "
                           f"${sum(n(extra[k]) for k in ('DesignCost', 'ConstrCost', 'DesignCont', 'ConstrCont', 'Fee')):,}.",
                           {"kept_access_id": keep["ID"], "dropped": jsonable(extra)}, project)

    def purchase_orders(self):
        self.po = {}
        for r in self.rows("SELECT * FROM PO"):
            project = self.proj(r["Project"], "POs")
            if project is None:
                self.skipped_pos.add(r["PONumber"])
                continue
            self.po[r["PONumber"]] = PurchaseOrder.objects.create(
                number=po_label(r["PONumber"]), project=project, vendor=self.company[r["Vendor"]],
                approved_date=d(r["ApprovedDate"]), amount=n(r["Order"]))
        self.po_numbers = set(self.po)

    def invoices(self):
        seen = set()
        by_number_po = defaultdict(list)
        today = dt.date.today()
        for r in self.rows("SELECT * FROM Invoices ORDER BY ID"):
            num, po, amt, date = s(r["InvoiceNumber"]), r["PONumber"], n(r["Amount"]), d(r["ExpItemDate"])
            if po in self.skipped_pos:
                continue   # another PM's project
            if po not in self.po_numbers:
                self.issue("Invoice on unknown PO", H,
                           f"Invoice {num} for ${amt:,} is on PO {po}, which doesn't exist. Not imported.",
                           jsonable(r))
                continue
            key = (po, num, amt, date)
            if key in seen:
                self.issue("Duplicate invoice", H,
                           f"Invoice {num} on PO {po_label(po)} for ${amt:,} dated {date} was entered twice in Access and "
                           f"counted twice. Imported once.", jsonable(r))
                continue
            seen.add(key)
            inv = Invoice.objects.create(po=self.po[po], number=num, amount=amt, date=date)
            by_number_po[(po, num)].append(inv)
            if date and (date.year < 2010 or date > today + dt.timedelta(days=365)):
                self.issue("Suspicious invoice date", M, f"Invoice {num} on PO {po_label(po)} is dated {date}.",
                           {"invoice_id": inv.id}, inv.po.project)
        for (po, num), invs in by_number_po.items():
            if len(invs) > 1:
                self.issue("Repeated invoice number", L,
                           f"Invoice {num} appears {len(invs)} times on PO {po_label(po)} with different amounts "
                           f"({', '.join(f'${i.amount:,}' for i in invs)}). Probably split lines; check if not.",
                           project=invs[0].po.project)

    def change_orders(self):
        for r in self.rows("SELECT * FROM PendingCO"):
            if r["PO"] in self.skipped_pos or r["PO"] not in self.po:
                continue   # another PM's project
            if r["Processed"]:
                state = ChangeOrder.State.PROCESSED
            elif r["Approved"]:
                state = ChangeOrder.State.APPROVED
            else:
                state = ChangeOrder.State.REQUESTED
            co = ChangeOrder.objects.create(po=self.po[r["PO"]], description=s(r["Description"])[:255] or "(no description)",
                                            value=n(r["Change_Value"]), status=state,
                                            folder_link=s(r["LinkToFolder"]), comment=s(r["Comment"]))
            if self.project.get(r["Project"]) != co.po.project:
                self.issue("Change order filed under wrong project", L,
                           f"Change order '{co.description}' (${co.value:,}) on PO {co.po.number} was filed under "
                           f"{getattr(self.project.get(r['Project']), 'io_number', r['Project'])} in Access but the PO "
                           f"belongs to {co.po.project.io_number}. It now follows the PO.", project=co.po.project)

    def statuses(self):
        for r in self.rows("SELECT * FROM Current_Project_Status"):
            project = self.proj(r["Project"], "status records")
            if project is None:
                continue
            ProjectStatus.objects.create(
                project=project, fiscal_year=r["Fiscal_Year"], previous=n(r["Previous"]),
                q1=n(r["Q1"]), q2=n(r["Q2"]), q3=n(r["Q3"]), q4=n(r["Q4"]), next_year=n(r["Next"]),
                future=n(r["Future"]), design_end=d(r["DesignEnd"]), construction_award=d(r["ConstructionAward"]),
                substantial=d(r["Substantial"]), closeout=d(r["Closeout"]),
                actual_design_end=d(r["ActualDesignEnd"]),
                actual_construction_award=d(r["ActualConstructionAward"]),
                actual_substantial=d(r["ActualSubstantial"]), actual_closeout=d(r["ActualCloseout"]),
                stage=self.stage.get(r["ProjectStage"]), comments=s(r["Comments"]), risk=s(r["Risk"]),
                final_mpsr=bool(r["FinalMPSR"]))

        hist = []
        K = StatusHistory.Kind
        for r in self.rows("SELECT * FROM Project_Comment_History"):
            hist.append(StatusHistory(project=self.proj(r["Project"], "history entries"), kind=K.COMMENT,
                                      changed_at=aware(r["Change_Date"]), text=s(r["Comments"])))
        for r in self.rows("SELECT * FROM Project_Risk_History"):
            hist.append(StatusHistory(project=self.proj(r["Project"], "history entries"), kind=K.RISK,
                                      changed_at=aware(r["Change_Date"]), text=s(r["Risk_Comment"])))
        for r in self.rows("SELECT * FROM Project_Date_History"):
            data = {k: (d(r[a]).isoformat() if r[a] else None) for k, a in
                    (("design_end", "DesignEnd"), ("construction_award", "ConstructionAward"),
                     ("substantial", "Substantial"), ("closeout", "Closeout"))}
            hist.append(StatusHistory(project=self.proj(r["Project"], "history entries"), kind=K.DATES,
                                      changed_at=aware(r["Change_Date"]), data=data))
        for r in self.rows("SELECT * FROM Project_Financial_History"):
            data = {k: float(n(r[a])) for k, a in (("previous", "Previous"), ("q1", "Q1"), ("q2", "Q2"),
                                                     ("q3", "Q3"), ("q4", "Q4"), ("next_year", "Next"),
                                                     ("future", "Future"))}
            data["fiscal_year"] = r["Fiscal_Year"]
            hist.append(StatusHistory(project=self.proj(r["Project"], "history entries"), kind=K.CASH_FLOW,
                                      changed_at=aware(r["Change_Date"]), data=data))
        StatusHistory.objects.bulk_create([h for h in hist if h.project_id is not None])

    def vow(self):
        by_day = {}
        for r in self.rows("SELECT * FROM VOW ORDER BY Project, ReportingDate"):
            key = (r["Project"], d(r["ReportingDate"]))
            p = self.proj(r["Project"], "VOW entries")
            if p is None:
                continue
            if key in by_day:
                self.issue("Same-day VOW entries", L,
                           f"{p.io_number} had two VOW entries on {key[1]}; kept the later one.", project=p)
            by_day[key] = r
        ValueOfWork.objects.bulk_create([
            ValueOfWork(project=self.project[p], reporting_date=day, total_no_fees=n(r["TotalVOW_NoFees"]),
                        previous_years_no_fees=n(r["PreviousVOW_NoFees"]),
                        current_year_funding=n(r["CurrentYearFunding_MPSR"]))
            for (p, day), r in by_day.items()])

    def checks(self):
        from tracker.operations import merge_duplicate_people
        from tracker.services import flag_out_of_order_schedules
        flag_out_of_order_schedules()   # current PCCFs whose milestone dates go backwards
        for kept, n in merge_duplicate_people(Project.pm_company()):   # BGIS staff entered twice in Access
            self.issue("Duplicate person merged", L,
                       f"{kept} was in Access {n + 1} times at BGIS; merged into one record with all their projects.")
        for po in PurchaseOrder.objects.select_related("project"):
            inv = po.invoiced()
            if inv > po.amount + po.pending_changes() + Decimal("0.01"):
                self.issue("PO over-invoiced", M,
                           f"PO {po.number} is invoiced ${inv:,} against a value of ${po.amount:,}.", project=po.project)
        for p in Project.objects.filter(status=Project.Status.ACTIVE):
            if not p.vow_entries.exists():
                self.issue("No value of work", L, f"Active project {p.io_number} has no VOW entries.", project=p)
            if not p.pccfs.exists():
                self.issue("No PCCF", M, f"Active project {p.io_number} has no PCCF submission.", project=p)
            if not hasattr(p, "current_status"):
                ProjectStatus.objects.create(project=p)
