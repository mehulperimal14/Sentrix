#!/usr/bin/env bash
# =============================================================================
# SENTRIX launcher (macOS / Linux)
#
# Creates the virtual environment, installs dependencies (again only when
# requirements.txt changes), creates .env with generated secrets on the first
# run, checks the setup and starts the dashboard.
#
# Debian/Ubuntu need:  sudo apt install python3-venv libportaudio2 libgl1 libglib2.0-0
# =============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "================================================================"
echo " SENTRIX - Multimodal Edge Threat Intelligence"
echo "================================================================"

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/5] Creating virtual environment..."
    python3 -m venv .venv
else
    echo "[1/5] Virtual environment present."
fi

PY=".venv/bin/python"
"$PY" --version

echo "[2/5] Installing dependencies (first run: several minutes)..."
STAMP=".venv/.requirements.sha256"
HASH=$("$PY" -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('requirements.txt').read_bytes()).hexdigest())")
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
    "$PY" -m pip install --upgrade pip --quiet
    "$PY" -m pip install -r requirements.txt --quiet
    printf '%s' "$HASH" > "$STAMP"
else
    echo "      requirements unchanged since the last install."
fi

echo "[3/5] Preparing .env..."
"$PY" tools/setup_env.py

echo "[4/5] Running pre-flight checks..."
"$PY" tools/doctor.py || true

echo "[5/5] Starting server - dashboard at http://127.0.0.1:8000/dashboard"
echo "      The first start downloads ~600 MB of model weights. Ctrl+C stops it."
echo
exec "$PY" -m sentrix
