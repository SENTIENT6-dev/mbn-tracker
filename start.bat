@echo off
rem Starts IO PM Tracker in the background and opens it in your browser (no window stays open).
rem To stop it, click "Stop tracker" at the bottom of the tracker's sidebar.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" launch.py
