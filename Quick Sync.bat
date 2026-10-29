@echo off
chcp 65001 >nul
title TNB Smart Sync -- Quick Sync

:: Downloads new 30-min readings for the account selected in the app, straight into its
:: Excel file, without opening the app window. Sign in once in "Start TNB Sync.bat" first.
:: Extra options (type them after the file name in a terminal):
::   --fill-gaps            re-download incomplete days in the history
::   --account 210000000002 sync another account
::   --list                 show the accounts on your myTNB login

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)

python main.py %*
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE%==0 (
    echo Done. Press any key to close.
) else if %EXIT_CODE%==2 (
    echo Not signed in yet. Open "Start TNB Sync.bat", sign in once, then run this again.
) else (
    echo Something went wrong -- see the messages above, or tnb_sync.log in this folder.
)
pause >nul
