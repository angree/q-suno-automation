@echo off
REM Generate on a NAMED account. Usage:  suno_account_run.bat <account> [extra args]
REM   e.g.  suno_account_run.bat second --csv example_songs.csv
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

set "ACC=%~1"
if "%ACC%"=="" set /p ACC=Account name [Enter = main account (default)]:
if "%ACC%"=="" set "ACC=default"
REM forward any further args (csv, range, flags) verbatim
set "REST=%2 %3 %4 %5 %6 %7 %8 %9"

echo === Account "%ACC%" ===  extra args:%REST%
"%PY%" "%~dp0suno_automation.py" --use-system-chrome --account "%ACC%" %REST%
echo.
echo Done with account "%ACC%".
pause
endlocal
