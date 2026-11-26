@echo off
setlocal
chcp 65001 >nul
set "PATH=%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"
if exist "%~dp0_tools\portable\provision.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0_tools\portable\provision.ps1"
    if errorlevel 1 exit /b 1
)
"%~dp0_python\python.exe" -B -X utf8 "%~dp0_tools\portable\bootstrap.py" %*
exit /b %errorlevel%
