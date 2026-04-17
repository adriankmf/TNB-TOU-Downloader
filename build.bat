@echo off
chcp 65001 >nul
title TNB Smart Sync -- Build EXE

echo.
echo ============================================================
echo   TNB Smart Sync -- Build Script
echo   Packages app.py into a standalone TNB_TOU.exe
echo ============================================================
echo.

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://www.python.org/
    pause & exit /b 1
)

:: Install / upgrade dependencies
echo [1/3] Installing dependencies...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection.
    pause & exit /b 1
)

:: Clean previous build
echo [2/3] Cleaning previous build...
if exist build   rmdir /s /q build
if exist dist    rmdir /s /q dist

:: Build
echo [3/3] Building TNB_TOU.exe -- this takes 1-3 minutes...
pyinstaller TNB_TOU.spec

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. See error messages above.
    pause & exit /b 1
)

echo.
echo ============================================================
echo   BUILD SUCCESSFUL!
echo.
echo   Your exe is at:  dist\TNB_TOU.exe
echo.
echo   To distribute:
echo     Copy  dist\TNB_TOU.exe  to any Windows PC.
echo     The exe is fully self-contained -- no Python needed.
echo     On first launch, settings.json is created next to the exe.
echo ============================================================
echo.
pause
