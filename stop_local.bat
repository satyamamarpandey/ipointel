@echo off
setlocal
cd /d "%~dp0"
echo Stopping IPO Intelligence Terminal processes only...

taskkill /FI "WINDOWTITLE eq IPO Terminal - Web*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq IPO Terminal - Worker*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq IPO Terminal - Mailpit*" /T /F >nul 2>&1

powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'app\.main:app' -or $_.CommandLine -match 'app\.worker' } | Where-Object { $_.CommandLine -match [regex]::Escape('%~dp0') -or $_.CommandLine -match '\.venv' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'mailpit.exe' -and $_.CommandLine -match [regex]::Escape('%~dp0') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Done. Only this project's web/worker processes were targeted.
endlocal
