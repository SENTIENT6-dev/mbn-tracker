"""Change log for purchase orders, invoices and change orders.

Every save or delete of one of these is recorded (old and new values) by Django signals, so it doesn't matter
which screen made the change. Bulk loads (demo data) switch it off with `quiet()`.
"""
import threading
from contextlib import contextmanager, suppress
from decimal import Decimal

from django.db.models.signals import post_delete, post_save, pre_save

from .models import ChangeLog, ChangeOrder, Invoice, PurchaseOrder

_state = threading.local()

# model -> (kind, {field: label})
TRACKED = {
    PurchaseOrder: ("PO", {"number": "PO #", "approved_date": "Approved date", "amount": "PO value",
                           "project": "Project", "vendor": "Vendor"}),
    Invoice: ("Invoice", {"number": "Invoice #", "amount": "Amount", "date": "Date", "po": "PO"}),
    ChangeOrder: ("Change order", {"description": "Description", "value": "Value", "status": "Status",
                                   "po": "PO", "comment": "Comment", "folder_link": "Folder link"}),
}


@contextmanager
def quiet():
    """Don't log changes made inside this block (bulk imports, demo data)."""
    before = getattr(_state, "quiet", False)
    _state.quiet = True
    try:
        yield
    finally:
        _state.quiet = before


def _show(obj, field):
    value = getattr(obj, field, None)
    if value is None or value == "":
        return ""
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if field == "status" and isinstance(obj, ChangeOrder):
        return obj.get_status_display()
    if field == "project":
        return value.io_number
    if field == "po":
        return value.number
    return str(value)


def _values(obj):
    _, fields = TRACKED[type(obj)]
    return {label: _show(obj, f) for f, label in fields.items()}


def _context(obj):
    """(label, project, po number) describing the record."""
    if isinstance(obj, PurchaseOrder):
        return f"PO {obj.number}", obj.project, obj.number
    po = obj.po
    if isinstance(obj, Invoice):
        return f"Invoice {obj.number} on PO {po.number}", po.project, po.number
    return f"Change order “{obj.description[:80]}” on PO {po.number}", po.project, po.number


def _log(obj, action, changes):
    label, project, po_number = _context(obj)
    ChangeLog.objects.create(kind=TRACKED[type(obj)][0], label=label[:255], action=action,
                             project_id=project.pk if project and project.pk else None,
                             project_number=project.io_number if project else "", po_number=po_number,
                             changes=changes)


def before_save(sender, instance, **kwargs):
    if getattr(_state, "quiet", False) or not instance.pk:
        instance._log_old = None
        return
    old = sender.objects.filter(pk=instance.pk).first()
    instance._log_old = _values(old) if old else None


def after_save(sender, instance, created, **kwargs):
    if getattr(_state, "quiet", False):
        return
    new = _values(instance)
    old = getattr(instance, "_log_old", None)
    if created or old is None:
        _log(instance, ChangeLog.Action.ADDED, {k: ["", v] for k, v in new.items() if v})
        return
    changes = {k: [old.get(k, ""), v] for k, v in new.items() if old.get(k, "") != v}
    if changes:
        _log(instance, ChangeLog.Action.CHANGED, changes)


def after_delete(sender, instance, **kwargs):
    if getattr(_state, "quiet", False):
        return
    # if its PO was deleted in the same step, the PO's own entry records it
    with suppress(PurchaseOrder.DoesNotExist, AttributeError):
        _log(instance, ChangeLog.Action.DELETED, {k: [v, ""] for k, v in _values(instance).items() if v})


def connect():
    for model in TRACKED:
        pre_save.connect(before_save, sender=model, dispatch_uid=f"log-pre-{model.__name__}")
        post_save.connect(after_save, sender=model, dispatch_uid=f"log-post-{model.__name__}")
        post_delete.connect(after_delete, sender=model, dispatch_uid=f"log-del-{model.__name__}")
