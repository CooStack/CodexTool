@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 install.py %*
    exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
    python install.py %*
    exit /b %errorlevel%
)

echo Python 3 未找到，请先安装 Python 3.10+。
exit /b 1
