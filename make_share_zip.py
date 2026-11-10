"""Makes a clean copy of the tracker to give to someone: IO-PM-Tracker.zip, next to this folder.

It leaves out your data (databases, backups, log), Excel/Access files and the Python environment, so it's safe
to send. Run:  .venv\\Scripts\\python.exe make_share_zip.py
"""
import fnmatch
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {".git", ".venv", "backups", "__pycache__", "staticfiles", ".claude", ".ruff_cache", "node_modules"}
SKIP_FILES = ["*.sqlite3", "*.sqlite3-journal", "*.sqlite3-wal", "*.sqlite3-shm", "*.db", "*.accdb", "*.mdb",
              "*.xlsx", "*.xls", "*.csv", "*.log", "*.pyc", ".env", "*.zip"]


def wanted(path):
    rel = path.relative_to(HERE)
    if any(part in SKIP_DIRS or part.startswith(".venv") for part in rel.parts[:-1]):
        return False
    return not any(fnmatch.fnmatch(rel.name.lower(), pat) for pat in SKIP_FILES)


def main():
    out = HERE.parent / "IO-PM-Tracker.zip"
    files = sorted(p for p in HERE.rglob("*") if p.is_file() and wanted(p))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(p, Path("IO-PM-Tracker") / p.relative_to(HERE))
    print(f"{out}  ({len(files)} files, no database, backups or personal data)")


if __name__ == "__main__":
    sys.exit(main())
