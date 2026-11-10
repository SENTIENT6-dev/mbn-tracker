"""Starts IO PM Tracker in the background (no command window) and opens it in the browser.

Run with pythonw.exe (start.bat and the desktop shortcut do this). Stop it from the tracker itself:
the Stop tracker button at the bottom of the sidebar. If it's already running, this just opens the browser.
What the server prints goes to tracker.log in this folder.
"""
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDRESS = "127.0.0.1:8000"
URL = f"http://{ADDRESS}/"


def is_up():
    try:
        urllib.request.urlopen(URL, timeout=2)
    except urllib.error.HTTPError:
        return True  # it answered, even if with an error page
    except OSError:
        return False
    return True


def open_when_ready():
    """The server takes a few seconds to start; opening the browser straight away showed an error page."""
    for _ in range(120):  # up to about a minute
        if is_up():
            break
        time.sleep(0.5)
    webbrowser.open(URL)


def main():
    os.chdir(HERE)
    if is_up():  # already running: just show it
        webbrowser.open(URL)
        return
    # pythonw has no console, so keep the server's messages in a log file
    with open(HERE / "tracker.log", "w", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        sys.path.insert(0, str(HERE))
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mbn_site.settings")
        import django
        from django.core.management import call_command
        django.setup()
        call_command("migrate", verbosity=0)  # creates or updates the database if needed
        threading.Thread(target=open_when_ready, daemon=True).start()
        call_command("runserver", ADDRESS, use_reloader=False)


if __name__ == "__main__":
    main()
