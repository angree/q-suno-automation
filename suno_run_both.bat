@echo off
REM ============================================================
REM  Run BOTH accounts one after another to spend each account's daily credits.
REM  Account 1 finishes (Chrome closes), then account 2 starts. Each uses its own
REM  saved login (separate Chrome profile + CDP port), so they never clash.
REM
REM  SET ONCE:
REM    ACC1 / ACC2  = your two account names (log them in via suno_account_login.bat)
REM    DEFAULT_CSV  = which CSV to generate from when run with no arguments
REM
REM  Bare double-click  -> both accounts, no prompts, from DEFAULT_CSV.
REM  With arguments     -> forwarded to both, e.g.:
REM      suno_run_both.bat --csv my_songs.csv --no-ask-suffix
REM ============================================================
setlocal
cd /d "%~dp0"

set "ACC1=default"
set "ACC2=second"
set "DEFAULT_CSV=example_songs.csv"

set "PY=python"
where py >nul 2>nul && set "PY=py"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

REM --retry-failed also re-tries rows that stopped yesterday on a credit limit
REM (done rows are skipped). --no-ask-suffix skips the style/filename prompts.
if "%~1"=="" (
  set "RUNARGS=--csv %DEFAULT_CSV% --retry-failed --no-ask-suffix"
) else (
  set "RUNARGS=%*"
)

echo ############################################################
echo #  ACCOUNT 1: %ACC1%   args: %RUNARGS%
echo ############################################################
"%PY%" "%~dp0suno_automation.py" --use-system-chrome --account "%ACC1%" %RUNARGS%

echo.
echo ############################################################
echo #  ACCOUNT 2: %ACC2%   args: %RUNARGS%
echo ############################################################
"%PY%" "%~dp0suno_automation.py" --use-system-chrome --account "%ACC2%" %RUNARGS%

echo.
echo Both accounts processed. (If a captcha shows up, the script beeps and waits -- solve it by hand.)
pause
endlocal
