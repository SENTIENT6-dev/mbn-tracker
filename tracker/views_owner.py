"""First-run setup and Settings: each project manager has their own tracker."""
import io

from django.conf import settings
from django.contrib import messages
from django.core.management import CommandError, call_command
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


def _run_import(request, form, owner):
    """Run the Access import from the page and report what happened."""
    d = form.cleaned_data
    out = io.StringIO()
    args = {"source": d["source"], "reset": True, "stdout": out}
    if d["only_mine"] and owner:
        args["pm"] = f"{owner.person.first_name} {owner.person.last_name}"
    else:
        args["all_pms"] = True
    try:
        call_command("import_access", **args)
    except CommandError as e:
        messages.error(request, str(e))
        return False
    o = Owner.current()
    if o:
        o.access_source = d["source"]
        o.save()
    n = Project.objects.count()
    messages.success(request, f"Imported {n} project{'s' if n != 1 else ''} from {d['source']}.")
    return True


def setup(request):
    """First run: who is this tracker for? Then optionally load their projects from Access."""
    owner = Owner.current()
    if owner and request.method == "GET" and "step" not in request.GET:
        return redirect("settings")
    step = "import" if owner else "profile"
    profile = F.OwnerForm(request.POST if step == "profile" and request.method == "POST" else None,
                          initial=_profile_initial(owner), prefix="me")
    has_data = Project.objects.exists()
    imp = F.AccessImportForm(request.POST if step == "import" and request.method == "POST" else None,
                             has_data=has_data, prefix="imp",
                             initial={"source": settings.ACCESS_SOURCE, "only_mine": True})
    if request.method == "POST":
        if step == "profile" and profile.is_valid():
            set_owner(**profile.cleaned_data, keep_existing=True)
            messages.success(request, f"Welcome, {profile.cleaned_data['first_name']}. This tracker is now yours.")
            return redirect("/setup/?step=import")
        if step == "import":
            if "skip" in request.POST:
                return redirect("overview")
            if imp.is_valid() and _run_import(request, imp, Owner.current()):
                return redirect("overview")
    return render(request, "tracker/setup.html", {"profile": profile, "imp": imp, "step": step,
                                                  "has_data": has_data})


def settings_page(request):
    owner = Owner.current()
    profile = F.OwnerForm(request.POST if "save_profile" in request.POST else None,
                          initial=_profile_initial(owner), prefix="me")
    imp = F.AccessImportForm(request.POST if "run_import" in request.POST else None,
                             has_data=Project.objects.exists(), prefix="imp",
                             initial={"source": (owner.access_source if owner and owner.access_source
                                                 else settings.ACCESS_SOURCE), "only_mine": True})
    if request.method == "POST":
        if "backup_now" in request.POST:
            from .backup import backup_database
            path = backup_database("manual")
            if path:
                messages.success(request, f"Backup saved: {path.name}")
            else:
                messages.error(request, "This copy of the tracker doesn't use a database file, so there's nothing to back up.")
            return redirect("settings")
        if "save_profile" in request.POST and profile.is_valid():
            set_owner(**profile.cleaned_data)
            messages.success(request, "Your details are saved.")
            return redirect("settings")
        if "run_import" in request.POST and imp.is_valid() and _run_import(request, imp, owner):
            return redirect("overview")
    from .backup import backups
    return render(request, "tracker/settings.html", {"profile": profile, "imp": imp,
                                                     "project_count": Project.objects.count(),
                                                     "backups": backups()[:12], "backup_dir": settings.BACKUP_DIR,
                                                     "keep_days": settings.BACKUP_KEEP_DAYS})
