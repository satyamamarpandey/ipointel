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

findstr /r /c:"^EMAIL_PROVIDER=mailpit" .env >nul 2>&1
if not errorlevel 1 (
  if not exist bin\mailpit.exe (
    echo Mailpit binary not found at bin\mailpit.exe - download it from
    echo https://github.com/axllent/mailpit/releases and unzip mailpit.exe there,
    echo or switch EMAIL_PROVIDER in .env to something else.
  ) else (
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"127.0.0.1:8025.*LISTENING"') do set MAILPIT_INUSE=%%p
    if defined MAILPIT_INUSE (
      echo Mailpit already running - not starting a second instance.
    ) else (
      echo Starting Mailpit ...
      start "IPO Terminal - Mailpit" /min bin\mailpit.exe --smtp 127.0.0.1:1025 --listen 127.0.0.1:8025
    )
  )
)

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
echo  Mail Inbox: http://localhost:8025/  (only if EMAIL_PROVIDER=mailpit)
echo ==============================================
echo Close the minimized "IPO Terminal" console windows,
echo or run stop_local.bat, to shut everything down.
echo.
endlocal
