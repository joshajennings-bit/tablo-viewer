@echo off
setlocal
title Tablo Viewer setup
cd /d "%~dp0"
echo.
echo === Tablo Viewer setup ===
echo.

rem ---- 1. Find Python 3.9 or newer ----
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo Python 3.9+ was not found.
    choice /m "Install Python 3.12 now with winget"
    if errorlevel 2 goto :nopython
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    echo.
    echo Python installed. Close this window and run Setup.bat again.
    pause
    exit /b 0
)
echo Found Python: & %PY% --version

rem ---- 2. Private environment + packages ----
if not exist ".venv\Scripts\python.exe" (
    echo Creating app environment...
    %PY% -m venv .venv || goto :fail
)
echo Installing packages (requests, keyring)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

rem ---- 3. Video player ----
set "HAVEPLAYER="
where mpv >nul 2>&1 && set "HAVEPLAYER=1"
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\mpv.exe" set "HAVEPLAYER=1"
if exist "%~dp0mpv\mpv.exe" set "HAVEPLAYER=1"
if not defined HAVEPLAYER (
    echo.
    echo The mpv video player is needed to show video inside the app.
    choice /m "Install mpv now with winget"
    if not errorlevel 2 winget install -e --id shinchiro.mpv --accept-package-agreements --accept-source-agreements
)

echo.
echo Setup complete. Double-click "Start Tablo Viewer.bat" to run the app.
pause
exit /b 0

:nopython
echo Install Python from https://www.python.org/downloads/ (check "Add python.exe to PATH"),
echo then run Setup.bat again.
pause
exit /b 1

:fail
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
