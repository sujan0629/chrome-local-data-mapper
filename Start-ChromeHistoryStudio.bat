@echo off
title WorkStudio - Chrome Local Data Mapper
cd /d "%~dp0"

echo ========================================================================
echo Starting WorkStudio (Chrome History SQLite Studio)...
echo Auto-detecting local Chrome profiles and opening in Microsoft Edge...
echo ========================================================================

if exist "dist\WorkStudio.exe" (
    "dist\WorkStudio.exe"
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
echo Either install Python from https://www.python.org/ or run WorkStudio.exe!
pause
