import datetime as dt
from decimal import Decimal as D

from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import (PCCF, Building, ChangeOrder, Company, CompanyType, Invoice, Person, Project, ProjectStatus,
                     Property, PurchaseOrder, StatusHistory, ValueOfWork)
from .services import milestones, project_financials


class Base(TestCase):
    def setUp(self):
        from .operations import set_owner
        self.owner = set_owner("Pat", "Manager", email="pat.manager@bgis.com")   # this tracker's PM
        consultant = CompanyType.objects.create(name="Consultant")
        contractor = CompanyType.objects.create(name="Contractor")
        self.arch = Company.objects.create(name="Arch Co", company_type=consultant)
        self.build = Company.objects.create(name="Build Co", company_type=contractor)
        self.p = Project.objects.create(io_number="1000001", title="Roof")
        ProjectStatus.objects.create(project=self.p, q1=D("10"), q2=D("20"))
        PCCF.objects.create(project=self.p, submission=0, design_cost=D("100000"), construction_cost=D("400000"),
                            design_contingency=D("10000"), construction_contingency=D("40000"),
                            fee_percent=D("10"), fee=D("55000"), q1=D("1000"), q2=D("2000"))
        self.po1 = PurchaseOrder.objects.create(number=111, project=self.p, vendor=self.arch, amount=D("105000"))
        self.po2 = PurchaseOrder.objects.create(number=222, project=self.p, vendor=self.build, amount=D("380000"))


class FinancialsTests(Base):
    def test_budget_commitments_contingency(self):
        f = project_financials(self.p)
        self.assertEqual(f.budget, D("550000"))
        self.assertEqual(f.budget_with_fee, D("605000"))
        self.assertEqual(f.commitments, D("485000"))
        self.assertEqual(f.used_design_contingency, D("5000"))      # 105k committed vs 100k design budget
        self.assertEqual(f.used_construction_contingency, D("0"))
        self.assertEqual(f.remaining_budget, D("65000"))

    def test_only_approved_change_orders_count_as_pending(self):
        ChangeOrder.objects.create(po=self.po2, description="a", value=D("5000"), status="approved")
        ChangeOrder.objects.create(po=self.po2, description="b", value=D("7000"), status="requested")
        ChangeOrder.objects.create(po=self.po2, description="c", value=D("9000"), status="processed")
        self.assertEqual(project_financials(self.p).pending, D("5000"))

    def test_latest_pccf_and_vow_used(self):
        PCCF.objects.create(project=self.p, submission=1, design_cost=D("1"))
        ValueOfWork.objects.create(project=self.p, reporting_date="2024-01-01", total_no_fees=D("10"))
        ValueOfWork.objects.create(project=self.p, reporting_date="2024-06-01", total_no_fees=D("50"),
                                   previous_years_no_fees=D("20"))
        f = project_financials(self.p)
        self.assertEqual(f.pccf.submission, 1)
        self.assertEqual(f.vow_total, D("50"))
        self.assertEqual(f.vow_current_year, D("30"))


class ConstraintTests(Base):
    def test_pccf_submission_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PCCF.objects.create(project=self.p, submission=0)

    def test_project_number_unique(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Project.objects.create(io_number="1000001", title="dup")

    def test_identical_invoice_rejected(self):
        Invoice.objects.create(po=self.po1, number="A1", amount=D("10"), date="2024-01-01")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Invoice.objects.create(po=self.po1, number="A1", amount=D("10"), date="2024-01-01")

    def test_project_with_pos_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.p.delete()


class InvoiceFormTests(Base):
    def post(self, **extra):
        data = {"inv-number": "INV-1", "inv-amount": "1000", "inv-date": "2024-05-01"}
        data.update(extra)
        return self.client.post("/pos/111/", data, follow=True)

    def test_add_invoice(self):
        self.post()
        self.assertEqual(self.po1.invoices.count(), 1)

    def test_duplicate_number_needs_confirmation(self):
        self.post()
        r = self.post(**{"inv-amount": "500"})
        self.assertContains(r, "already recorded")
        self.assertEqual(self.po1.invoices.count(), 1)
        self.post(**{"inv-amount": "500", "inv-confirm_duplicate": "on"})
        self.assertEqual(self.po1.invoices.count(), 2)

    def test_over_po_is_blocked(self):
        r = self.post(**{"inv-amount": "200000"})
        self.assertContains(r, "Not enough budget for approval")
        self.assertContains(r, "PO 111 has only $105,000.00 left")
        self.post(**{"inv-amount": "200000", "inv-allow_overrun": "on"})   # old override no longer exists
        self.assertEqual(self.po1.invoices.count(), 0)
        self.post(**{"inv-amount": "105000"})                               # exactly what's left is fine
        self.assertEqual(self.po1.invoices.count(), 1)

    def test_project_budget_after_fees_is_a_limit(self):
        # Budget excl. fee is $550,000. A big PO can't be invoiced past that.
        big = PurchaseOrder.objects.create(number=333, project=self.p, vendor=self.build, amount=D("900000"))
        Invoice.objects.create(po=self.po2, number="X", amount=D("500000"), date="2024-01-01")
        r = self.client.post("/pos/333/", {"inv-number": "Y", "inv-amount": "60000", "inv-date": "2024-02-01"})
        self.assertContains(r, "project 1000001 has only $50,000.00 of budget left after fees")
        self.assertEqual(big.invoices.count(), 0)

    def test_credits_always_allowed(self):
        self.post(**{"inv-amount": "-500"})
        self.assertEqual(self.po1.invoices.count(), 1)

    def test_editing_doesnt_count_invoice_against_itself(self):
        inv = Invoice.objects.create(po=self.po1, number="FULL", amount=D("105000"), date="2024-01-01")
        r = self.client.post(f"/invoices/{inv.pk}/edit/", {"po": self.po1.pk, "number": "FULL", "amount": "105000",
                                                            "date": "2024-01-02"})
        self.assertEqual(r.status_code, 302)
        r = self.client.post(f"/invoices/{inv.pk}/edit/", {"po": self.po1.pk, "number": "FULL", "amount": "105001",
                                                            "date": "2024-01-02"})
        self.assertContains(r, "Not enough budget for approval")

    def test_room_shown_on_forms(self):
        r = self.client.get("/pos/111/")
        self.assertContains(r, 'id="room-data"')
        self.assertContains(r, 'class="room-msg"')
        r = self.client.get("/invoices/new/")
        self.assertEqual(r.context["rooms"][str(self.po1.pk)]["available"], 105000.0)


class ChangeOrderTests(Base):
    def test_process_adds_to_po_once(self):
        co = ChangeOrder.objects.create(po=self.po1, description="extra", value=D("2500"))
        self.client.post(f"/change-orders/{co.pk}/process/")
        self.client.post(f"/change-orders/{co.pk}/process/")  # second click must do nothing
        self.po1.refresh_from_db()
        co.refresh_from_db()
        self.assertEqual(self.po1.amount, D("107500"))
        self.assertEqual(co.status, "processed")

    def test_process_button_confirms_on_page_not_with_popup(self):
        ChangeOrder.objects.create(po=self.po1, description="extra", value=D("2500"))
        for url in ("/change-orders/", "/pos/111/"):
            r = self.client.get(url)
            self.assertContains(r, "data-confirm=")
            self.assertNotContains(r, "return confirm(")   # browser pop-ups are silently refused in the app pane

    def test_requested_cannot_be_processed(self):
        co = ChangeOrder.objects.create(po=self.po1, description="x", value=D("1"), status="requested")
        self.client.post(f"/change-orders/{co.pk}/process/")
        self.po1.refresh_from_db()
        self.assertEqual(self.po1.amount, D("105000"))


class MPSRTests(Base):
    def test_cash_flow_update_logs_history(self):
        self.client.post(f"/projects/{self.p.pk}/update/cash/", {
            "cash-fiscal_year": "2025", "cash-previous": "0", "cash-q1": "5", "cash-q2": "6", "cash-q3": "7",
            "cash-q4": "8", "cash-next_year": "9", "cash-future": "10"})
        st = ProjectStatus.objects.get(project=self.p)
        self.assertEqual(st.q4, D("8"))
        h = StatusHistory.objects.get(project=self.p, kind="cash_flow")
        self.assertEqual(h.data["q4"], 8.0)

    def test_comment_and_risk(self):
        self.client.post(f"/projects/{self.p.pk}/update/comment/", {"comment-text": "On track"})
        self.client.post(f"/projects/{self.p.pk}/update/risk/", {"risk-text": "Long lead items"})
        st = ProjectStatus.objects.get(project=self.p)
        self.assertEqual((st.comments, st.risk), ("On track", "Long lead items"))
        self.assertEqual(StatusHistory.objects.filter(project=self.p).count(), 2)

    def test_vow_strips_fee_from_previous_years(self):
        self.client.post(f"/projects/{self.p.pk}/update/vow/", {
            "vow-reporting_date": "2024-08-01", "vow-previous_years_with_fees": "110000",
            "vow-current_year_no_fees": "25000"})
        v = ValueOfWork.objects.get(project=self.p)
        self.assertEqual(v.previous_years_no_fees, D("100000.00"))
        self.assertEqual(v.total_no_fees, D("125000.00"))
        self.assertEqual(v.current_year_funding, D("3000"))  # approved Q1+Q2 from the PCCF


class PCCFTests(Base):
    def test_new_pccf_prefills_next_submission(self):
        r = self.client.get(f"/projects/{self.p.pk}/pccf/new/")
        self.assertEqual(r.context["title"], "1000001: new PCCF 1")
        self.assertEqual(r.context["form"].initial["design_cost"], D("100000"))

    def test_submission_number_is_generated_not_entered(self):
        r = self.client.get(f"/projects/{self.p.pk}/pccf/new/")
        self.assertNotIn("submission", r.context["form"].fields)
        self.assertContains(r, "new PCCF 1")
        zeros = {k: "0" for k in ("design_cost", "construction_cost", "design_contingency",
                                  "construction_contingency", "fee_percent", "fee", "previous", "q1", "q2", "q3",
                                  "q4", "next_year", "future")}
        self.client.post(f"/projects/{self.p.pk}/pccf/new/", {**zeros, "submission": "0"})   # a typed number is ignored
        self.client.post(f"/projects/{self.p.pk}/pccf/new/", zeros)
        self.assertEqual(list(self.p.pccfs.order_by("submission").values_list("submission", flat=True)), [0, 1, 2])
        pc = self.p.pccfs.get(submission=2)
        self.client.post(f"/projects/{self.p.pk}/pccf/{pc.pk}/", {**zeros, "submission": "9"})
        pc.refresh_from_db()
        self.assertEqual(pc.submission, 2)

    def test_fiscal_year_shown_as_range_and_saved_as_year_ending(self):
        pc = self.p.pccfs.get()
        pc.fiscal_year = 2025
        pc.save()
        r = self.client.get(f"/projects/{self.p.pk}/?tab=budget")
        self.assertContains(r, "2024/25")
        r = self.client.get(f"/projects/{self.p.pk}/pccf/{pc.pk}/")
        self.assertContains(r, '<option value="2025" selected>2024/25</option>', html=True)
        self.assertNotContains(r, "cat_a")

    def test_new_pccf_saves_notes(self):
        self.client.post(f"/projects/{self.p.pk}/pccf/new/", {
            "submission": "1", "notes": "Scope grew.", "fiscal_year": "2026", **{f: "0" for f in (
                "design_cost", "construction_cost", "design_contingency", "construction_contingency", "fee_percent",
                "fee", "previous", "q1", "q2", "q3", "q4", "next_year", "future")}})
        pc = self.p.pccfs.get(submission=1)
        self.assertEqual((pc.notes, pc.fiscal_year, pc.fiscal_year_label), ("Scope grew.", 2026, "2025/26"))


class InvoiceTabTests(Base):
    def setUp(self):
        super().setUp()
        Invoice.objects.create(po=self.po1, number="A-100", amount=D("1000"), date="2024-03-31")  # FY 2023/24
        Invoice.objects.create(po=self.po1, number="A-200", amount=D("2000"), date="2024-04-01")  # FY 2024/25
        Invoice.objects.create(po=self.po2, number="B-300", amount=D("4000"), date="2025-03-31")  # FY 2024/25

    def test_list_totals_and_fiscal_year_filter(self):
        r = self.client.get("/invoices/")
        self.assertEqual((r.context["count"], r.context["total"]), (3, D("7000")))
        r = self.client.get("/invoices/?fy=2025")
        self.assertEqual((r.context["count"], r.context["total"]), (2, D("6000")))
        self.assertContains(r, "FY 2024/25")
        self.assertEqual(len(r.context["months"]), 12)

    def test_search_and_date_range(self):
        r = self.client.get("/invoices/?q=Build Co")
        self.assertEqual(r.context["count"], 1)
        r = self.client.get("/invoices/?from=2024-04-01&to=2024-12-31")
        self.assertEqual(r.context["total"], D("2000"))

    def test_add_invoice_with_po_picker_and_checks(self):
        data = {"inv-po": self.po1.pk, "inv-number": "A-100", "inv-amount": "50", "inv-date": "2024-06-01"}
        r = self.client.post("/invoices/new/", data)
        self.assertContains(r, "already recorded")           # duplicate number on that PO
        data["inv-number"] = "A-999"
        self.client.post("/invoices/new/", data)
        self.assertTrue(Invoice.objects.filter(number="A-999", po=self.po1).exists())
        data.update({"inv-number": "A-1000", "inv-amount": "999999"})
        r = self.client.post("/invoices/new/", data)
        self.assertContains(r, "Not enough budget for approval")   # over the PO value

    def test_edit_from_list_returns_to_list(self):
        inv = Invoice.objects.get(number="B-300")
        r = self.client.post(f"/invoices/{inv.pk}/edit/?next=/invoices/%3Ffy%3D2025",
                             {"po": self.po2.pk, "number": "B-300", "amount": "4500", "date": "2025-03-31"})
        self.assertEqual(r["Location"], "/invoices/?fy=2025")
        r = self.client.post(f"/invoices/{inv.pk}/delete/?next=//evil.example")
        self.assertEqual(r["Location"], f"/pos/{self.po2.number}/")   # outside links are ignored


class CharterTests(Base):
    MONEY = ("design_cost", "design_contingency", "construction_cost", "construction_contingency", "fee_percent",
             "fee", "previous", "q1", "q2", "q3", "q4", "next_year", "future")

    def setUp(self):
        super().setUp()
        self.bldg = Building.objects.create(asset_number="B12345", name="Queen's Park", address="99 Wellesley St W",
                                            city="Toronto")

    def data(self, **charter):
        d = {"p-io_number": "3000003", "p-title": "New roof", "p-status": "1", "p-kind": "1",
             "p-building_asset": "B12345"}
        d.update({f"c-{k}": "0" for k in self.MONEY})
        d.update({f"c-{k}": v for k, v in charter.items()})
        return d

    def test_project_needs_a_charter_budget(self):
        r = self.client.post("/projects/new/", self.data())
        self.assertContains(r, "The charter needs an approved budget")
        self.assertFalse(Project.objects.filter(io_number="3000003").exists())

    def test_project_created_with_charter_and_seeded_forecast(self):
        r = self.client.post("/projects/new/", self.data(design_cost="10000", construction_cost="90000",
                                                         fee_percent="10", fee="10000", q1="2500", q2="57500",
                                                         next_year="50000", fiscal_year="2026",
                                                         substantial="2026-09-30"))
        p = Project.objects.get(io_number="3000003")
        self.assertRedirects(r, f"/projects/{p.pk}/")
        pc = p.pccfs.get()
        self.assertEqual((pc.submission, pc.total_budget), (0, D("110000")))
        self.assertEqual((p.current_status.q1, p.current_status.substantial, p.current_status.fiscal_year),
                         (D("2500"), dt.date(2026, 9, 30), 2026))

    def test_only_required_fields_and_one_amount(self):
        r = self.client.post("/projects/new/", {"p-io_number": "4000004", "p-title": "Minimal", "p-status": "1",
                                                "p-kind": "1", "p-building_asset": "b12345",
                                                "c-construction_cost": "25000", "c-future": "25000"})
        p = Project.objects.get(io_number="4000004")
        self.assertRedirects(r, f"/projects/{p.pk}/")
        self.assertEqual(p.building, self.bldg)                        # matched case-insensitively
        self.assertEqual((p.pccfs.get().total_budget, p.pccfs.get().fee), (D("25000"), D("0")))
        r = self.client.get("/projects/new/")
        self.assertContains(r, 'class="required">IO project #:</label>', html=False)
        self.assertContains(r, 'class="required">Building (asset #):</label>', html=False)

    def test_client_is_always_io(self):
        r = self.client.get("/projects/new/")
        self.assertNotIn("client", r.context["form"].fields)
        self.client.post("/projects/new/", self.data(design_cost="100", future="100") | {"p-client": "999"})
        p = Project.objects.get(io_number="3000003")
        self.assertEqual(p.client.name, "Infrastructure Ontario")
        self.assertEqual(Company.objects.filter(name="Infrastructure Ontario").count(), 1)

    def test_building_required_and_must_exist(self):
        r = self.client.post("/projects/new/", self.data(design_cost="1000", **{}) | {"p-building_asset": ""})
        self.assertFalse(Project.objects.filter(io_number="3000003").exists())
        r = self.client.post("/projects/new/", self.data(design_cost="1000") | {"p-building_asset": "B99999"})
        self.assertFalse(Project.objects.filter(io_number="3000003").exists())
        self.assertContains(r, 'href="/facilities/buildings/new/?asset=B99999"')   # a real link, not escaped text
        r = self.client.get("/facilities/buildings/new/?asset=B99999")
        self.assertEqual(r.context["form"].initial["asset_number"], "B99999")

    def test_building_search(self):
        r = self.client.get("/facilities/buildings/search/?q=wellesley").json()
        self.assertEqual(r["results"][0]["asset"], "B12345")
        r = self.client.get("/facilities/buildings/search/?q=B12345").json()
        self.assertTrue(r["exact"])
        r = self.client.get("/facilities/buildings/search/?q=B999").json()
        self.assertEqual((r["results"], r["exact"]), ([], False))

    def test_older_project_with_unlisted_building_can_still_be_edited(self):
        old = Project.objects.create(io_number="OLD2", title="Old", building_code="B00016")
        r = self.client.post(f"/projects/{old.pk}/edit/", {"io_number": "OLD2", "title": "Old renamed", "status": "4",
                                                           "kind": "1", "building_asset": "B00016"})
        self.assertEqual(r.status_code, 302)
        old.refresh_from_db()
        self.assertEqual((old.title, old.building_code), ("Old renamed", "B00016"))

    def test_charter_cannot_be_deleted_or_renumbered(self):
        charter = self.p.pccfs.get(submission=0)
        self.client.post(f"/projects/{self.p.pk}/pccf/{charter.pk}/delete/")
        self.assertTrue(PCCF.objects.filter(pk=charter.pk).exists())
        r = self.client.get(f"/projects/{self.p.pk}/pccf/{charter.pk}/delete/")
        self.assertContains(r, "PCCF 0 is the project&#x27;s charter")
        self.assertNotContains(r, "Delete permanently")
        r = self.client.post(f"/projects/{self.p.pk}/pccf/{charter.pk}/", {
            "submission": "5", **{k: "0" for k in self.MONEY}, "design_cost": "1", "future": "1"})
        self.assertEqual(r.status_code, 302)   # saved (balanced), but the number stays 0
        charter.refresh_from_db()
        self.assertEqual(charter.submission, 0)

    def test_charter_cash_flow_must_equal_budget(self):
        r = self.client.post("/projects/new/", self.data(design_cost="10000", fee_percent="10", q1="10000"))
        self.assertContains(r, "cash flow adds up to $10,000.00 but the total budget (incl. fee) is $11,000.00")
        self.assertContains(r, "short by $1,000.00")
        self.assertFalse(Project.objects.filter(io_number="3000003").exists())
        # within $1 of rounding is accepted
        self.client.post("/projects/new/", self.data(design_cost="10000", fee_percent="10", q1="10999.40"))
        self.assertTrue(Project.objects.filter(io_number="3000003").exists())

    def test_new_and_edited_pccf_must_balance(self):
        base = {k: "0" for k in self.MONEY}
        r = self.client.post(f"/projects/{self.p.pk}/pccf/new/", {**base, "submission": "1", "construction_cost": "500",
                                                                  "q1": "400", "q2": "200"})
        self.assertContains(r, "over by $100.00")
        self.assertFalse(self.p.pccfs.filter(submission=1).exists())
        self.client.post(f"/projects/{self.p.pk}/pccf/new/", {**base, "submission": "1", "construction_cost": "500",
                                                              "q1": "300", "q2": "200"})
        pc = self.p.pccfs.get(submission=1)
        r = self.client.post(f"/projects/{self.p.pk}/pccf/{pc.pk}/", {**base, "submission": "1",
                                                                      "construction_cost": "900", "q1": "500"})
        self.assertContains(r, "short by $400.00")
        pc.refresh_from_db()
        self.assertEqual(pc.construction_cost, D("500"))

    def test_schedule_dates_must_be_in_order(self):
        base = {**{k: "0" for k in self.MONEY}, "design_cost": "100", "future": "100"}
        r = self.client.post(f"/projects/{self.p.pk}/pccf/new/", {
            **base, "approval_date": "2025-01-10", "design_start": "2025-03-01", "design_end": "2025-02-01"})
        self.assertContains(r, "Design end (2025-02-01) is before Design start (2025-03-01)")
        self.assertFalse(self.p.pccfs.filter(submission=1).exists())
        # blanks are skipped: closeout is compared with the last filled date before it
        r = self.client.post(f"/projects/{self.p.pk}/pccf/new/", {
            **base, "approval_date": "2025-01-10", "substantial": "2026-06-01", "closeout": "2026-05-01"})
        self.assertContains(r, "Closeout (2026-05-01) is before Substantial performance (2026-06-01)")
        # equal dates are fine, and an ordered schedule saves
        self.client.post(f"/projects/{self.p.pk}/pccf/new/", {
            **base, "approval_date": "2025-01-10", "design_start": "2025-01-10", "design_end": "2025-06-01",
            "closeout": "2027-01-01"})
        self.assertTrue(self.p.pccfs.filter(submission=1).exists())

    def test_charter_dates_must_be_in_order(self):
        r = self.client.post("/projects/new/", self.data(design_cost="100", future="100",
                                                         design_start="2026-05-01", design_end="2026-04-01"))
        self.assertContains(r, "Design end (2026-04-01) is before Design start (2026-05-01)")
        self.assertFalse(Project.objects.filter(io_number="3000003").exists())

    def test_out_of_order_flag_clears_when_fixed(self):
        from .models import DataIssue
        from .services import flag_out_of_order_schedules
        pc = self.p.pccfs.get()
        pc.design_start, pc.design_end = dt.date(2025, 5, 1), dt.date(2025, 4, 1)
        pc.save()
        self.assertEqual(flag_out_of_order_schedules(), 1)
        self.assertEqual(flag_out_of_order_schedules(), 0)   # not flagged twice
        fields = {f: str(getattr(pc, f)) for f in self.MONEY}
        fields.update(design_start="2025-03-01", design_end="2025-04-01",
                      future=str(pc.total_budget - pc.q1 - pc.q2), q1=str(pc.q1), q2=str(pc.q2))
        self.client.post(f"/projects/{self.p.pk}/pccf/{pc.pk}/", fields)
        self.assertFalse(DataIssue.objects.filter(category="Schedule dates out of order", resolved=False).exists())

    def test_fee_is_calculated_not_typed(self):
        r = self.client.post("/projects/new/", self.data(design_cost="10,000.00", construction_cost="$90,000",
                                                         fee_percent="7.38", fee="999999", future="107,380.00"))
        p = Project.objects.get(io_number="3000003")
        self.assertRedirects(r, f"/projects/{p.pk}/")
        pc = p.pccfs.get()
        self.assertEqual((pc.fee, pc.total_budget), (D("7380.00"), D("107380.00")))   # typed fee ignored
        r = self.client.get(f"/projects/{p.pk}/pccf/{pc.pk}/")
        self.assertContains(r, 'value="7,380.00"')
        self.assertContains(r, "readonly")
        self.assertContains(r, 'value="90,000.00"')   # amounts shown in financial format

    def test_money_field_parsing(self):
        from .forms import MoneyField
        m = MoneyField(max_digits=14, decimal_places=2)
        self.assertEqual([m.clean(v) for v in ("$1,234.56", "1234.5", "(1,000.00)", " 2 500 ")],
                         [D("1234.56"), D("1234.5"), D("-1000.00"), D("2500")])

    def test_later_pccf_can_be_deleted(self):
        pc1 = PCCF.objects.create(project=self.p, submission=1)
        self.client.post(f"/projects/{self.p.pk}/pccf/{pc1.pk}/delete/")
        self.assertFalse(PCCF.objects.filter(pk=pc1.pk).exists())

    def test_older_project_without_charter_gets_prompt(self):
        old = Project.objects.create(io_number="OLD1", title="Old")
        PCCF.objects.create(project=old, submission=3)
        r = self.client.get(f"/projects/{old.pk}/?tab=budget")
        self.assertContains(r, "Add the charter")
        r = self.client.get(f"/projects/{old.pk}/pccf/new/?charter=1")
        self.assertEqual(r.context["title"], "OLD1: new charter (PCCF 0)")
        self.client.post(f"/projects/{old.pk}/pccf/new/?charter=1", {"design_cost": "10", "future": "10"})
        self.assertEqual(sorted(old.pccfs.values_list("submission", flat=True)), [0, 3])


class FiscalYearDefaultTests(Base):
    def test_current_fiscal_year(self):
        from .textutil import current_fiscal_year
        self.assertEqual(current_fiscal_year(dt.date(2026, 11, 8)), 2027)   # Nov 2026 is in 2026/27
        self.assertEqual(current_fiscal_year(dt.date(2026, 3, 31)), 2026)   # Mar 2026 is in 2025/26
        self.assertEqual(current_fiscal_year(dt.date(2026, 4, 1)), 2027)

    def test_new_forms_default_to_current_fiscal_year(self):
        from .textutil import current_fiscal_year
        now = current_fiscal_year()
        pc = self.p.pccfs.get()
        pc.fiscal_year = 2019
        pc.save()
        r = self.client.get(f"/projects/{self.p.pk}/pccf/new/")      # not copied from the previous PCCF
        self.assertEqual(r.context["form"].initial["fiscal_year"], now)
        r = self.client.get("/projects/new/")
        self.assertEqual(r.context["charter"].initial["fiscal_year"], now)
        r = self.client.get(f"/projects/{self.p.pk}/pccf/{pc.pk}/")  # an existing PCCF keeps its own year
        self.assertNotEqual(r.context["form"].initial.get("fiscal_year"), now)
        self.assertContains(r, '<option value="2019" selected>2018/19</option>', html=True)


class OwnerTests(TestCase):
    def test_first_run_goes_to_setup_then_tracker_is_yours(self):
        r = self.client.get("/")
        self.assertRedirects(r, "/setup/", fetch_redirect_response=False)
        r = self.client.post("/setup/", {"me-first_name": "Jo", "me-last_name": "Smith",
                                         "me-email": "jo.smith@bgis.com", "me-position": "Project Manager"})
        self.assertRedirects(r, "/setup/?step=import", fetch_redirect_response=False)
        from .models import Owner
        me = Owner.current().person
        self.assertEqual((str(me), me.company.name), ("Jo Smith", "BGIS"))
        self.assertEqual(self.client.get("/").status_code, 200)
        r = self.client.post("/setup/?step=import", {"skip": "1"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)

    def test_projects_belong_to_the_owner(self):
        from .operations import set_owner
        set_owner("Jo", "Smith")
        b = Building.objects.create(asset_number="B1")
        self.client.post("/projects/new/", {"p-io_number": "5", "p-title": "T", "p-status": "1", "p-kind": "1",
                                            "p-building_asset": "B1", "c-design_cost": "1", "c-future": "1"})
        p = Project.objects.get(io_number="5")
        self.assertEqual(str(p.project_manager), "Jo Smith")
        self.assertNotIn("project_manager", self.client.get("/projects/new/").context["form"].fields)
        # renaming yourself keeps every project yours
        self.client.post("/settings/", {"save_profile": "1", "me-first_name": "Joanne", "me-last_name": "Smith"})
        p.refresh_from_db()
        self.assertEqual(str(p.project_manager), "Joanne Smith")

    def test_setup_reuses_existing_bgis_person_and_merges_duplicates(self):
        from .operations import merge_duplicate_people, set_owner
        bgis = Company.objects.create(name="BGIS")
        a = Person.objects.create(first_name="Sam", last_name="Rivera", company=bgis, email="►")
        b = Person.objects.create(first_name="Sam ", last_name="rivera", company=bgis, email="m@bgis.com")
        p1 = Project.objects.create(io_number="1", title="x", project_manager=a)
        p2 = Project.objects.create(io_number="2", title="y", project_manager=b)
        kept, n = merge_duplicate_people(bgis)[0]
        self.assertEqual((n, kept.email, Person.objects.filter(company=bgis).count()), (1, "m@bgis.com", 1))
        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertEqual(p1.project_manager, p2.project_manager)
        owner = set_owner("Sam", "Rivera")
        self.assertEqual(owner.person, kept)                     # no new duplicate created
        r = self.client.post(f"/people/new/", {"first_name": "SAM", "last_name": "Rivera",
                                               "company": bgis.pk})
        self.assertContains(r, "is already listed at BGIS")

    def test_setup_keeps_details_already_on_file(self):
        bgis = Company.objects.create(name="BGIS")
        Person.objects.create(first_name="Ana", last_name="Lee", company=bgis, email="ana@bgis.com", phone="416")
        self.client.post("/setup/", {"me-first_name": "Ana", "me-last_name": "Lee", "me-position": "Project Manager"})
        p = Person.objects.get(last_name="Lee")
        self.assertEqual((p.email, p.phone), ("ana@bgis.com", "416"))   # blank boxes didn't erase them

    def test_settings_import_needs_existing_file_and_confirmation(self):
        from .operations import set_owner
        set_owner("Jo", "Smith")
        Project.objects.create(io_number="9", title="x")
        r = self.client.post("/settings/", {"run_import": "1", "imp-source": r"C:\nope\MBN.accdb",
                                            "imp-only_mine": "on"})
        self.assertContains(r, "That file doesn")
        self.assertContains(r, "Type REPLACE")


class MPSRTests(Base):
    def setUp(self):
        super().setUp()
        self.p2 = Project.objects.create(io_number="1000002", title="Second")
        ProjectStatus.objects.create(project=self.p2)

    def post_data(self, r, **changes):
        """Everything the MPSR page shows, as it would be sent back, with some changes."""
        data = {}
        for prefix, form in r.context["f"].items():
            for name, field in form.fields.items():
                key = f"{prefix}-{name}"
                val = form[name].value()
                if isinstance(val, bool):
                    if val:
                        data[key] = "on"
                elif val is not None:
                    data[key] = str(val)
        data.update(changes)
        return data

    def test_list_and_progress(self):
        r = self.client.get("/mpsr/")
        self.assertEqual((r.context["done_count"], r.context["total"]), (0, 2))
        self.assertContains(r, "Start MPSR")

    def test_save_next_records_comment_and_moves_on(self):
        from .models import MPSRReview
        r = self.client.get(f"/mpsr/{self.p.pk}/")
        r = self.client.post(f"/mpsr/{self.p.pk}/", self.post_data(r, **{
            "comment-text": "Design 60% complete.", "risk-text": "Long lead items.", "save_next": "1"}))
        self.assertRedirects(r, f"/mpsr/{self.p2.pk}/?period={dt.date.today():%Y-%m}", fetch_redirect_response=False)
        st = ProjectStatus.objects.get(project=self.p)
        self.assertEqual((st.comments, st.risk), ("Design 60% complete.", "Long lead items."))
        self.assertEqual(set(self.p.status_history.values_list("kind", flat=True)), {"comment", "risk"})
        self.assertTrue(MPSRReview.objects.filter(project=self.p).exists())
        self.assertEqual(self.client.get("/mpsr/").context["done_count"], 1)

    def test_saving_without_changes_marks_done_but_logs_nothing(self):
        from .models import MPSRReview
        ValueOfWork.objects.create(project=self.p, reporting_date="2026-01-01", total_no_fees=D("1000"),
                                   previous_years_no_fees=D("400"))
        r = self.client.get(f"/mpsr/{self.p.pk}/")
        self.client.post(f"/mpsr/{self.p.pk}/", self.post_data(r, save="1"))
        self.assertTrue(MPSRReview.objects.filter(project=self.p).exists())
        self.assertEqual(self.p.status_history.count(), 0)
        self.assertEqual(self.p.vow_entries.count(), 1)     # unchanged VOW isn't recorded again

    def test_forecast_change_is_logged(self):
        r = self.client.get(f"/mpsr/{self.p.pk}/")
        self.client.post(f"/mpsr/{self.p.pk}/", self.post_data(r, **{"cash-q3": "12,345.00", "save": "1"}))
        self.assertEqual(ProjectStatus.objects.get(project=self.p).q3, D("12345.00"))
        self.assertTrue(self.p.status_history.filter(kind="cash_flow").exists())


class BudgetBarTests(Base):
    def test_segments_fill_budget_and_overrun_is_separate(self):
        f = project_financials(self.p)        # budget 550,000; committed 485,000; nothing invoiced
        self.assertEqual((round(f.bar_committed, 1), f.bar_over), (88.2, 0))
        self.assertEqual(round(f.committed_pct), 88)
        PurchaseOrder.objects.create(number=333, project=self.p, vendor=self.build, amount=D("165000"))
        f = project_financials(self.p)        # committed 650,000 -> 100,000 over
        self.assertEqual(f.over_amount, D("100000"))
        self.assertAlmostEqual(f.bar_committed + f.bar_over, 100, places=5)
        r = self.client.get("/projects/")
        self.assertContains(r, "over by $100,000")


class CombineNotesTests(TestCase):
    def test_drops_no_change_and_repeated_sentences(self):
        from .textutil import combine_notes
        scope = "No change to scope. This change is to request funding. The load bank moves outside."
        budget = "This change is to request funding. The load bank moves outside. Cash flow adjusted accordingly."
        out = combine_notes([("Scope", scope), ("Methodology", "No change"), ("Milestones", " no change. "),
                             ("Budget", budget)])
        self.assertEqual(out, f"Scope: {scope}\n\nBudget: Cash flow adjusted accordingly.")

    def test_single_part_has_no_label(self):
        from .textutil import combine_notes
        self.assertEqual(combine_notes([("Scope", "Only this."), ("Budget", "Only this.")]), "Only this.")


class CalendarTests(Base):
    def setUp(self):
        super().setUp()
        st = self.p.current_status
        st.design_end = dt.date(2020, 1, 15)                  # past, no actual -> overdue
        st.substantial = dt.date(2099, 6, 1)                  # future -> upcoming
        st.closeout, st.actual_closeout = dt.date(2020, 3, 1), dt.date(2020, 3, 5)  # done
        st.save()
        pc = self.p.pccfs.get()
        pc.construction_award = dt.date(2021, 2, 1)           # no forecast -> falls back to approved date
        pc.save()

    def test_milestone_states(self):
        today = dt.date(2026, 1, 1)
        ev = {e.key: e for e in milestones([self.p])}
        self.assertEqual(ev["design"].state(today), "missed")
        self.assertTrue(ev["design"].overdue(today))
        self.assertEqual(ev["substantial"].state(today), "upcoming")
        self.assertEqual(ev["closeout"].state(today), "done")
        self.assertEqual(ev["closeout"].date, dt.date(2020, 3, 5))      # actual wins
        self.assertEqual(ev["award"].date, dt.date(2021, 2, 1))         # approved fallback

    def test_finished_projects_show_missed_but_not_overdue(self):
        self.p.status = Project.Status.COMPLETED
        self.p.save()
        ev = {e.key: e for e in milestones([self.p])}
        self.assertEqual(ev["design"].state(dt.date(2026, 1, 1)), "missed")   # red ✗ on the calendar
        self.assertFalse(ev["design"].overdue(dt.date(2026, 1, 1)))          # but not counted as overdue

    def test_calendar_marks_past_events(self):
        self.p.status = Project.Status.COMPLETED   # past events of finished projects still shown by default
        self.p.save()
        r = self.client.get("/calendar/?m=2020-01")
        self.assertContains(r, 'class="chip k-design missed"')
        self.assertContains(r, "✗</span>")
        r = self.client.get("/calendar/?m=2020-03")
        self.assertContains(r, 'class="chip k-closeout done"')
        self.assertContains(r, "✓</span>")

    def test_calendar_month_shows_event(self):
        r = self.client.get("/calendar/?m=2099-06")
        self.assertContains(r, "1000001")
        self.assertContains(r, "Substantial performance")
        r = self.client.get("/calendar/?m=2099-06&type=closeout")
        self.assertNotContains(r, 'class="chip k-substantial')

    def test_overview_and_lists_render(self):
        for url in ("/", "/projects/", "/projects/?show=all", "/pos/", "/change-orders/", "/issues/",
                    f"/projects/{self.p.pk}/", f"/projects/{self.p.pk}/?tab=budget",
                    f"/projects/{self.p.pk}/?tab=pos", f"/projects/{self.p.pk}/?tab=history"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_exporter_removed(self):
        self.assertEqual(self.client.get("/export/mpsr.xlsx").status_code, 404)


class EditDeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.inv = Invoice.objects.create(po=self.po1, number="I-1", amount=D("1000"), date="2024-01-01")
        ValueOfWork.objects.create(project=self.p, reporting_date="2024-01-01", total_no_fees=D("5"))

    def test_delete_project_needs_typed_confirmation_and_removes_everything(self):
        r = self.client.post(f"/projects/{self.p.pk}/delete/", {"confirm": "wrong"})
        self.assertContains(r, "Type 1000001")
        self.assertTrue(Project.objects.filter(pk=self.p.pk).exists())
        self.client.post(f"/projects/{self.p.pk}/delete/", {"confirm": "1000001"})
        self.assertFalse(Project.objects.filter(pk=self.p.pk).exists())
        for model in (PurchaseOrder, Invoice, PCCF, ValueOfWork, ProjectStatus):
            self.assertEqual(model.objects.count(), 0, model.__name__)
        self.assertTrue(Company.objects.filter(pk__in=[self.arch.pk, self.build.pk]).count() == 2)  # vendors stay

    def test_renumber_po_keeps_invoices_and_moves_with_project(self):
        other = Project.objects.create(io_number="2000002", title="Other")
        self.client.post("/pos/111/edit/", {"number": "999", "project": other.pk, "vendor": self.arch.pk,
                                            "amount": "105000"})
        po = PurchaseOrder.objects.get(number=999)
        self.assertEqual(po.invoices.count(), 1)
        self.assertEqual(project_financials(other).invoiced, D("1000"))
        self.assertEqual(project_financials(self.p).invoiced, D("0"))

    def test_edit_invoice_updates_totals(self):
        self.client.post(f"/invoices/{self.inv.pk}/edit/", {"po": self.po2.pk, "number": "I-1", "amount": "2500",
                                                             "date": "2024-02-01"})
        self.assertEqual(self.po2.invoiced(), D("2500"))
        self.assertEqual(self.po1.invoiced(), D("0"))

    def test_processed_change_order_edit_and_delete_adjust_po(self):
        co = ChangeOrder.objects.create(po=self.po1, description="x", value=D("1000"))
        self.client.post(f"/change-orders/{co.pk}/process/")
        self.po1.refresh_from_db()
        self.assertEqual(self.po1.amount, D("106000"))
        self.client.post(f"/change-orders/{co.pk}/edit/", {"description": "x", "value": "1500", "status": "processed"})
        self.po1.refresh_from_db()
        self.assertEqual(self.po1.amount, D("106500"))
        self.client.post(f"/change-orders/{co.pk}/delete/", {"reverse": "on"})
        self.po1.refresh_from_db()
        self.assertEqual(self.po1.amount, D("105000"))

    def test_vendor_with_pos_cannot_be_deleted_but_can_be_merged(self):
        self.client.post(f"/vendors/{self.arch.pk}/delete/")
        self.assertTrue(Company.objects.filter(pk=self.arch.pk).exists())
        self.client.post(f"/vendors/{self.arch.pk}/merge/", {"m-target": self.build.pk})
        self.assertFalse(Company.objects.filter(pk=self.arch.pk).exists())
        self.po1.refresh_from_db()
        self.assertEqual(self.po1.vendor, self.build)

    def test_unused_vendor_deleted_with_contacts(self):
        c = Company.objects.create(name="Spare Co")
        Person.objects.create(first_name="A", last_name="B", company=c)
        self.client.post(f"/vendors/{c.pk}/delete/")
        self.assertFalse(Company.objects.filter(pk=c.pk).exists())
        self.assertFalse(Person.objects.filter(last_name="B").exists())

    def test_building_rename_follows_and_delete_keeps_code(self):
        b = Building.objects.create(asset_number="B1", name="HQ")
        self.p.building = b
        self.p.save()
        self.client.post(f"/facilities/buildings/{b.pk}/edit/", {"asset_number": "B2", "name": "HQ"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.building.asset_number, "B2")
        self.client.post(f"/facilities/buildings/{b.pk}/delete/")
        self.p.refresh_from_db()
        self.assertIsNone(self.p.building)
        self.assertEqual(self.p.building_code, "B2")

    def test_new_building_links_projects_that_referenced_it(self):
        self.p.building_code = "B77"
        self.p.save()
        self.client.post("/facilities/buildings/new/", {"asset_number": "B77", "name": "New"})
        self.p.refresh_from_db()
        self.assertEqual(self.p.building.asset_number, "B77")

    def test_lists_page_removed(self):
        self.assertEqual(self.client.get("/lists/").status_code, 404)

    def test_new_pages_render(self):
        b = Building.objects.create(asset_number="B9")
        prop = Property.objects.create(number="P1")
        for url in ("/vendors/", "/vendors/?sort=name&type=1", f"/vendors/{self.arch.pk}/", "/vendors/new/",
                    "/facilities/", "/facilities/?q=B&only=projects", f"/facilities/buildings/{b.pk}/",
                    "/facilities/properties/", f"/facilities/properties/{prop.pk}/",
                    f"/invoices/{self.inv.pk}/edit/", f"/projects/{self.p.pk}/delete/", "/pos/111/delete/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class HostingTests(TestCase):
    def test_password_gate(self):
        from django.test import override_settings
        with override_settings(SITE_PASSWORD="open sesame"):
            r = self.client.get("/projects/")
            self.assertRedirects(r, "/login/?next=/projects/", fetch_redirect_response=False)
            self.assertEqual(self.client.get("/admin/").status_code, 302)          # admin is behind it too
            r = self.client.post("/login/", {"password": "wrong", "next": "/projects/"})
            self.assertEqual(r.status_code, 401)
            r = self.client.post("/login/", {"password": "open sesame", "next": "https://evil.example/"})
            self.assertRedirects(r, "/", fetch_redirect_response=False)            # no redirect to other sites
            self.assertRedirects(self.client.get("/projects/"), "/setup/",     # past the gate (no PM set up yet)
                                 fetch_redirect_response=False)
            self.client.get("/logout/")
            self.assertEqual(self.client.get("/projects/").status_code, 302)

    def test_no_password_when_not_configured(self):
        from django.test import override_settings
        with override_settings(SITE_PASSWORD=""):
            self.assertEqual(self.client.get("/login/").status_code, 302)
            self.assertRedirects(self.client.get("/"), "/setup/", fetch_redirect_response=False)

    def test_seed_demo_loads_consistent_data(self):
        from django.core.management import call_command
        from .models import Owner
        from io import StringIO
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Project.objects.count(), 10)
        self.assertEqual(str(Owner.current()), "Alex Morgan")
        for p in Project.objects.all():
            c = p.pccfs.get(submission=0)
            self.assertEqual(c.previous + c.q1 + c.q2 + c.q3 + c.q4 + c.next_year + c.future, c.total_budget, p)
            self.assertLessEqual(p.invoiced(), p.commitments(), p)
        call_command("seed_demo", stdout=StringIO())
        self.assertEqual(Project.objects.count(), 10)                              # second run changes nothing
        for url in ("/", "/projects/", "/mpsr/", "/calendar/", "/pos/", "/invoices/", "/change-orders/",
                    "/vendors/", "/facilities/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class TenderMilestoneTests(Base):
    def _status(self, **kw):
        st, _ = ProjectStatus.objects.get_or_create(project=self.p)
        for k, v in kw.items():
            setattr(st, k, v)
        st.save()
        return st

    def _tender(self):
        fresh = Project.objects.get(pk=self.p.pk)        # reload, as each page request does
        return next((e for e in milestones([fresh]) if e.key == "tender"), None)

    def test_no_tender_until_design_actually_ends(self):
        self._status(design_end=dt.date(2026, 5, 1))                # forecast only
        self.assertIsNone(self._tender())

    def test_deadline_is_30_days_after_actual_design_end_and_follows_changes(self):
        st = self._status(actual_design_end=dt.date(2026, 3, 10))
        self.assertEqual(self._tender().forecast, dt.date(2026, 4, 9))
        st.actual_design_end = dt.date(2026, 4, 1)                  # design end corrected later
        st.save()
        self.assertEqual(self._tender().forecast, dt.date(2026, 5, 1))
        r = self.client.post(f"/projects/{self.p.pk}/update/act/", {
            "act-actual_design_end": "2026-04-20", "act-stage": ""})
        self.assertEqual(self._tender().forecast, dt.date(2026, 5, 20))

    def test_open_tender_past_deadline_is_overdue(self):
        from django.utils import timezone
        today = timezone.localdate()
        self._status(actual_design_end=today - dt.timedelta(days=45))
        e = self._tender()
        self.assertEqual((e.state(today), e.overdue(today), e.late_days), ("missed", True, 15))

    def test_tick_and_untick(self):
        from django.utils import timezone
        today = timezone.localdate()
        self._status(actual_design_end=today - dt.timedelta(days=5))
        r = self.client.post(f"/projects/{self.p.pk}/tender/", {"action": "done", "next": "/calendar/"})
        self.assertRedirects(r, "/calendar/", fetch_redirect_response=False)
        e = self._tender()
        self.assertEqual((e.done, e.actual, e.state(today)), (True, today, "done"))
        self.assertTrue(self.p.status_history.filter(text__startswith="Tender issued").exists())
        self.client.post(f"/projects/{self.p.pk}/tender/", {"action": "undo"})
        self.assertFalse(self._tender().done)

    def test_cannot_tick_without_design_end_or_redirect_offsite(self):
        self._status()
        r = self.client.post(f"/projects/{self.p.pk}/tender/", {"action": "done", "next": "https://evil.example/"})
        self.assertRedirects(r, f"/projects/{self.p.pk}/?tab=mpsr", fetch_redirect_response=False)
        self.assertIsNone(ProjectStatus.objects.get(project=self.p).actual_tender)

    def test_award_means_tender_happened(self):
        from django.utils import timezone
        today = timezone.localdate()
        self._status(actual_design_end=today - dt.timedelta(days=200),
                     actual_construction_award=today - dt.timedelta(days=100))
        e = self._tender()
        self.assertEqual((e.done, e.overdue(today), e.late_days), (True, False, None))
        self.assertContains(self.client.get(f"/projects/{self.p.pk}/"), "done (construction awarded)")

    def test_tender_date_must_follow_design_end(self):
        from .forms import ActualsForm
        st = self._status()
        f = ActualsForm({"actual_tender": "2026-03-01"}, instance=st)
        self.assertIn("actual_tender", f.errors)
        f = ActualsForm({"actual_design_end": "2026-03-10", "actual_tender": "2026-03-01"}, instance=st)
        self.assertIn("before the actual design end", str(f.errors))
        f = ActualsForm({"actual_design_end": "2026-03-10", "actual_tender": "2026-03-20"}, instance=st)
        self.assertTrue(f.is_valid(), f.errors)

    def test_pages_show_tender(self):
        from django.utils import timezone
        self._status(actual_design_end=timezone.localdate() - dt.timedelta(days=3))
        for url in (f"/projects/{self.p.pk}/", f"/mpsr/{self.p.pk}/", "/calendar/?type=tender", "/"):
            r = self.client.get(url)
            self.assertContains(r, "Go to tender", msg_prefix=url)
            self.assertContains(r, "Mark tendered", msg_prefix=url)


class IONPOTests(Base):
    def test_po_label(self):
        from .models import po_label
        self.assertEqual([po_label(x) for x in (99900777, "9990045", " ion00123 ", 240001, "999")],
                         ["ION00777", "ION0045", "ION00123", "240001", "999"])

    def test_new_po_typed_with_999_is_saved_as_ion_and_pages_work(self):
        r = self.client.post("/pos/new/", {"number": "99900555", "project": self.p.pk, "vendor": self.arch.pk,
                                           "amount": "1000"})
        po = PurchaseOrder.objects.get(number="ION00555")
        self.assertRedirects(r, "/pos/ION00555/", fetch_redirect_response=False)
        for url in ("/pos/ION00555/", "/pos/ION00555/edit/", "/pos/ION00555/delete/", "/pos/?q=ION",
                    "/invoices/new/?po=ION00555"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.post("/pos/new/", {"number": "ION00555", "project": self.p.pk, "vendor": self.arch.pk,
                                           "amount": "1"})
        self.assertContains(r, "PO ION00555 already exists")
        self.assertEqual(PurchaseOrder.objects.filter(number="ION00555").count(), 1)
        self.assertEqual(po.vendor, self.arch)


class TenderConfirmTests(Base):
    def setUp(self):
        super().setUp()
        from django.utils import timezone
        self.today = timezone.localdate()
        self.st, _ = ProjectStatus.objects.get_or_create(project=self.p)
        self.st.actual_design_end = self.today - dt.timedelta(days=20)
        self.st.save()

    def test_confirm_box_data_and_chosen_date(self):
        r = self.client.get(f"/projects/{self.p.pk}/")
        self.assertContains(r, 'data-tender-confirm="')
        self.assertContains(r, f'data-min="{self.st.actual_design_end:%Y-%m-%d}"')
        self.assertContains(r, 'id="tender-confirm"')
        picked = self.today - dt.timedelta(days=4)
        r = self.client.post(f"/projects/{self.p.pk}/tender/", {"action": "done", "date": picked.isoformat()},
                             follow=True)
        self.st.refresh_from_db()
        self.assertEqual(self.st.actual_tender, picked)
        self.assertContains(r, f"tender marked as issued {picked}")
        self.assertContains(r, f'action="/projects/{self.p.pk}/tender/" class="msg-undo"')   # Undo next to message

    def test_undo_from_message(self):
        self.client.post(f"/projects/{self.p.pk}/tender/", {"action": "done"}, follow=True)   # message shown
        r = self.client.post(f"/projects/{self.p.pk}/tender/", {"action": "undo", "next": "/calendar/"}, follow=True)
        self.st.refresh_from_db()
        self.assertIsNone(self.st.actual_tender)
        self.assertContains(r, "tender marked as not issued")
        self.assertNotContains(r, 'class="msg-undo"')

    def test_bad_dates_are_refused(self):
        for bad in ((self.today + dt.timedelta(days=1)).isoformat(),
                    (self.st.actual_design_end - dt.timedelta(days=1)).isoformat(), "not-a-date"):
            r = self.client.post(f"/projects/{self.p.pk}/tender/", {"action": "done", "date": bad}, follow=True)
            self.assertContains(r, "Not saved", msg_prefix=bad)
            self.st.refresh_from_db()
            self.assertIsNone(self.st.actual_tender, bad)


class TenderInConstructionTests(Base):
    _status = TenderMilestoneTests._status
    _tender = TenderMilestoneTests._tender

    def setUp(self):
        super().setUp()
        from .models import StageGate
        self.design = StageGate.objects.create(name="99% Design", order=4)
        self.constr = StageGate.objects.create(name="Construction", order=6)
        self.close = StageGate.objects.create(name="Closeout", order=7)
        self._status(actual_design_end=dt.date(2026, 1, 10))

    def test_construction_stage_or_later_completes_tender(self):
        self._status(stage=self.design)
        self.assertFalse(self._tender().done)
        for gate in (self.constr, self.close):
            self._status(stage=gate)
            t = self._tender()
            self.assertTrue(t.done, gate.name)
            self.assertIsNone(t.late_days)
            self.assertIsNone(t.actual)                  # nothing invented: no tender date is recorded
        self._status(stage=self.design)                  # moved back: open again
        self.assertFalse(self._tender().done)

    def test_construction_phase_completes_tender(self):
        Project.objects.filter(pk=self.p.pk).update(phase=Project.Phase.CONSTRUCTION)
        self.assertTrue(self._tender().done)

    def test_shown_as_in_construction(self):
        self._status(stage=self.constr)
        r = self.client.get(f"/projects/{self.p.pk}/")
        self.assertContains(r, "done (in construction)")
        self.assertContains(r, "✓</span>In construction")
        self.assertNotContains(r, "data-tender-confirm=")     # no tick button left to press


class RolloverTests(Base):
    """Base project: forecast Q1 10 + Q2 20; PCCF 0 approves Q1 1,000 + Q2 2,000."""

    def setUp(self):
        super().setUp()
        from .textutil import current_fiscal_year
        self.fy = current_fiscal_year()
        ProjectStatus.objects.filter(project=self.p).update(
            fiscal_year=self.fy - 1, previous=D("100"), q1=D("10"), q2=D("20"), q3=D("0"), q4=D("0"),
            next_year=D("1000.01"), future=D("500"))

    def test_roll_one_year(self):
        from .rollover import FIELDS, roll_one_year
        before = {"previous": D("100"), "q1": D("10"), "q2": D("20"), "q3": D("0"), "q4": D("0"),
                  "next_year": D("1000.01"), "future": D("500")}
        after = roll_one_year(before)
        self.assertEqual(after, {"previous": D("130"), "q1": D("250.00"), "q2": D("250.00"), "q3": D("250.00"),
                                 "q4": D("250.01"), "next_year": D("500"), "future": D("0")})
        self.assertEqual(sum(after[k] for k in FIELDS), sum(before[k] for k in FIELDS))   # total unchanged

    def test_several_years_behind(self):
        from .rollover import plan
        ProjectStatus.objects.filter(project=self.p).update(fiscal_year=self.fy - 2)
        p = plan([ProjectStatus.objects.get(project=self.p)])[0]
        self.assertEqual((p.years, p.after["previous"], p.after["next_year"]), (2, D("1130.01"), D("0")))
        self.assertEqual(p.after_year, D("500"))

    def test_stale_forecast_pauses_cash_check_and_warns(self):
        f = project_financials(Project.objects.get(pk=self.p.pk))
        self.assertTrue(f.forecast_stale)
        self.assertFalse(f.pccf_required)              # last year's quarters aren't compared
        self.assertContains(self.client.get("/mpsr/"), "1 forecast is still set to last fiscal year")
        self.assertContains(self.client.get("/"), "still set to last fiscal year")
        r = self.client.get(f"/mpsr/{self.p.pk}/")
        self.assertContains(r, "Move forecast forward")
        self.assertContains(r, f"/mpsr/rollover/?project={self.p.pk}")

    def test_preview_then_apply_ticked_only(self):
        from unittest import mock
        other = Project.objects.create(io_number="1000002", title="Paint")
        ProjectStatus.objects.create(project=other, fiscal_year=self.fy - 1, q1=D("5"))
        r = self.client.get("/mpsr/rollover/")
        self.assertEqual(len(r.context["plans"]), 2)
        self.assertEqual(ProjectStatus.objects.get(project=self.p).fiscal_year, self.fy - 1)   # preview changes nothing
        with mock.patch("tracker.rollover.backup_database", return_value=None) as backup:
            r = self.client.post("/mpsr/rollover/", {"project": [str(self.p.pk)], "next": "/mpsr/"}, follow=True)
        backup.assert_called_once()
        st = ProjectStatus.objects.get(project=self.p)
        self.assertEqual((st.fiscal_year, st.previous, st.q4, st.next_year, st.future),
                         (self.fy, D("130"), D("250.01"), D("500"), D("0")))
        self.assertEqual(ProjectStatus.objects.get(project=other).fiscal_year, self.fy - 1)   # not ticked
        self.assertTrue(StatusHistory.objects.filter(project=self.p, text__startswith="Fiscal-year rollover").exists())
        self.assertContains(r, "1 forecast moved to FY")
        # now current: compared with the PCCF again (approved 3,000 this year vs forecast 1,000.01)
        self.assertTrue(project_financials(Project.objects.get(pk=self.p.pk)).pccf_required)

    def test_approved_lined_up_with_forecast_year(self):
        from .services import approved_for_year
        pc = PCCF.objects.get(project=self.p)
        PCCF.objects.filter(pk=pc.pk).update(fiscal_year=self.fy - 1, previous=D("50"), next_year=D("4000"),
                                             future=D("700"))
        pc.refresh_from_db()
        same = approved_for_year(pc, self.fy - 1)
        self.assertEqual((same["q1"], same["year"], same["next_year"], same["shifted"]),
                         (D("1000"), D("3000"), D("4000"), False))
        moved = approved_for_year(pc, self.fy)            # forecast rolled to this year, PCCF from last year
        self.assertEqual((moved["previous"], moved["q1"], moved["year"], moved["next_year"], moved["future"]),
                         (D("3050"), None, D("4000"), None, D("700")))
        self.assertTrue(moved["shifted"])
        self.assertIsNone(approved_for_year(pc, self.fy + 1)["year"])   # too far apart: nothing to compare

    def test_screens_after_rollover_compare_same_year(self):
        from unittest import mock
        PCCF.objects.filter(project=self.p).update(fiscal_year=self.fy - 1, next_year=D("4000"))
        with mock.patch("tracker.rollover.backup_database", return_value=None):
            self.client.post("/mpsr/rollover/", {"project": [str(self.p.pk)]})
        r = self.client.get(f"/mpsr/{self.p.pk}/")
        ap = r.context["ap"]
        self.assertEqual((ap["q1"], ap["year"]), (None, D("4000")))
        self.assertEqual(r.context["approved_year"], D("4000"))
        self.assertContains(r, "It isn't split by quarter")
        r = self.client.get(f"/projects/{self.p.pk}/")
        self.assertEqual([q["approved"] for q in r.context["quarters"]], [None] * 4)
        self.assertContains(r, "not split by quarter in PCCF 0")
        # value of work stores this year's approved funding, not the PCCF's own year
        self.client.post(f"/projects/{self.p.pk}/update/vow/", {
            "vow-reporting_date": "2026-11-01", "vow-previous_years_with_fees": "0",
            "vow-current_year_no_fees": "100"})
        self.assertEqual(ValueOfWork.objects.get(project=self.p).current_year_funding, D("4000"))

    def test_nothing_ticked(self):
        r = self.client.post("/mpsr/rollover/", {}, follow=True)
        self.assertContains(r, "no projects were ticked")
        self.assertEqual(ProjectStatus.objects.get(project=self.p).fiscal_year, self.fy - 1)


class KPIAndApproachingTests(Base):
    """Cash flow is put in line so only the dates matter here."""

    def setUp(self):
        super().setUp()
        from django.utils import timezone
        self.today = timezone.localdate()
        ProjectStatus.objects.filter(project=self.p).update(q1=D("1000"), q2=D("2000"))

    def _dates(self, approved, forecast, field="substantial"):
        PCCF.objects.filter(project=self.p).update(**{field: approved})
        ProjectStatus.objects.filter(project=self.p).update(**{field: forecast})

    def _fin(self):
        return project_financials(Project.objects.get(pk=self.p.pk))

    def test_missed_target_can_be_marked_kpi_failure_and_undone(self):
        from .models import KPIFailure
        target = self.today - dt.timedelta(days=10)
        self._dates(target, self.today + dt.timedelta(days=40))
        reason = self._fin().pccf_reasons[0]
        self.assertTrue(reason.target_passed)
        r = self.client.get(f"/projects/{self.p.pk}/")
        self.assertContains(r, "Mark as KPI failure")
        r = self.client.post(f"/projects/{self.p.pk}/kpi/substantial/mark/",
                             {"note": "Permit delay", "next": f"/mpsr/{self.p.pk}/"}, follow=True)
        k = KPIFailure.objects.get()
        self.assertEqual((k.milestone, k.target, k.note), ("substantial", target, "Permit delay"))
        self.assertFalse(self._fin().pccf_required)                  # no longer asks for a PCCF
        self.assertContains(r, "recorded as a KPI failure")
        self.assertContains(r, f'action="/projects/{self.p.pk}/kpi/{k.pk}/undo/" class="msg-undo"')
        self.assertContains(r, "KPI failures")                      # listed on the page
        self.assertTrue(StatusHistory.objects.filter(project=self.p, text__startswith="KPI failure").exists())
        self.client.post(f"/projects/{self.p.pk}/kpi/{k.pk}/undo/", {"next": "/"})
        self.assertFalse(KPIFailure.objects.exists())
        self.assertTrue(self._fin().pccf_required)                   # back to asking for a PCCF

    def test_future_target_cannot_be_marked(self):
        from .models import KPIFailure
        self._dates(self.today + dt.timedelta(days=60), self.today + dt.timedelta(days=90))
        self.assertFalse(self._fin().pccf_reasons[0].target_passed)
        self.assertNotContains(self.client.get(f"/projects/{self.p.pk}/"), "Mark as KPI failure")
        r = self.client.post(f"/projects/{self.p.pk}/kpi/substantial/mark/", follow=True)
        self.assertContains(r, "Not saved")
        self.assertFalse(KPIFailure.objects.exists())

    def test_kpi_failure_only_covers_that_target(self):
        """Other dates still flag, and so does the same milestone if a new PCCF sets a new target."""
        from .models import KPIFailure
        old = self.today - dt.timedelta(days=5)
        self._dates(old, self.today + dt.timedelta(days=30))
        self._dates(self.today - dt.timedelta(days=50), self.today, field="design_end")
        KPIFailure.objects.create(project=self.p, milestone="substantial", target=old)
        self.assertEqual([r.key for r in self._fin().pccf_reasons], ["design_end"])
        PCCF.objects.create(project=self.p, submission=1, q1=D("1000"), q2=D("2000"),
                            substantial=self.today + dt.timedelta(days=10), design_end=self.today)
        self.assertEqual([r.key for r in self._fin().pccf_reasons], ["substantial"])

    def test_approaching_target_within_15_days(self):
        for days, expected in ((16, False), (15, True), (3, True), (0, True), (-1, False)):
            target = self.today + dt.timedelta(days=days)
            self._dates(target, target)
            self.assertEqual(bool(self._fin().approaching), expected, days)
        self._dates(self.today + dt.timedelta(days=5), self.today + dt.timedelta(days=5))
        w = self._fin().approaching[0]
        self.assertEqual(w.text, f"Substantial performance target {w.target} is in 5 days")
        self.assertFalse(self._fin().pccf_required)                  # a separate category from PCCF required
        ProjectStatus.objects.filter(project=self.p).update(actual_substantial=self.today)
        self.assertFalse(self._fin().approaching)                    # already achieved

    def test_approaching_shown_on_dashboards(self):
        self._dates(self.today + dt.timedelta(days=7), self.today + dt.timedelta(days=7))
        r = self.client.get("/")
        self.assertEqual(len(r.context["approaching"]), 1)
        self.assertContains(r, "Approaching target dates")
        self.assertContains(self.client.get(f"/mpsr/{self.p.pk}/"), "Approaching target date")
        self.assertContains(self.client.get("/mpsr/"), "⏱ 7d")
        r = self.client.get("/projects/?show=approaching")
        self.assertEqual([f.project.pk for f in r.context["rows"]], [self.p.pk])


class PCCFRequiredTests(Base):
    """Base project: approved PCCF 0 has Q1 1,000 + Q2 2,000 this year; the forecast has Q1 10 + Q2 20."""

    def _fin(self):
        return project_financials(Project.objects.get(pk=self.p.pk))

    def _status(self, **kw):
        ProjectStatus.objects.filter(project=self.p).update(**kw)

    def _cash(self, **quarters):
        data = {"cash-fiscal_year": "", **{f"cash-{k}": "0" for k in ("previous", "q1", "q2", "q3", "q4",
                                                                       "next_year", "future")}}
        data.update({f"cash-{k}": v for k, v in quarters.items()})
        return self.client.post(f"/projects/{self.p.pk}/update/cash/", data, follow=True)

    def test_cash_flow_difference_flags(self):
        f = self._fin()
        self.assertTrue(f.pccf_required)
        self.assertEqual([r.key for r in f.pccf_reasons], ["cash"])
        self.assertIn("forecast $30 vs approved $3,000", f.pccf_reasons[0].text)

    def test_cents_dont_flag(self):
        self._status(q1=D("1000.40"), q2=D("2000"))
        self.assertFalse(self._fin().pccf_required)

    def test_design_end_and_substantial_dates_flag(self):
        self._status(q1=D("1000"), q2=D("2000"), design_end=dt.date(2026, 6, 1), substantial=dt.date(2027, 3, 1))
        PCCF.objects.filter(project=self.p).update(design_end=dt.date(2026, 6, 1), substantial=dt.date(2027, 1, 30))
        f = self._fin()
        self.assertEqual([r.key for r in f.pccf_reasons], ["substantial"])
        self.assertIn("30 days later", f.pccf_reasons[0].text)
        self._status(substantial=dt.date(2027, 1, 30))
        self.assertFalse(self._fin().pccf_required)

    def test_other_dates_dont_flag(self):
        self._status(q1=D("1000"), q2=D("2000"), closeout=dt.date(2028, 1, 1), construction_award=dt.date(2026, 9, 1))
        PCCF.objects.filter(project=self.p).update(closeout=dt.date(2027, 1, 1))
        self.assertFalse(self._fin().pccf_required)

    def test_new_pccf_clears_flag(self):
        PCCF.objects.create(project=self.p, submission=1, q1=D("10"), q2=D("20"))
        self.assertFalse(self._fin().pccf_required)

    def test_changing_forecast_back_clears_flag_with_message(self):
        r = self._cash(q1="1000", q2="2000")
        self.assertFalse(self._fin().pccf_required)
        self.assertContains(r, "matches approved PCCF 0 again")
        r = self._cash(q1="1000", q2="2500")
        self.assertTrue(self._fin().pccf_required)
        self.assertContains(r, "PCCF required")

    def test_pccf_from_last_fiscal_year_uses_its_next_year(self):
        PCCF.objects.filter(project=self.p).update(fiscal_year=2026, next_year=D("30"))
        self._status(fiscal_year=2027)
        self.assertFalse(self._fin().pccf_required)       # last year's PCCF approved 30 for this year
        PCCF.objects.filter(project=self.p).update(fiscal_year=2024)
        self.assertFalse(self._fin().pccf_required)       # too old to know what it approved for this year

    def test_closed_projects_not_flagged(self):
        Project.objects.filter(pk=self.p.pk).update(status=Project.Status.CLOSED)
        self.assertFalse(self._fin().pccf_required)

    def test_flag_shown_on_screens(self):
        r = self.client.get(f"/mpsr/{self.p.pk}/")
        self.assertContains(r, "PCCF required: the forecast no longer matches approved PCCF 0")
        self.assertContains(r, f"/projects/{self.p.pk}/pccf/new/?from=forecast")
        self.assertContains(r, "differs")
        self.assertContains(self.client.get("/mpsr/"), "⚠ Required")
        self.assertContains(self.client.get(f"/projects/{self.p.pk}/"), "PCCF required")
        r = self.client.get("/")
        self.assertEqual(r.context["pccf_count"], 1)
        self.assertContains(r, "PCCFs required")
        self.assertContains(r, "forecast $30 vs approved $3,000")

    def test_projects_list_filter(self):
        other = Project.objects.create(io_number="1000002", title="Paint")
        r = self.client.get("/projects/?show=pccf")
        self.assertEqual([f.project.pk for f in r.context["rows"]], [self.p.pk])
        self.assertContains(r, "PCCF required (1)")
        r = self.client.get("/projects/")
        self.assertEqual({f.project.pk for f in r.context["rows"]}, {self.p.pk, other.pk})
        self.assertContains(r, "⚠ Required")

    def test_new_pccf_from_forecast_prefills_forecast(self):
        self._status(design_end=dt.date(2026, 6, 1), fiscal_year=2027)
        r = self.client.get(f"/projects/{self.p.pk}/pccf/new/?from=forecast")
        form = r.context["form"]
        self.assertEqual(form.initial["q1"], D("10"))
        self.assertEqual(form.initial["design_end"], dt.date(2026, 6, 1))
        self.assertEqual(form.initial["fiscal_year"], 2027)
        self.assertEqual(form.initial["design_cost"], D("100000"))     # budget still from the last PCCF
        self.assertContains(r, "start from the current MPSR forecast")
