@echo off
chcp 65001 >nul
title Selftest - Map to CAD
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist "runtime\python.exe" (
  echo.
  echo   [ERROR] runtime\python.exe not found. Folder is incomplete.
  pause
  exit /b
)

"runtime\python.exe" -u selftest.py

echo.
pause
