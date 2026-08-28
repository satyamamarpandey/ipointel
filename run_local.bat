@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist .env copy .env.example .env >nul

if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
)

set PY=.venv\Scripts\python.exe
"%PY%" -m pip install -q -r requirements.txt

if not exist data mkdir data
if not exist logs mkdir logs

set PORT=8010
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127.0.0.1:8010.*LISTENING"') do set INUSE=%%p

if defined INUSE (
  echo Port %PORT% is already in use by PID !INUSE! - not starting a second web process.
  echo If that PID is not this project, stop it or change PORT above.
  set SKIP_WEB=1
)

if not defined SKIP_WEB (
  echo Starting web on http://localhost:%PORT% ...
  start "IPO Terminal - Web" /min "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --log-level info
)

echo Starting worker ...
start "IPO Terminal - Worker" /min "%PY%" -m app.worker

echo.
echo ==============================================
echo  IPO Intelligence Terminal is starting.
echo  Landing:    http://localhost:%PORT%/
echo  Dashboard:  http://localhost:%PORT%/app
echo  API docs:   http://localhost:%PORT%/api/docs
echo  Health:     http://localhost:%PORT%/health
echo ==============================================
echo Close the two minimized "IPO Terminal" console windows,
echo or run stop_local.bat, to shut everything down.
echo.
endlocal
