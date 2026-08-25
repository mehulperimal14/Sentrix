@echo off
REM ==============================================================================
REM SENTRIX — Intelligent Multimodal Security & Threat Orchestration Platform
REM Unified Start Script (Windows)
REM ==============================================================================

cd /d "%~dp0"

echo =================================================================
echo                SENTRIX — Edge Security System                    
echo =================================================================

IF NOT EXIST "backend\.env" (
    echo [Init] Creating backend\.env from .env.example...
    copy "backend\.env.example" "backend\.env"
)

IF EXIST ".venv\Scripts\python.exe" (
    SET "PYTHON_EXEC=.venv\Scripts\python.exe"
) ELSE IF EXIST "venv\Scripts\python.exe" (
    SET "PYTHON_EXEC=venv\Scripts\python.exe"
) ELSE (
    SET "PYTHON_EXEC=python"
)

echo [Init] Using Python interpreter: %PYTHON_EXEC%
cd backend
"%PYTHON_EXEC%" app.py
pause
