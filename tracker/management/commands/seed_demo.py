"""Fill the tracker with made-up demo data (every name, number and amount is fictional).

Usage:  python manage.py seed_demo [--reset]
Without --reset it does nothing if projects already exist, so it's safe to run on every start.
"""
import datetime as dt
import random
from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tracker.models import (PCCF, Building, BuildingRole, ChangeOrder, Company, CompanyType,
                            HeritageStatus, Invoice, MPSRReview, Owner, Person, Project, ProjectStatus, Property,
                            PurchaseOrder, RoleType, StageGate, StatusHistory, ValueOfWork)
from tracker.operations import set_owner
from tracker.textutil import current_fiscal_year

C = Decimal("0.01")
STAGES = ["Initiation", "Planning", "33% Design", "66% Design", "99% Design", "Tender", "Construction", "Closeout"]

VENDORS = [  # name, type, acronym, city
    ("Northwind Builders Ltd.", "Contractor", "NWB", "Toronto"),
    ("Maple Leaf Design Group", "Consultant", "MLDG", "Ottawa"),
    ("Lakeshore Mechanical Inc.", "Contractor", "LMI", "Hamilton"),
    ("Granite Ridge Engineering", "Consultant", "GRE", "Kingston"),
    ("Bluewater Electrical Co.", "Contractor", "BEC", "Sarnia"),
    ("Pinecrest Environmental", "Consultant", "PCE", "Sudbury"),
]

BUILDINGS = [  # asset #, name, property #, address, city, floors, year
    ("B10001", "Riverside Courthouse", "P501", "100 Example Ave", "Toronto", 6, 1978),
    ("B10002", "Riverside Annex", "P501", "104 Example Ave", "Toronto", 2, 1992),
    ("B10010", "North Bay Service Centre", "P502", "25 Sample Rd", "North Bay", 3, 1985),
    ("B10020", "Lakeview Government Office", "P503", "8 Demo Street", "Kingston", 9, 1969),
    ("B10021", "Lakeview Parking Structure", "P503", "10 Demo Street", "Kingston", 4, 1971),
    ("B10030", "Westfield Records Building", "P504", "300 Placeholder Blvd", "London", 3, 2001),
]

PROJECTS = [  # IO #, title, building, status, phase, stage, budget, months since start, progress 0..1
    ("1100101", "Roof replacement", "B10001", 1, 3, "Construction", 1_450_000, 22, 0.62),
    ("1100102", "Elevator modernization (2 cars)", "B10001", 1, 1, "66% Design", 980_000, 9, 0.12),
    ("1100103", "HVAC chiller replacement", "B10020", 1, 6, "Tender", 2_300_000, 14, 0.08),
    ("1100104", "Parking garage concrete repairs", "B10021", 1, 3, "Construction", 3_150_000, 30, 0.81),
    ("1100105", "Fire alarm system upgrade", "B10010", 1, 1, "33% Design", 640_000, 5, 0.04),
    ("1100106", "Accessibility washroom upgrades", "B10030", 2, 1, "Planning", 410_000, 7, 0.02),
    ("1100107", "Window and curtain wall repairs", "B10002", 1, 4, "Closeout", 760_000, 40, 0.97),
    ("1100108", "Building condition assessment", "B10020", 4, 5, "Closeout", 120_000, 26, 1.0),
    ("1100109", "Emergency generator replacement", "B10010", 1, 3, "Construction", 1_080_000, 18, 0.55),
    ("1100110", "Lighting retrofit (LED)", "B10030", 1, 1, "99% Design", 350_000, 11, 0.10),
]


def m(x):
    return Decimal(x).quantize(C, ROUND_HALF_UP)


def add_months(d, n):
    y, mth = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, mth + 1, min(d.day, 28))


class Command(BaseCommand):
    help = "Load fictional demo data"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete everything first")

    @transaction.atomic
    def handle(self, *args, reset=False, **opts):
        if reset:
            for model in (Owner, StatusHistory, MPSRReview, ValueOfWork, ProjectStatus, ChangeOrder,
                          Invoice, PurchaseOrder, PCCF, Project, StageGate, BuildingRole, RoleType, Building,
                          HeritageStatus, Property, Person, Company, CompanyType):
                model.objects.all().delete()
        elif Project.objects.exists():
            self.stdout.write("Projects already exist; nothing loaded (use --reset to replace them).")
            return

        rnd = random.Random(42)                       # same demo every time
        today = timezone.localdate()
        fy = current_fiscal_year(today)

        types = {n: CompanyType.objects.get_or_create(name=n)[0]
                 for n in ("Consultant", "Contractor", "Client", "Project Manager", "Facility Manager")}
        stages = {n: StageGate.objects.get_or_create(name=n, defaults={"order": i})[0] for i, n in enumerate(STAGES)}
        roles = {n: RoleType.objects.get_or_create(name=n)[0] for n in ("Facility Manager", "PREM")}
        heritage = HeritageStatus.objects.get_or_create(name="Not Heritage")[0]

        owner = set_owner("Alex", "Morgan", email="alex.morgan@example.com", phone="(555) 010-0100")
        Company.objects.filter(name=Project.PM_COMPANY).update(company_type=types["Project Manager"])
        Company.objects.get_or_create(name=Project.CLIENT_NAME, defaults={"acronym": "IO", "company_type": types["Client"]})

        vendors = []
        for i, (name, kind, acr, city) in enumerate(VENDORS):
            v = Company.objects.create(name=name, company_type=types[kind], acronym=acr, city=city, province="ON",
                                       vendor_number=f"V{7000 + i}", phone=f"(555) 01{i}-{1000 + i * 7}",
                                       address=f"{10 + i * 5} Fictional Way")
            Person.objects.create(first_name=["Jordan", "Priya", "Marc", "Lena", "Omar", "Chris"][i],
                                  last_name=["Lee", "Shah", "Tremblay", "Novak", "Haddad", "Wong"][i], company=v,
                                  position="Project lead", email=f"contact{i + 1}@example.com")
            vendors.append(v)
        fm_co = Company.objects.create(name="Demo Facility Services", company_type=types["Facility Manager"])
        fm = Person.objects.create(first_name="Robin", last_name="Taylor", company=fm_co, position="Facility Manager",
                                   email="robin.taylor@example.com")

        props, blds = {}, {}
        for asset, name, prop, addr, city, floors, year in BUILDINGS:
            if prop not in props:
                props[prop] = Property.objects.create(number=prop, name=f"{name.split()[0]} Complex")
            b = Building.objects.create(asset_number=asset, name=name, property=props[prop], address=addr, city=city,
                                        floors=floors, construction_year=year, heritage_status=heritage,
                                        purpose="Office", area=floors * 2400.0)
            BuildingRole.objects.create(building=b, person=fm, role=roles["Facility Manager"])
            blds[asset] = b

        po_no = 99900001
        for io, title, asset, status, phase, stage, budget, age, progress in PROJECTS:
            start = add_months(today, -age)
            p = Project.objects.create(io_number=io, title=title, building=blds[asset], status=status, phase=phase,
                                       bgis_number=f"BG-{io[-3:]}", kind=Project.Kind.CAPITAL if budget > 1e6
                                       else Project.Kind.MINISTRY, project_manager=owner.person)
            # charter: design 12%, construction 70%, contingencies 4% + 14%, fee 8%
            base = Decimal(budget)
            parts = dict(design_cost=m(base * Decimal(".12")), construction_cost=m(base * Decimal(".70")),
                         design_contingency=m(base * Decimal(".04")))
            parts["construction_contingency"] = base - sum(parts.values())
            dates = dict(approval_date=start, design_start=add_months(start, 1), design_end=add_months(start, 8),
                         construction_award=add_months(start, 11), substantial=add_months(start, 24),
                         closeout=add_months(start, 30))
            total = m(base * Decimal("1.08"))
            spent_before = m(total * Decimal(str(min(progress, .9))) * Decimal(".6"))
            rest = total - spent_before
            q = [m(rest * Decimal(f)) for f in (".1", ".12", ".12", ".11")]
            nxt = m(rest * Decimal(".35"))
            cash = dict(previous=spent_before, q1=q[0], q2=q[1], q3=q[2], q4=q[3], next_year=nxt,
                        future=rest - sum(q) - nxt)
            PCCF.objects.create(project=p, submission=0, date=start, fiscal_year=fy, fee_percent=Decimal("8"),
                                fee=total - base, final=False, notes="Charter: scope as per building condition "
                                "assessment. Fictional demo data.", **parts, **dates, **cash)

            ps = ProjectStatus.objects.create(
                project=p, fiscal_year=fy, stage=stages[stage], **cash,
                design_end=dates["design_end"], construction_award=dates["construction_award"],
                substantial=add_months(dates["substantial"], rnd.choice([0, 0, 1, 2])), closeout=dates["closeout"],
                comments=f"{stage} under way; no issues to report this month.",
                risk="Supply lead times for major equipment." if rnd.random() < .5 else "")
            for field, done in (("design_end", 9), ("construction_award", 12), ("substantial", 25), ("closeout", 31)):
                if age >= done and rnd.random() < .85:
                    setattr(ps, "actual_" + field, add_months(start, done - 1))
            if ps.actual_design_end and age >= 11:          # tendered, some on time and some late
                ps.actual_tender = ps.actual_design_end + dt.timedelta(days=rnd.choice([12, 21, 28, 41]))
            ps.save()

            # consultant PO, plus a contractor PO once tendered
            design_vendor = vendors[rnd.choice([1, 3, 5])]
            pos = [(design_vendor, parts["design_cost"], dates["design_start"])]
            if age >= 11:
                pos.append((vendors[rnd.choice([0, 2, 4])], m(parts["construction_cost"] * Decimal(".97")),
                            dates["construction_award"]))
            for vendor, amount, approved in pos:
                seq = po_no % 1000
                po = PurchaseOrder.objects.create(number=f"ION{po_no % 100000:05d}", project=p, vendor=vendor,
                                                  amount=amount, approved_date=approved)
                po_no += 1
                share = Decimal(str(min(progress * (1.3 if vendor is design_vendor else 1), 1)))
                to_bill, n, d = m(amount * share), 1, add_months(approved, 1)
                while to_bill > 0 and d <= today:
                    amt = min(to_bill, m(amount * Decimal(str(rnd.uniform(.08, .2)))))
                    Invoice.objects.create(po=po, number=f"INV-{seq:03d}-{n:02d}", amount=amt, date=d)
                    to_bill -= amt
                    n += 1
                    d = add_months(d, rnd.choice([1, 1, 2]))
            if age >= 14 and rnd.random() < .7:
                ChangeOrder.objects.create(po=po, description="Additional work found on site", status=rnd.choice(
                    [ChangeOrder.State.APPROVED, ChangeOrder.State.REQUESTED]), value=m(base * Decimal(".02")))

            ValueOfWork.objects.create(project=p, reporting_date=add_months(today, -1),
                                       total_no_fees=m(base * Decimal(str(progress))),
                                       previous_years_no_fees=m(base * Decimal(str(progress)) * Decimal(".6")),
                                       current_year_funding=sum(q))
            StatusHistory.objects.create(project=p, kind=StatusHistory.Kind.COMMENT,
                                         changed_at=timezone.now() - dt.timedelta(days=30),
                                         text=f"Moved to {stage}.")

        # one project over budget, to show the warnings
        over = PurchaseOrder.objects.filter(project__io_number="1100104").order_by("-amount").first()
        if over:
            ChangeOrder.objects.create(po=over, description="Unforeseen rebar corrosion", value=Decimal("700000"),
                                       status=ChangeOrder.State.APPROVED)

        self.stdout.write(self.style.SUCCESS(
            f"Demo data loaded: {Project.objects.count()} projects, {PurchaseOrder.objects.count()} POs, "
            f"{Invoice.objects.count()} invoices for {owner.person}."))
