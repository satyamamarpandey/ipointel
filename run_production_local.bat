@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set PG_BIN=C:\Program Files\PostgreSQL\17\bin
set PGDATA_DIR=%~dp0pgdata
set WEB_PORT=8010
set CADDY_PORT=8080
set LOGDIR=%~dp0logs
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo === 1. Loading environment (.env.production) ===
if not exist ".env.production" (
    echo FATAL: .env.production not found. Cannot start in production mode without it.
    exit /b 1
)
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env.production") do (
    if not "%%A"=="" set "%%A=%%B"
)
if not "%APP_ENV%"=="production" (
    echo FATAL: APP_ENV is "%APP_ENV%", expected "production". Check .env.production.
    exit /b 1
)
if "%DATABASE_URL:sqlite=%"=="%DATABASE_URL%" (
    echo DATABASE_URL is PostgreSQL - OK
) else (
    echo FATAL: DATABASE_URL is SQLite - refusing to run production mode against SQLite.
    exit /b 1
)

echo === 2. PostgreSQL ===
"%PG_BIN%\pg_isready.exe" -h 127.0.0.1 -p 5435 >nul 2>&1
if errorlevel 1 (
    echo PostgreSQL not responding on 5435 - attempting to start the local cluster...
    "%PG_BIN%\pg_ctl.exe" -D "%PGDATA_DIR%" -l "%LOGDIR%\postgres.log" start
    timeout /t 3 /nobreak >nul
    "%PG_BIN%\pg_isready.exe" -h 127.0.0.1 -p 5435 >nul 2>&1
    if errorlevel 1 (
        echo FATAL: PostgreSQL could not be started. Check %LOGDIR%\postgres.log
        exit /b 1
    )
)
echo PostgreSQL OK on 5435

echo === 3. Alembic migration ===
python -m alembic upgrade head
if errorlevel 1 (
    echo FATAL: alembic upgrade head failed.
    exit /b 1
)

echo === 4. Web service ===
netstat -ano | findstr ":%WEB_PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Port %WEB_PORT% already listening - assuming web service already running, not starting a duplicate.
) else (
    start "IPO Web (production)" /min cmd /c "python -m uvicorn app.main:app --host 127.0.0.1 --port %WEB_PORT% --log-level info > "%LOGDIR%\web.log" 2>&1"
    echo Web service starting on 127.0.0.1:%WEB_PORT%
)

echo === 5. Worker service ===
tasklist /fi "imagename eq python.exe" /v | findstr /c:"app.worker" >nul 2>&1
if not errorlevel 1 (
    echo A python.exe process is already running app.worker - not starting a duplicate.
) else (
    start "IPO Worker (production)" /min cmd /c "python -m app.worker > "%LOGDIR%\worker.log" 2>&1"
    echo Worker service starting
)

echo === 6. Caddy reverse proxy ===
netstat -ano | findstr ":%CADDY_PORT% " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo Port %CADDY_PORT% already listening - assuming Caddy already running, not starting a duplicate.
) else (
    start "IPO Caddy (production)" /min cmd /c "bin\caddy.exe run --config deploy\Caddyfile.local --adapter caddyfile > "%LOGDIR%\caddy.log" 2>&1"
    echo Caddy starting on http://localhost:%CADDY_PORT%
)

echo === 7. Mailpit (local-production email testing) ===
tasklist /fi "imagename eq mailpit.exe" | findstr /i mailpit.exe >nul 2>&1
if not errorlevel 1 (
    echo Mailpit already running - not starting a duplicate.
) else (
    start "IPO Mailpit" /min cmd /c "bin\mailpit.exe > "%LOGDIR%\mailpit.log" 2>&1"
    echo Mailpit starting
)

echo === 8. Waiting for health ===
set /a tries=0
:healthloop
set /a tries+=1
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:%WEB_PORT%/health > "%TEMP%\ipo_health_code.txt" 2>nul
set /p HEALTH_CODE=<"%TEMP%\ipo_health_code.txt"
if "%HEALTH_CODE%"=="200" goto healthok
if %tries% GEQ 30 (
    echo FATAL: web service did not become healthy within 30s. Check %LOGDIR%\web.log
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto healthloop
:healthok
echo Web service healthy.

echo.
echo === IPO Intelligence Terminal - production-local stack is up ===
echo   Primary URL (via Caddy):  http://localhost:%CADDY_PORT%
echo   Direct web (bypass proxy): http://127.0.0.1:%WEB_PORT%
echo   Health:                   http://127.0.0.1:%WEB_PORT%/health
echo   Mailpit UI:                http://127.0.0.1:8025
echo   Logs:                      %LOGDIR%
echo.
echo Stop with: stop_production_local.bat
endlocal
