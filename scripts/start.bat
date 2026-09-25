@echo off
rem J.A.R.V.I.S. launcher for Windows: sets up a virtual environment on first run, then starts the HUD.
setlocal
cd /d "%~dp0\.."

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.11+ is required: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install -e ".[desktop,windows]"
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Created .env - add your GROQ_API_KEY, save, then run this script again.
    notepad ".env"
    exit /b 0
)

".venv\Scripts\python.exe" -m jarvis serve --open %*
