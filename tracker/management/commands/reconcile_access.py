"""Compare the new app's project figures with Access's Project_Financials query.

Usage:  python manage.py reconcile_access [--source PATH]
Every difference should be explained by a DataIssue (duplicates removed, etc.).
"""
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path

try:
    import pyodbc
except ImportError:          # hosted (Linux) copies have no Access driver
    pyodbc = None
from django.conf import settings
from django.core.management.base import BaseCommand

from tracker.models import Project
from tracker.services import financials

FIELDS = [("Budget", "budget"), ("Commitments", "commitments"), ("Pending_Changes", "pending"),
          ("Invoiced", "invoiced"), ("Value_of_Work", "vow_total")]


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--source", default=settings.ACCESS_SOURCE)

    def handle(self, *args, source, **opts):
        tmp = Path(tempfile.mkdtemp()) / "MBN_rec.accdb"
        shutil.copy2(source, tmp)
        conn = pyodbc.connect(f"Driver={{Microsoft Access Driver (*.mdb, *.accdb)}};DBQ={tmp};ReadOnly=1;")
        cur = conn.cursor()
        cur.execute("SELECT * FROM Project_Financials")
        cols = [c[0] for c in cur.description]
        access = {}
        for r in cur.fetchall():
            access.setdefault(r[cols.index("IO_ProjNum")], []).append(dict(zip(cols, r)))
        conn.close()
        shutil.rmtree(tmp.parent, ignore_errors=True)

        new = {f.project.io_number: f for f in financials(Project.objects.all())}
        diffs = 0
        for io, rows in sorted(access.items()):
            f = new.get(io)
            if len(rows) > 1:
                self.stdout.write(f"{io}: Access shows this project {len(rows)} times (duplicate PCCF)")
            a = rows[-1]
            for acol, attr in FIELDS:
                av = Decimal(str(a[acol] or 0)).quantize(Decimal("0.01"))
                nv = getattr(f, attr).quantize(Decimal("0.01"))
                if abs(av - nv) > Decimal("0.05"):
                    diffs += 1
                    self.stdout.write(f"{io:<12} {acol:<16} Access {av:>14,}  new {nv:>14,}  diff {nv - av:>12,}")
        self.stdout.write(f"\n{len(access)} Access projects compared, {diffs} differing figures.")
