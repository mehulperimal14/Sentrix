#!/usr/bin/env bash
# =============================================================================
# SENTRIX launcher (macOS / Linux)
#
# Unlike the previous version, this actually does what the docs said it did:
# creates the virtual environment, installs dependencies, and seeds .env.
# =============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "================================================================"
echo " SENTRIX - Multimodal Edge Threat Intelligence"
echo "================================================================"

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/4] Creating virtual environment..."
    python3 -m venv .venv
else
    echo "[1/4] Virtual environment present."
fi

PY=".venv/bin/python"

echo "[2/4] Installing dependencies..."
"$PY" -m pip install --upgrade pip --quiet
"$PY" -m pip install -r requirements.txt --quiet

if [ ! -f ".env" ]; then
    echo "[3/4] Creating .env from .env.example..."
    cp .env.example .env
    if command -v openssl >/dev/null 2>&1; then
        # Seed the two secrets that have no safe default.
        SESSION=$(openssl rand -hex 32)
        EVIDENCE=$(openssl rand -hex 32)
        sed -i.bak "s|^SESSION_SECRET=.*|SESSION_SECRET=${SESSION}|" .env
        sed -i.bak "s|^EVIDENCE_AES_KEY=.*|EVIDENCE_AES_KEY=${EVIDENCE}|" .env
        rm -f .env.bak
        echo "      Generated SESSION_SECRET and EVIDENCE_AES_KEY."
    fi
    echo
    echo "  ACTION REQUIRED: set SENTRIX_PASSWORD in .env before logging in."
    echo
else
    echo "[3/4] .env present."
fi

echo "[4/4] Running pre-flight checks..."
"$PY" tools/doctor.py || true

echo
echo "Starting server - dashboard at http://127.0.0.1:8000/dashboard"
echo "Press Ctrl+C to stop."
echo
exec "$PY" -m sentrix
