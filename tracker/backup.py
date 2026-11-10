"""Backups of the tracker's database (a single SQLite file).

* A daily copy is made automatically the first time the tracker is used each day (DailyBackupMiddleware).
  Daily copies older than BACKUP_KEEP_DAYS are deleted.
* "Back up now" on the Settings page, and the fiscal-year rollover, make extra copies; those are kept until
  you delete them.

Copies go to settings.BACKUP_DIR (the backups folder next to the tracker by default, or MBN_BACKUP_DIR,
e.g. a OneDrive folder). To restore one: stop the tracker, copy the backup over db.sqlite3, start it again.
"""
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

log = logging.getLogger(__name__)
DAILY = "daily"


def _is_backupable():
    db = settings.DATABASES["default"]
    name = str(db.get("NAME", ""))
    # tests use an in-memory database: nothing to back up
    return db["ENGINE"] == "django.db.backends.sqlite3" and "memory" not in name and name != ":memory:"


def folder():
    path = Path(settings.BACKUP_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_database(label="manual"):
    """Copy the database into the backups folder. Returns the file path (None if it isn't a SQLite file)."""
    if not _is_backupable():
        return None
    target = folder() / f"db-{label}-{timezone.localtime():%Y%m%d-%H%M%S}.sqlite3"
    connection.ensure_connection()
    dest = sqlite3.connect(str(target))
    try:
        connection.connection.backup(dest)   # safe while the tracker is running
    finally:
        dest.close()
    return target


@dataclass
class Backup:
    path: Path

    @property
    def name(self):
        return self.path.name

    @property
    def kind(self):
        """daily / manual / rollover, from db-<kind>-YYYYMMDD-HHMMSS.sqlite3 (older files: db-before-rollover-...)."""
        parts = self.name[:-len(".sqlite3")].split("-")[1:-2]
        return "-".join(p for p in parts if p != "before")

    @property
    def kind_label(self):
        return {"daily": "Daily", "manual": "Back up now", "rollover": "Before rollover"}.get(
            self.kind, self.kind.capitalize() or "Backup")

    @property
    def made(self):
        return dt.datetime.fromtimestamp(self.path.stat().st_mtime)

    @property
    def size_mb(self):
        return self.path.stat().st_size / 1_048_576


def backups():
    """Every backup in the folder, newest first."""
    if not Path(settings.BACKUP_DIR).exists():
        return []
    return sorted((Backup(p) for p in Path(settings.BACKUP_DIR).glob("db-*.sqlite3")),
                  key=lambda b: b.made, reverse=True)


def prune(keep_days=None, today=None):
    """Delete daily copies older than keep_days. Other backups are never deleted automatically."""
    keep_days = settings.BACKUP_KEEP_DAYS if keep_days is None else keep_days
    cutoff = (today or timezone.localdate()) - dt.timedelta(days=keep_days)
    removed = 0
    for b in backups():
        if b.kind == DAILY and b.made.date() < cutoff:
            b.path.unlink(missing_ok=True)
            removed += 1
    return removed


def ensure_daily_backup(today=None):
    """Make today's daily copy if there isn't one yet, then tidy old ones. Returns the new path or None."""
    if not _is_backupable():
        return None
    today = today or timezone.localdate()
    if any(folder().glob(f"db-{DAILY}-{today:%Y%m%d}-*.sqlite3")):
        return None
    path = backup_database(DAILY)
    prune(today=today)
    return path


class DailyBackupMiddleware:
    """Checks once per day per running tracker; a failed backup is logged and never stops the page."""
    def __init__(self, get_response):
        self.get_response = get_response
        self.checked = None

    def __call__(self, request):
        today = timezone.localdate()
        if self.checked != today and getattr(settings, "BACKUP_DAILY", True):
            self.checked = today
            try:
                ensure_daily_backup(today)
            except Exception:   # noqa: BLE001 - a backup problem must not break the tracker
                log.exception("Daily backup failed")
        return self.get_response(request)
