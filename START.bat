@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Sarraf Reconciliation

rem ---- find Python 3.10+ ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto nopython

rem ---- create virtual environment on first run ----
if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Creating virtual environment...
    %PY% -m venv .venv || goto venverr
)

rem ---- install / update requirements when requirements.txt changes ----
fc /b requirements.txt ".venv\requirements.installed" >nul 2>nul
if errorlevel 1 (
    echo [2/2] Installing requirements, please wait...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto piperr
    copy /y requirements.txt ".venv\requirements.installed" >nul
)

start "" ".venv\Scripts\pythonw.exe" main.py
exit /b 0

:nopython
echo Python 3.10 or newer is required. Download it from https://www.python.org/downloads/windows/
echo During installation tick "Add python.exe to PATH".
pause
exit /b 1

:venverr
echo Could not create the virtual environment (.venv).
pause
exit /b 1

:piperr
echo Installing requirements failed. Check the internet connection for the first run only.
pause
exit /b 1
