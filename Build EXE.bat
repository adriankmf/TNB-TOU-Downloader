@echo off
chcp 65001 >nul
title TNB Smart Sync -- Build EXE

echo.
echo ============================================================
echo   TNB Smart Sync -- Build Script
echo   Packages the app into a standalone dist\TNB_TOU.exe
echo ============================================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://www.python.org/
    pause & exit /b 1
)

echo [1/4] Installing dependencies...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection.
    pause & exit /b 1
)

echo [2/4] Running tests...
python -m pytest -q tests
if errorlevel 1 (
    echo [ERROR] Tests failed -- exe not built.
    pause & exit /b 1
)

:: Only the old exe is removed from dist -- settings.json and browser_profile next to it
:: hold your account list and login session and must survive a rebuild.
echo [3/4] Cleaning previous build...
if exist build              rmdir /s /q build
if exist dist\TNB_TOU.exe   del /q dist\TNB_TOU.exe

echo [4/4] Building TNB_TOU.exe -- this takes 1-3 minutes...
python -m PyInstaller --noconfirm TNB_TOU.spec
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See error messages above.
    pause & exit /b 1
)

echo.
echo ============================================================
echo   BUILD SUCCESSFUL:  dist\TNB_TOU.exe
echo.
echo   Needs Google Chrome or Microsoft Edge installed.
echo   settings.json, browser_profile\ and tnb_sync.log live next to the exe.
echo   The password is kept in Windows Credential Manager ("TNB Smart Sync").
echo ============================================================
echo.
if not "%1"=="--no-pause" pause
