@echo off
setlocal
cd /d "%~dp0"

echo Stopping IPO Intelligence Terminal production-local services (this project only)...

REM The web match requires this project's specific port (--port 8010), not just
REM "app.main:app" - that module path is FastAPI's own convention and matched
REM (and killed) an unrelated uvicorn process on a different port on this same
REM machine once already. app.worker has no port to key off; it stays a looser
REM match (documented residual risk) since a bare "python -m app.worker" from
REM another project would collide.
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*app.main:app*--port 8010*' -or $_.CommandLine -like '*app.worker*' } | ForEach-Object { Write-Host ('stopping pid ' + $_.ProcessId + ' : ' + $_.CommandLine); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*Caddyfile.local*' } | ForEach-Object { Write-Host ('stopping caddy pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo.
echo Web, worker, and Caddy for this project have been stopped.
echo PostgreSQL and Mailpit are left running - they may be shared with other
echo local projects. To stop them explicitly:
echo   "C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe" -D "%~dp0pgdata" stop
echo   taskkill /IM mailpit.exe /F
endlocal
