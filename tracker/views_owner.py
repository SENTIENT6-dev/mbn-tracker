"""First-run setup and Settings: each project manager has their own tracker."""
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render

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
