#!/usr/bin/env bash
# ==============================================================================
# SENTRIX — Intelligent Multimodal Security & Threat Orchestration Platform
# Unified Start Script (macOS / Linux)
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "================================================================="
echo "               SENTRIX — Edge Security System                     "
echo "================================================================="

# Create default .env if missing
if [ ! -f "backend/.env" ]; then
    echo "[Init] Creating backend/.env from .env.example..."
    cp backend/.env.example backend/.env
    # Generate random SESSION_SECRET if openssl is available
    if command -v openssl >/dev/null 2>&1; then
        SECRET=$(openssl rand -hex 32)
        sed -i.bak "s/SESSION_SECRET=/SESSION_SECRET=$SECRET/" backend/.env && rm -f backend/.env.bak
    fi
fi

# Detect Virtual Environment
if [ -d "$SCRIPT_DIR/.venv" ]; then
    PYTHON_EXEC="$SCRIPT_DIR/.venv/bin/python"
elif [ -d "$SCRIPT_DIR/venv" ]; then
    PYTHON_EXEC="$SCRIPT_DIR/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_EXEC="$(command -v python3)"
else
    PYTHON_EXEC="python"
fi

echo "[Init] Using Python interpreter: $PYTHON_EXEC"
cd backend
exec "$PYTHON_EXEC" app.py
