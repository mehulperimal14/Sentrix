@echo off
REM ============================================================================
REM SENTRIX launcher (Windows)
REM
REM Creates the virtual environment, installs dependencies (again only when
REM requirements.txt changes), creates .env with generated secrets on the first
REM run, checks the setup and starts the dashboard.
REM ============================================================================
setlocal
cd /d "%~dp0"

echo ================================================================
echo  SENTRIX - Multimodal Edge Threat Intelligence
echo ================================================================

if not exist ".venv\Scripts\python.exe" (
    echo [1/5] Creating virtual environment...
    REM Prefer the Python launcher; any Python 3.10-3.14 works.
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist ".venv\Scripts\python.exe" (
        echo ERROR: could not create .venv. Install Python 3.10-3.14 from python.org.
        exit /b 1
    )
) else (
    echo [1/5] Virtual environment present.
)

set "PY=.venv\Scripts\python.exe"
"%PY%" --version

echo [2/5] Installing dependencies (first run: several minutes)...
"%PY%" -c "import hashlib,pathlib,sys;h=hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest();s=pathlib.Path('.venv/.requirements.sha256');sys.exit(0 if s.exists() and s.read_text()==h else 1)"
if errorlevel 1 (
    "%PY%" -m pip install --upgrade pip --quiet
    "%PY%" -m pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo ERROR: dependency installation failed.
        exit /b 1
    )
    "%PY%" -c "import hashlib,pathlib;pathlib.Path('.venv/.requirements.sha256').write_text(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())"
) else (
    echo       requirements unchanged since the last install.
)

echo [3/5] Preparing .env...
"%PY%" tools\setup_env.py
if errorlevel 1 exit /b 1

echo [4/5] Running pre-flight checks...
"%PY%" tools\doctor.py

echo [5/5] Starting server - dashboard at http://127.0.0.1:8000/dashboard
echo       The first start downloads ~600 MB of model weights. Ctrl+C stops it.
echo.
"%PY%" -m sentrix

endlocal
