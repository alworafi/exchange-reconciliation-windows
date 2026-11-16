@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run START.bat once first to create the environment.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt || goto err
".venv\Scripts\python.exe" -m pytest -q tests || goto testerr
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean SarrafRecon.spec || goto err
echo.
echo Done: dist\SarrafRecon\SarrafRecon.exe
echo Copy the whole dist\SarrafRecon folder. Data is stored in its "data" subfolder.
pause
exit /b 0
:testerr
echo Tests failed - EXE was not built.
pause
exit /b 1
:err
echo Build failed.
pause
exit /b 1
