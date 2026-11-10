@echo off
rem One-time setup on a new PC: creates the Python environment and installs what the tracker needs.
rem Needs Python 3.11 (python.org; "install for me only" works without admin rights).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (py -3.11 -m venv .venv 2>nul || python -m venv .venv)
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
".venv\Scripts\python.exe" manage.py migrate -v 0
rem Desktop shortcut that starts the tracker with no window (same as start.bat, without the brief flash)
powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\IO PM Tracker.lnk'); $s.TargetPath = '%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments = 'launch.py'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'Open IO PM Tracker'; $s.Save()"
echo.
echo Done. Use the "IO PM Tracker" shortcut on your desktop (or start.bat) to open your tracker.
echo To stop it, click "Stop tracker" at the bottom of the tracker's sidebar.
pause
