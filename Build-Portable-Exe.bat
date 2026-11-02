@echo off
title Build Standalone ChromeHistoryStudio.exe
cd /d "%~dp0"

echo ========================================================================
echo Building standalone single-file ChromeHistoryStudio.exe (no Python needed on target PC)...
echo ========================================================================

python -m pip install --upgrade pyinstaller
python -m PyInstaller --onefile --name "ChromeHistoryStudio" app.py

echo.
echo ========================================================================
echo DONE! Share this single file with anyone:
echo   %~dp0dist\ChromeHistoryStudio.exe
echo They can double-click ChromeHistoryStudio.exe without installing Python!
echo ========================================================================
pause
