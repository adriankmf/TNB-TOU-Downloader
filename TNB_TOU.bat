@echo off
chcp 65001 >nul
title TNB Smart Sync

echo.
echo ============================================================
echo   TNB Smart Sync — Time-of-Use Data Downloader
echo ============================================================
echo.

:: Change to the folder where this .bat file lives
cd /d "%~dp0"

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python and try again.
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Run the sync script
python main.py
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE%==0 (
    echo Done! Press any key to close.
) else (
    echo.
    echo [!] Something went wrong. Check the output above.
    echo [!] If you see "session has expired", refresh your cookie:
    echo     1. Open Chrome - go to the myTNB Smart Living portal
    echo     2. Press F12 - Network tab - click any request - Headers
    echo     3. Copy the full "Cookie:" value
    echo     4. Open .env in this folder and update TNB_COOKIE_STRING=
    echo     5. Run this file again.
    echo.
    echo Press any key to close.
)

pause >nul
