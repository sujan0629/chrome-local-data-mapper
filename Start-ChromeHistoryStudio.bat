@echo off
title Chrome History SQLite Studio
cd /d "%~dp0"

echo ========================================================================
echo Starting Chrome History SQLite Studio...
echo Auto-detecting local Chrome profiles and opening in Microsoft Edge...
echo ========================================================================

if exist "dist\ChromeHistoryStudio.exe" (
    "dist\ChromeHistoryStudio.exe"
    goto :eof
)

where python >nul 2>nul
if %errorlevel% equ 0 (
    python app.py --auto-connect --open-browser
    goto :eof
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    py app.py --auto-connect --open-browser
    goto :eof
)

echo [ERROR] Python is not installed on this computer.
echo Either install Python from https://www.python.org/ or run Build-Portable-Exe.bat on your PC first and share ChromeHistoryStudio.exe!
pause
