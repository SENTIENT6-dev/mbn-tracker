"""Opens the tracker in the browser once the server answers (used by start.bat).

Opening it straight away showed an error page, because the server takes a few seconds to start.
"""
import sys
import time
import urllib.error
import urllib.request
import webbrowser

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/"

for _ in range(120):  # up to about a minute
    try:
        urllib.request.urlopen(URL, timeout=2)
        break
    except urllib.error.HTTPError:
        break  # the server answered (even an error page means it's up)
    except OSError:
        time.sleep(0.5)
webbrowser.open(URL)
