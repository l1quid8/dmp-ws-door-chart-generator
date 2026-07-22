@echo off
REM Build the Windows .exe for "C1 DMP Toolkit".
REM Double-click this file in Explorer. No prior setup needed beyond
REM Python 3.13 + Tesseract-OCR + Ghostscript.
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "BUILD_HOME=%USERPROFILE%\.dmp-doorchart"
set "VENV=%BUILD_HOME%\venv"
REM Inside set "..." quotes the & is literal, so no ^ escape is needed (and a
REM ^ would become part of the value). Every use of %APP_NAME% below is quoted.
set "APP_NAME=C1 DMP Toolkit"

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

REM 7. Copy app folder to Desktop -------------------------------------------
REM    The build produces a one-folder app, not a single .exe. Resolve the
REM    real Desktop path -- it may be redirected into OneDrive, in which case
REM    %USERPROFILE%\Desktop does not exist.
set "DESKTOP=%USERPROFILE%\Desktop"
for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders" /v Desktop 2^>nul ^| findstr /C:"REG_"') do call set "DESKTOP=%%B"
if not exist "%DESKTOP%" set "DESKTOP=%USERPROFILE%\Desktop"

echo ==^> Copying app to the Desktop...
if exist "%DESKTOP%\%APP_NAME%\" rmdir /S /Q "%DESKTOP%\%APP_NAME%"
xcopy /E /I /Y /Q "%BUILD_HOME%\dist\%APP_NAME%" "%DESKTOP%\%APP_NAME%\" >nul
if errorlevel 1 ( echo ERROR: could not copy the app to the Desktop. & pause & exit /b 1 )

echo.
echo ==^> Done. App folder copied to your Desktop:
echo     "%DESKTOP%\%APP_NAME%"
echo     Open that folder and double-click the app to run it.
pause
