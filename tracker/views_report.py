"""Monthly portfolio report: a printable page (Print / Save as PDF) and the same report as Excel."""
import datetime as dt

from django.http import HttpResponse
from django.shortcuts import render

from .report import monthly_report, report_workbook
from .views_mpsr import _period


def report_page(request):
    report = monthly_report(_period(request))
    prev_p = (report.first - dt.timedelta(days=1)).replace(day=1)
    next_p = (report.first + dt.timedelta(days=32)).replace(day=1)
    return render(request, "tracker/report_monthly.html", {
        "r": report, "q": f"?period={report.first:%Y-%m}",
        "prev": prev_p.strftime("%Y-%m"), "next": next_p.strftime("%Y-%m")})


def report_xlsx(request):
    report = monthly_report(_period(request))
    resp = HttpResponse(report_workbook(report),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="Monthly report {report.first:%Y-%m}.xlsx"'
    return resp
