#!/usr/bin/env bash
# =============================================================================
# SENTRIX launcher (macOS / Linux / Raspberry Pi)
#
# Creates the virtual environment, installs dependencies (again only when
# the requirements file changes), creates .env with generated secrets on the
# first run, checks the setup and starts the dashboard.
#
# Debian/Ubuntu need:  sudo apt install python3-venv libportaudio2 libgl1 libglib2.0-0
#
# On a Raspberry Pi the venv is created with --system-site-packages and
# requirements-pi.txt is installed instead: Picamera2/libcamera and lgpio come
# from apt and are invisible to a plain venv (the camera and the siren would be
# silently dead). One-time system setup: sudo deploy/pi5/install_pi.sh
# (apt: python3-picamera2 python3-lgpio libportaudio2).
#
# SENTRIX_RUN_DRY_RUN=1 prints the platform decision and exits.
# =============================================================================
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PI_MODEL_FILE="${SENTRIX_PI_MODEL_FILE:-/proc/device-tree/model}"
PI_MODEL="$(tr -d '\0' < "$PI_MODEL_FILE" 2>/dev/null || true)"
case "$PI_MODEL" in
    *"Raspberry Pi"*) ON_PI=1 ;;
    *) ON_PI=0 ;;
esac
if [ "$ON_PI" = 1 ]; then
    VENV_ARGS="--system-site-packages"
    REQUIREMENTS="requirements-pi.txt"
else
    VENV_ARGS=""
    REQUIREMENTS="requirements.txt"
fi

if [ "${SENTRIX_RUN_DRY_RUN:-0}" = 1 ]; then
    echo "platform=$([ "$ON_PI" = 1 ] && echo "pi ($PI_MODEL)" || echo other)"
    echo "venv_args=${VENV_ARGS:-none}"
    echo "requirements=$REQUIREMENTS"
    exit 0
fi

echo "================================================================"
echo " SENTRIX - Multimodal Edge Threat Intelligence"
echo "================================================================"
if [ "$ON_PI" = 1 ]; then
    echo " $PI_MODEL: venv with system site packages, $REQUIREMENTS"
    if ! python3 -c "import picamera2, lgpio" >/dev/null 2>&1; then
        echo " WARNING: python3-picamera2 / python3-lgpio are not installed (apt); run"
        echo "          sudo deploy/pi5/install_pi.sh  (or sudo apt install"
        echo "          python3-picamera2 python3-lgpio libportaudio2)"
    fi
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "[1/5] Creating virtual environment..."
    # shellcheck disable=SC2086
    python3 -m venv $VENV_ARGS .venv
else
    echo "[1/5] Virtual environment present."
    if [ "$ON_PI" = 1 ] && ! grep -qi '^include-system-site-packages *= *true' .venv/pyvenv.cfg 2>/dev/null; then
        echo "      WARNING: .venv was made without --system-site-packages, so Picamera2"
        echo "      and lgpio are invisible. Recreate it: rm -rf .venv && ./run.sh"
    fi
fi

PY=".venv/bin/python"
"$PY" --version

echo "[2/5] Installing dependencies (first run: several minutes)..."
STAMP=".venv/.requirements.sha256"
HASH=$("$PY" -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('$REQUIREMENTS').read_bytes()).hexdigest())")
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
    "$PY" -m pip install --upgrade pip --quiet
    "$PY" -m pip install -r "$REQUIREMENTS" --quiet
    if [ "$ON_PI" = 1 ] && "$PY" -m pip show opencv-python >/dev/null 2>&1; then
        # ultralytics pulls the Qt build of OpenCV; keep only the headless one.
        "$PY" -m pip uninstall -y opencv-python --quiet
        "$PY" -m pip install --force-reinstall --no-deps "opencv-python-headless>=4.10.0,<5" --quiet
    fi
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
