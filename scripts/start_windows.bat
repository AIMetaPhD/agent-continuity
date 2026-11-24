@echo off
setlocal
cd /d "%~dp0\.."
if not exist ".venv\Scripts\python.exe" (
  echo [Agent Continuity] Creating local Python environment...
  python -m venv .venv
  if errorlevel 1 goto :error
  ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :error
)
echo [Agent Continuity] Starting...
".venv\Scripts\python.exe" app.py --open
goto :eof
:error
echo.
echo Failed to prepare the local environment. Make sure Python 3.11+ is installed and on PATH.
pause
