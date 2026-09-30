@echo off
REM ============================================================================
REM SENTRIX launcher (Windows)
REM
REM Unlike the previous version, this actually does what the docs said it did:
REM creates the virtual environment, installs dependencies, and seeds .env.
REM ============================================================================
setlocal
cd /d "%~dp0"

echo ================================================================
echo  SENTRIX - Multimodal Edge Threat Intelligence
echo ================================================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/4] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create .venv. Is Python 3.10+ on PATH?
        exit /b 1
    )
) else (
    echo [1/4] Virtual environment present.
)

set "PY=.venv\Scripts\python.exe"

echo [2/4] Installing dependencies...
"%PY%" -m pip install --upgrade pip --quiet
"%PY%" -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo ERROR: dependency installation failed.
    exit /b 1
)

if not exist ".env" (
    echo [3/4] Creating .env from .env.example...
    copy ".env.example" ".env" >nul
    echo.
    echo   ACTION REQUIRED: set SENTRIX_PASSWORD in .env before logging in.
    echo   Generate secrets with:
    echo     .venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
    echo.
) else (
    echo [3/4] .env present.
)

echo [4/4] Running pre-flight checks...
"%PY%" tools\doctor.py
echo.
echo Starting server - dashboard at http://127.0.0.1:8000/dashboard
echo Press Ctrl+C to stop.
echo.
"%PY%" -m sentrix

endlocal
