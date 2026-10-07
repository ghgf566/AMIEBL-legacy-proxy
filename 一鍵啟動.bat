@echo off
chcp 65001 >nul
if /I "%~1"=="stop" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0interactive-start.ps1" -Stop
    exit /b %ERRORLEVEL%
)

rem Start PowerShell in its own console and let this batch file exit immediately.
rem Ctrl+C is then handled by PowerShell, so cmd.exe will not ask
rem "Terminate batch job (Y/N)?".
start "Local llama.cpp" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0interactive-start.ps1" %*
exit /b 0
