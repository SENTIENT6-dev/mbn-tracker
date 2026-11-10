"""First-run setup, Settings and Stop tracker: each project manager has their own tracker."""
import os
import threading

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from . import forms as F
from .models import Owner, Project
from .operations import set_owner


def _profile_initial(owner):
    if not owner:
        return {"position": "Project Manager"}
    p = owner.person
    return {"first_name": p.first_name, "last_name": p.last_name, "email": p.email, "phone": p.phone,
            "position": p.position}


def setup(request):
    """First run: who is this tracker for? Then straight to the (empty) tracker."""
    owner = Owner.current()
    if owner:
        return redirect("settings")
    profile = F.OwnerForm(request.POST or None, initial=_profile_initial(owner), prefix="me")
    if request.method == "POST" and profile.is_valid():
        set_owner(**profile.cleaned_data, keep_existing=True)
        messages.success(request, f"Welcome, {profile.cleaned_data['first_name']}. This tracker is now yours. "
                                  f"Add your first project from Projects → ⋯ → New project.")
        return redirect("overview")
    return render(request, "tracker/setup.html", {"profile": profile})


def _exit_soon():
    # after the "stopped" page has been sent
    threading.Timer(0.5, os._exit, (0,)).start()


@require_POST
def stop_tracker(request):
    """Shuts the tracker down (Stop tracker in the sidebar). Only on your own PC, only from this PC."""
    if not settings.ALLOW_STOP or request.META.get("REMOTE_ADDR") not in ("127.0.0.1", "::1"):
        return HttpResponseForbidden("Stopping the tracker isn't allowed here.")
    _exit_soon()
    return render(request, "tracker/stopped.html")


@transaction.non_atomic_requests   # Back up now can't run inside a transaction
def settings_page(request):
    owner = Owner.current()
    profile = F.OwnerForm(request.POST if "save_profile" in request.POST else None,
                          initial=_profile_initial(owner), prefix="me")
    if request.method == "POST":
        if "backup_now" in request.POST:
            from .backup import backup_database
            path = backup_database("manual")
            if path:
                messages.success(request, f"Backup saved: {path.name}")
            else:
                messages.error(request, "This copy of the tracker doesn't use a database file, so there's "
                                        "nothing to back up.")
            return redirect("settings")
        if "save_profile" in request.POST and profile.is_valid():
            set_owner(**profile.cleaned_data)
            messages.success(request, "Your details are saved.")
            return redirect("settings")
    from .backup import backups
    return render(request, "tracker/settings.html", {"profile": profile,
                                                     "project_count": Project.objects.count(),
                                                     "backups": backups()[:12], "backup_dir": settings.BACKUP_DIR,
                                                     "keep_days": settings.BACKUP_KEEP_DAYS})
