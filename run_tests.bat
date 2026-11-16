@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pip install -q pytest
".venv\Scripts\python.exe" -m pytest -v tests
pause
