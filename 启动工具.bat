@echo off
chcp 65001 >nul
title Map to CAD - 免费矢量地图工具
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "runtime\python.exe" goto NOENV

"runtime\python.exe" -u server.py

echo.
pause
exit /b

:NOENV
echo.
echo   [ERROR] runtime\python.exe not found.
echo   Please copy the WHOLE folder, not a single file.
echo.
pause
