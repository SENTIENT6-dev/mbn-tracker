@echo off
rem One-time setup on a new PC: creates the Python environment and installs what the tracker needs.
rem Needs Python 3.9+ (python.org) and, for importing from Access, the Microsoft Access Database Engine.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
".venv\Scripts\python.exe" manage.py migrate -v 0
echo.
echo Done. Double-click start.bat to open your tracker.
pause
