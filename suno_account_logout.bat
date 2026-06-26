@echo off
REM Remove a stored account login (deletes that account's Chrome profile).
REM Usage:  suno_account_logout.bat <account>     e.g.  suno_account_logout.bat second
setlocal
cd /d "%~dp0"

set "ACC=%~1"
if "%ACC%"=="" set /p ACC=Account to remove (e.g. second):
if "%ACC%"=="" ( echo No account name given. & pause & exit /b 1 )

set "BASE=%LOCALAPPDATA%\suno_automation"
if /i "%ACC%"=="default" ( set "DIR=%BASE%\playwright_session" ) else ( set "DIR=%BASE%\playwright_session_%ACC%" )

if not exist "%DIR%" ( echo Profile does not exist: "%DIR%" & pause & exit /b 0 )
echo This deletes the saved login for account "%ACC%":
echo   %DIR%
set /p OK=Are you sure? (y/n):
if /i not "%OK%"=="y" ( echo Cancelled. & pause & exit /b 0 )
rmdir /s /q "%DIR%"
echo Removed profile for account "%ACC%".
pause
endlocal
