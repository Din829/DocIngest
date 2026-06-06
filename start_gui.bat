@echo off
REM DocIngest GUI launcher - double-click to open the desktop window.
setlocal
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ or create a .venv.
    pause
    exit /b 1
)

"%PY%" -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pywebview not installed. Run:  "%PY%" -m pip install pywebview
    pause
    exit /b 1
)

echo Starting DocIngest GUI...
"%PY%" -m docingest.gui
if errorlevel 1 (
    echo.
    echo [ERROR] The GUI exited with an error (see messages above).
    pause
    exit /b 1
)
endlocal
