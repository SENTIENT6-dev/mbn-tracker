from django.contrib import admin
from django.urls import path

from tracker import views as v
from tracker import views_manage as m
from tracker import views_mpsr as mp
from tracker import views_report as rp
from tracker import setup as site
from tracker import views_owner as o

urlpatterns = [
    path("", v.overview, name="overview"),
    path("calendar/", v.calendar_view, name="calendar"),
    path("mpsr/", mp.mpsr_list, name="mpsr"),
    path("mpsr/<int:pk>/", mp.mpsr_project, name="mpsr_project"),
    path("mpsr/rollover/", mp.rollover, name="rollover"),
    path("reports/monthly/", rp.report_page, name="report_monthly"),
    path("reports/monthly.xlsx", rp.report_xlsx, name="report_monthly_xlsx"),

    # projects
    path("projects/", v.dashboard, name="projects"),
    path("projects/new/", v.project_form, name="project_new"),
    path("projects/<int:pk>/", v.project_detail, name="project"),
    path("projects/<int:pk>/edit/", v.project_form, name="project_edit"),
    path("projects/<int:pk>/delete/", m.project_delete, name="project_delete"),
    path("projects/<int:pk>/update/<str:section>/", v.project_update, name="project_update"),
    path("projects/<int:pk>/tender/", v.tender_tick, name="tender_tick"),
    path("projects/<int:pk>/kpi/<str:key>/mark/", v.kpi_mark, name="kpi_mark"),
    path("projects/<int:pk>/kpi/<int:kpi_id>/undo/", v.kpi_undo, name="kpi_undo"),
    path("projects/<int:pk>/pccf/new/", v.pccf_form, name="pccf_new"),
    path("projects/<int:pk>/pccf/<int:pccf_id>/", v.pccf_form, name="pccf_edit"),
    path("projects/<int:pk>/pccf/<int:pccf_id>/delete/", m.pccf_delete, name="pccf_delete"),
    path("vow/<int:pk>/edit/", m.vow_form, name="vow_edit"),
    path("vow/<int:pk>/delete/", m.vow_delete, name="vow_delete"),

    # purchase orders, invoices, change orders
    path("pos/", v.po_list, name="po_list"),
    path("pos/new/", v.po_form, name="po_new"),
    path("pos/<str:number>/", v.po_detail, name="po"),
    path("pos/<str:number>/edit/", v.po_form, name="po_edit"),
    path("pos/<str:number>/delete/", m.po_delete, name="po_delete"),
    path("invoices/", m.invoice_list, name="invoices"),
    path("invoices/new/", m.invoice_new, name="invoice_new"),
    path("invoices/<int:pk>/edit/", m.invoice_form, name="invoice_edit"),
    path("invoices/<int:pk>/delete/", m.invoice_delete, name="invoice_delete"),
    path("change-orders/", v.pending_changes, name="pending"),
    path("change-orders/<int:pk>/edit/", m.change_order_form, name="co_edit"),
    path("change-orders/<int:pk>/delete/", m.change_order_delete, name="co_delete"),
    path("change-orders/<int:co_id>/<str:action>/", v.change_order_action, name="co_action"),

    # vendors & contacts
    path("vendors/", m.vendor_list, name="vendors"),
    path("vendors/new/", m.vendor_form, name="vendor_new"),
    path("vendors/<int:pk>/", m.vendor_detail, name="vendor"),
    path("vendors/<int:pk>/edit/", m.vendor_form, name="vendor_edit"),
    path("vendors/<int:pk>/delete/", m.vendor_delete, name="vendor_delete"),
    path("vendors/<int:pk>/merge/", m.vendor_merge, name="vendor_merge"),
    path("people/new/", m.person_form, name="person_new"),
    path("people/<int:pk>/edit/", m.person_form, name="person_edit"),
    path("people/<int:pk>/delete/", m.person_delete, name="person_delete"),

    # facilities
    path("facilities/", m.building_list, name="buildings"),
    path("facilities/buildings/new/", m.building_form, name="building_new"),
    path("facilities/buildings/search/", m.building_search, name="building_search"),
    path("facilities/buildings/<int:pk>/", m.building_detail, name="building"),
    path("facilities/buildings/<int:pk>/edit/", m.building_form, name="building_edit"),
    path("facilities/buildings/<int:pk>/delete/", m.building_delete, name="building_delete"),
    path("facilities/roles/<int:pk>/delete/", m.role_delete, name="role_delete"),
    path("facilities/properties/", m.property_list, name="properties"),
    path("facilities/properties/new/", m.property_form, name="property_new"),
    path("facilities/properties/<int:pk>/", m.property_detail, name="property"),
    path("facilities/properties/<int:pk>/edit/", m.property_form, name="property_edit"),
    path("facilities/properties/<int:pk>/delete/", m.property_delete, name="property_delete"),

    # this PM's tracker
    path("setup/", o.setup, name="setup"),
    path("settings/", o.settings_page, name="settings"),

    # site password (hosted copies)
    path("login/", site.login_view, name="login"),
    path("logout/", site.logout_view, name="logout"),

    # misc
    path("issues/", v.issues, name="issues"),
    path("issues/<int:pk>/toggle/", v.issue_toggle, name="issue_toggle"),
    path("admin/", admin.site.urls),
]
