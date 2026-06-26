@echo off
REM Log in (once) to a NAMED account profile, stored separately from the others.
REM Usage:  suno_account_login.bat <account>     e.g.  suno_account_login.bat second
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

set "ACC=%~1"
if "%ACC%"=="" set /p ACC=Account name [Enter = main account (default)]:
if "%ACC%"=="" set "ACC=default"

echo ============================================================
echo   Logging in to Suno account: "%ACC%"
echo   A separate Chrome profile for this account will open.
echo   Sign in with Google, wait for suno.com/create to load,
echo   then come back here (the script will ask for Enter).
echo ============================================================
"%PY%" "%~dp0suno_automation.py" --use-system-chrome --login-only --account "%ACC%"
echo.
echo Session for account "%ACC%" saved.
pause
endlocal
