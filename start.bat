@echo off
rem Starts IO PM Tracker and opens it in your browser. Close this window to stop it.
cd /d "%~dp0"
rem Creates or updates the database if needed (a fresh copy starts empty and asks who you are)
".venv\Scripts\python.exe" manage.py migrate -v 0
rem Opens the browser once the server is ready (opening it straight away showed an error page)
start "" /b ".venv\Scripts\python.exe" open_when_ready.py http://127.0.0.1:8000/
".venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000
