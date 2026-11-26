@echo off
setlocal
chcp 65001 >nul
set "PATH=%~dp0_python;%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0_tools\Manage-Skills.ps1" %*
exit /b %errorlevel%
