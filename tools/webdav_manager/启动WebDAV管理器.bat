@echo off
title WebDAV Manager
cd /d "%~dp0"

rem ============================================================
rem  WebDAV Manager launcher (davgui.py)
rem ============================================================

rem ---- server (SFTPGo, normally no need to change) ----
set "DAV_URL=https://gary-pyreader.eastasia.cloudapp.azure.com/"

rem ---- login ----
rem NOTE: do NOT hardcode the password here (this repo is public).
rem Enter the password in the dialog and tick "remember" to store it
rem in the system credential store instead.
set "DAV_USER=gary"
rem set "DAV_PASS=your-password-here"

rem ---- bypass the system proxy and connect directly ----
rem (system proxy points to sing-box 127.0.0.1:1081; when it is
rem  down, requests would fail with it, so we go direct instead)
set "NO_PROXY=*"
set "no_proxy=*"

rem ---- check Python ----
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3 and check "Add python.exe to PATH".
    echo Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

rem ---- check / install dependencies ----
python -c "import webdav3, PySide6" >nul 2>nul
if errorlevel 1 (
    echo [First run] Installing webdavclient3 / PySide6 ...
    python -m pip install webdavclient3 PySide6 -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [ERROR] Dependency install failed. Check network and retry.
        pause
        exit /b 1
    )
)

echo Starting WebDAV Manager ...
python davgui.py

if errorlevel 1 (
    echo.
    echo [ERROR] Startup failed. See the error above.
    pause
)
