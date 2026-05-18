@echo off
REM ─────────────────────────────────────────────────────────────────────
REM  snapshot_cron.bat  ·  invoked by Windows Task Scheduler at 12:10 am
REM
REM  Runs snapshot_inventory.py with stdout+stderr appended to a daily log
REM  so unattended failures are visible the next morning. Self-contained:
REM  uses fetch_inventory_smart() which falls back to direct Shopify if
REM  server.py isn't running.
REM ─────────────────────────────────────────────────────────────────────

setlocal
cd /d "%~dp0"

REM Force UTF-8 for Python's stdout/stderr — prevents UnicodeEncodeError on
REM characters like the success-tick when the script is invoked via this bat
REM (Windows redirected stdout defaults to cp1252 which can't encode many of
REM the unicode chars used by print statements). Without this, the inserts
REM succeed but the script exits with code 1, making the task look failed.
set "PYTHONIOENCODING=utf-8"

set "LOG_DIR=%~dp0logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM Use date for the log filename (YYYY-MM-DD via PowerShell — locale-safe)
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

set "LOG_FILE=%LOG_DIR%\snapshot_%TODAY%.log"

echo ============================================== >> "%LOG_FILE%"
echo Snapshot run started at %DATE% %TIME% >> "%LOG_FILE%"
echo ============================================== >> "%LOG_FILE%"

python "%~dp0snapshot_inventory.py" >> "%LOG_FILE%" 2>&1
set "RC=%ERRORLEVEL%"

echo Snapshot run finished at %DATE% %TIME% (exit code %RC%) >> "%LOG_FILE%"
echo. >> "%LOG_FILE%"

exit /b %RC%
