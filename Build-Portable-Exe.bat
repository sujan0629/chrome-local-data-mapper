@echo off
title Build Standalone WorkStudio.exe
cd /d "%~dp0"

echo ========================================================================
echo Building standalone single-file WorkStudio.exe (no Python needed on target PC)...
echo ========================================================================

python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --name "WorkStudio" app.py

echo.
echo ========================================================================
echo DONE! Share this single file with anyone:
echo   %~dp0dist\WorkStudio.exe
echo They can double-click WorkStudio.exe without installing Python!
echo ========================================================================
pause
