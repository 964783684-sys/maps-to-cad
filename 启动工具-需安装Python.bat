@echo off
chcp 65001 >nul
title Map to CAD - 免费矢量地图工具
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

rem 先找 Python
where python >nul 2>nul
if %errorlevel%==0 goto HAVE_PY

where py >nul 2>nul
if %errorlevel%==0 goto HAVE_PY

echo.
echo   [提示] 没检测到 Python，请先安装：
echo.
echo   1. 打开官网 https://www.python.org/downloads/
echo   2. 下载 Python 3.10 或更高版本
echo   3. 安装时务必勾选 "Add Python to PATH"
echo   4. 装好后重新双击本文件
echo.
pause
exit /b

:HAVE_PY
rem 装依赖（有 requirements.txt 时）
if exist "requirements.txt" (
  echo   首次运行会自动安装依赖，请稍候...
  python -m pip install -r requirements.txt
)

echo.
echo   启动中，浏览器会自动打开工具页面...
python server.py

echo.
pause
