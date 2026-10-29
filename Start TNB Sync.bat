@echo off
chcp 65001 >nul
title TNB Smart Sync -- Starting...

:: Opens the TNB Smart Sync app window (app.py) with the latest code in this folder.
:: First time: sign in with your myTNB email + password. After that it remembers you.
:: The black window closes by itself once the app opens.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install it from https://www.python.org/downloads/
    echo         and tick "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

:: First run only: install the packages the app needs.
python -c "import webview, keyring, selenium, pandas, openpyxl" >nul 2>&1
if errorlevel 1 (
    echo Installing required packages, one-time only...
    python -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo [ERROR] Package install failed. Check your internet connection.
        pause
        exit /b 1
    )
)

start "" pythonw app.py
