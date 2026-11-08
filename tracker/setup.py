"""First-run setup: each project manager runs their own copy of the tracker.

Until someone has entered who they are, every page sends them to /setup/.
"""
import hmac
import time

from django.conf import settings
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from .models import Owner

OPEN_PATHS = ("/setup/", "/static/", "/admin/", "/favicon.ico", "/login/", "/logout/")
SESSION_KEY = "site_password_ok"


class SitePasswordMiddleware:
    """When MBN_PASSWORD is set (hosted copies), every page asks for it once per browser session."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (settings.SITE_PASSWORD and not request.session.get(SESSION_KEY)
                and not request.path.startswith(("/login/", "/static/", "/favicon.ico"))):
            return redirect(f"/login/?next={request.get_full_path()}")
        return self.get_response(request)


def login_view(request):
    nxt = request.POST.get("next") or request.GET.get("next") or "/"
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = "/"
    if not settings.SITE_PASSWORD:
        return redirect(nxt)
    error = False
    if request.method == "POST":
        if hmac.compare_digest(request.POST.get("password", "").encode(), settings.SITE_PASSWORD.encode()):
            request.session.cycle_key()
            request.session[SESSION_KEY] = True
            request.session.set_expiry(60 * 60 * 24 * 14)     # stay signed in for two weeks
            return redirect(nxt)
        time.sleep(1)                                         # slows down guessing
        error = True
    return render(request, "tracker/login.html", {"next": nxt, "error": error}, status=401 if error else 200)


def logout_view(request):
    request.session.flush()
    return redirect("/login/")


class RequireOwnerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(OPEN_PATHS) and not Owner.objects.exists():
            return redirect("/setup/")
        return self.get_response(request)


def owner_context(request):
    """Makes `me` (the PM this tracker belongs to) available on every page."""
    owner = Owner.current()
    return {"me": owner.person if owner else None, "owner": owner,
            "site_password": bool(settings.SITE_PASSWORD)}
