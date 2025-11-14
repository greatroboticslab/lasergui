@echo off
setlocal

REM Absolute path to Windows PowerShell (present on all supported Windows)
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

REM Resolve repo root and run the driver
"%PS%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
