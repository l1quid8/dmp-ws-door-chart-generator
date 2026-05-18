@echo off
REM Build the Windows .exe for "DMP WS & Door Chart Generator".
REM Double-click this file in Explorer. No prior setup needed beyond
REM Python 3.13 + Tesseract-OCR + Ghostscript.
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "BUILD_HOME=%USERPROFILE%\.dmp-doorchart"
set "VENV=%BUILD_HOME%\venv"
set "APP_NAME=DMP WS ^& Door Chart Generator"

echo ==^> Project:    %CD%
echo ==^> Build home: %BUILD_HOME%  (local, never synced)
if not exist "%BUILD_HOME%" mkdir "%BUILD_HOME%"

REM 1. Python 3.13 ----------------------------------------------------------
py -3.13 --version >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python 3.13 not found.
    echo        Install it from https://www.python.org/downloads/ and retry.
    pause
    exit /b 1
)

REM 2. Tesseract ------------------------------------------------------------
if not exist "C:\Program Files\Tesseract-OCR\tesseract.exe" (
    where tesseract >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Tesseract-OCR not found.
        echo        Install the UB Mannheim build from
        echo        https://github.com/UB-Mannheim/tesseract/wiki and retry.
        pause
        exit /b 1
    )
)

REM 3. Ghostscript ----------------------------------------------------------
if not exist "C:\Program Files\gs" (
    where gswin64c >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Ghostscript not found.
        echo        Install it from https://ghostscript.com/releases/ and retry.
        pause
        exit /b 1
    )
)

REM 4. Build virtualenv -----------------------------------------------------
if not exist "%VENV%\Scripts\python.exe" (
    echo ==^> Creating build venv...
    py -3.13 -m venv "%VENV%"
    if errorlevel 1 ( echo ERROR: could not create venv. & pause & exit /b 1 )
)

REM 5. Dependencies ---------------------------------------------------------
echo ==^> Installing dependencies...
"%VENV%\Scripts\pip" install --quiet --upgrade pip
"%VENV%\Scripts\pip" install --quiet -r requirements.txt
if errorlevel 1 ( echo ERROR: dependency install failed. & pause & exit /b 1 )

REM 6. PyInstaller build ----------------------------------------------------
echo ==^> Building (this takes a few minutes)...
"%VENV%\Scripts\pyinstaller" --noconfirm --distpath "%BUILD_HOME%\dist" --workpath "%BUILD_HOME%\build" dmp_doorchart.spec
if errorlevel 1 ( echo ERROR: build failed. & pause & exit /b 1 )

REM 7. Copy to Desktop ------------------------------------------------------
copy /Y "%BUILD_HOME%\dist\%APP_NAME%.exe" "%USERPROFILE%\Desktop\" >nul
if errorlevel 1 ( echo ERROR: could not copy the .exe to the Desktop. & pause & exit /b 1 )

echo.
echo ==^> Done. App copied to your Desktop:
echo     %USERPROFILE%\Desktop\%APP_NAME%.exe
pause
