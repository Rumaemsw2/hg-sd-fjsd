@echo off
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py"
) else (
    set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Python was not found or could not create the virtual environment.
        echo Install Python 3.11/3.12 and enable "Add Python to PATH".
        pause
        exit /b 1
    )
)

echo [2/2] Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo ERROR: Could not install dependencies.
    pause
    exit /b 1
)

echo.
echo Starting Flow AI КПК...
echo Keep this window open while the bot is running.
echo Press Ctrl+C to stop it.
echo.
".venv\Scripts\python.exe" bot.py

echo.
echo Bot stopped.
pause
