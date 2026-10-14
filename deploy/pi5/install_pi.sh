#!/usr/bin/env bash
# =============================================================================
# SENTRIX on a Raspberry Pi 5 — one-time system setup (Raspberry Pi OS Trixie
# or Bookworm, 64-bit). Run from the clone, as root:
#
#   sudo deploy/pi5/install_pi.sh [options]
#
#   --user NAME        service user (default: sentrix)
#   --boot-config      append config.txt.snippet to /boot/firmware/config.txt
#                      (siren held off at boot, RTC charging, watchdog; reboot)
#   --ufw [CIDR]       apply the LAN-only firewall (default 192.168.0.0/16)
#   --modem-apn APN    install the ModemManager 4G profile (route metric 700)
#   --modem-ecm [IF]   install the ECM/RNDIS 4G dongle profile (default usb0)
#   --no-apt           skip apt (re-run offline)
#   --no-service       do not install or enable the systemd unit
#   --start            start the service at the end
#   --dry-run          print what would be done, change nothing
#
# What it does, in order: apt packages (Picamera2, lgpio and PortAudio come
# from apt, never pip); the service user and its groups; a venv WITH system
# site packages and requirements-pi.txt (headless OpenCV < 5); .env with
# generated secrets plus the Pi defaults (deploy/pi5/pi.env); /etc/sentrix
# for the device identity; the systemd unit, the journald caps and the
# watchdog drop-in; NetworkManager's connectivity check and the polkit rule
# for /setup/wifi; optionally config.txt, the firewall and the 4G profile.
# Bring-up order and the relay test: deploy/pi5/README.md.
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$HERE/../.." && pwd)"
SERVICE_USER="sentrix"
BOOT_CONFIG=0
UFW=0
LAN_CIDR="192.168.0.0/16"
MODEM_APN=""
MODEM_ECM=""
APT=1
SERVICE=1
START=0
DRY=0
MODEL_FILE="${SENTRIX_PI_MODEL_FILE:-/proc/device-tree/model}"

while [ $# -gt 0 ]; do
    case "$1" in
        --user) SERVICE_USER="$2"; shift ;;
        --boot-config) BOOT_CONFIG=1 ;;
        --ufw)
            UFW=1
            if [ $# -gt 1 ] && [[ "$2" != --* ]]; then LAN_CIDR="$2"; shift; fi ;;
        --modem-apn) MODEM_APN="$2"; shift ;;
        --modem-ecm)
            MODEM_ECM="usb0"
            if [ $# -gt 1 ] && [[ "$2" != --* ]]; then MODEM_ECM="$2"; shift; fi ;;
        --no-apt) APT=0 ;;
        --no-service) SERVICE=0 ;;
        --start) START=1 ;;
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
run()  { if [ "$DRY" = 1 ]; then printf '+ %s\n' "$*"; else "$@"; fi; }

# install_file SRC DEST MODE: copy with @APP_DIR@/@USER@/... filled in.
render() {
    sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@USER@|$SERVICE_USER|g" \
        -e "s|@SIREN_PIN@|$SIREN_PIN|g" -e "s|@SIREN_OFF@|$SIREN_OFF|g" \
        -e "s|@APN@|${MODEM_APN}|g" -e "s|@IFNAME@|${MODEM_ECM}|g" "$1"
}
install_file() {
    local src="$1" dest="$2" mode="$3"
    if [ "$DRY" = 1 ]; then
        printf '+ install -m %s %s -> %s\n' "$mode" "$src" "$dest"
        return
    fi
    install -d "$(dirname "$dest")"
    render "$src" > "$dest.tmp"
    chmod "$mode" "$dest.tmp"
    mv "$dest.tmp" "$dest"
}

# ── preconditions ─────────────────────────────────────────────────────────────
MODEL="$(tr -d '\0' < "$MODEL_FILE" 2>/dev/null || true)"
case "$MODEL" in
    *"Raspberry Pi"*) ;;
    *) echo "not a Raspberry Pi ($MODEL_FILE: '${MODEL:-missing}'); on a laptop use ./run.sh" >&2
       exit 1 ;;
esac
case "$MODEL" in
    *"Raspberry Pi 5"*) ;;
    *) warn "$MODEL: SENTRIX's Pi plan targets the Pi 5 (HARDWARE.md); continuing" ;;
esac
if [ "$DRY" = 0 ] && [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0 $*" >&2
    exit 1
fi
echo "SENTRIX Pi setup: $MODEL, app in $APP_DIR, service user $SERVICE_USER"

# ── 1. apt ────────────────────────────────────────────────────────────────────
REQUIRED_PKGS="python3-venv python3-pip python3-picamera2 python3-libcamera python3-lgpio \
libportaudio2 network-manager ufw"
OPTIONAL_PKGS="rpicam-apps-lite raspi-utils i2c-tools"
if [ "$APT" = 1 ]; then
    say "apt packages"
    run apt-get update
    # shellcheck disable=SC2086
    run apt-get install -y --no-install-recommends $REQUIRED_PKGS
    for pkg in $OPTIONAL_PKGS; do
        run apt-get install -y --no-install-recommends "$pkg" || warn "optional $pkg not installed"
    done
fi

# ── 2. service user ───────────────────────────────────────────────────────────
say "service user $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    run useradd --system --home-dir "/var/lib/$SERVICE_USER" --create-home \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi
for group in gpio video render i2c dialout audio netdev; do
    if getent group "$group" >/dev/null 2>&1; then
        run usermod -aG "$group" "$SERVICE_USER"
    fi
done

# ── 3. venv and requirements ──────────────────────────────────────────────────
say "virtual environment (with system site packages) and requirements-pi.txt"
cd "$APP_DIR"
if [ -f .venv/pyvenv.cfg ] && ! grep -qi '^include-system-site-packages *= *true' .venv/pyvenv.cfg; then
    warn ".venv was created without --system-site-packages: moving it aside"
    run mv .venv ".venv.no-system-site.$(date +%s)"
fi
if [ ! -x .venv/bin/python ]; then
    run python3 -m venv --system-site-packages .venv
fi
run .venv/bin/python -m pip install --upgrade pip --quiet
run .venv/bin/python -m pip install -r requirements-pi.txt
# ultralytics depends on opencv-python (Qt/X11 build); both it and the
# headless build provide cv2, so keep exactly one: the headless one.
if [ "$DRY" = 1 ] || .venv/bin/python -m pip show opencv-python >/dev/null 2>&1; then
    run .venv/bin/python -m pip uninstall -y opencv-python
    run .venv/bin/python -m pip install --force-reinstall --no-deps "opencv-python-headless>=4.10.0,<5"
fi
if [ "$DRY" = 0 ]; then
    .venv/bin/python -c "import hashlib,pathlib;print(hashlib.sha256(pathlib.Path('requirements-pi.txt').read_bytes()).hexdigest(),end='')" \
        > .venv/.requirements.sha256
    .venv/bin/python -c "import picamera2, lgpio, gpiozero, cv2; print('imports ok: picamera2, lgpio, gpiozero, cv2', cv2.__version__)" \
        || warn "the venv cannot import the Pi stack; check the apt packages above"
fi

# ── 4. .env, var/, identity directory ─────────────────────────────────────────
say ".env and runtime directories"
FRESH=""
[ -f .env ] || FRESH="--fresh"
run .venv/bin/python tools/setup_env.py || true
run .venv/bin/python deploy/pi5/merge_env.py .env deploy/pi5/pi.env $FRESH
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" var "/var/lib/$SERVICE_USER"
run install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" /etc/sentrix
run chown -R "$SERVICE_USER:$SERVICE_USER" var
run chown "$SERVICE_USER:$SERVICE_USER" .env
run chmod 600 .env

SIREN_PIN=17
SIREN_OFF=dl
if grep -qE '^SENTRIX_SIREN_ACTIVE_HIGH=(0|false|no|off)' .env 2>/dev/null; then
    SIREN_OFF=dh
fi

# ── 5. systemd, journald, watchdog ────────────────────────────────────────────
if [ "$SERVICE" = 1 ]; then
    say "systemd unit, journald caps, hardware watchdog"
    install_file "$HERE/systemd/sentrix.service.in" /etc/systemd/system/sentrix.service 0644
    install_file "$HERE/systemd/10-sentrix-journald.conf" \
        /etc/systemd/journald.conf.d/10-sentrix.conf 0644
    install_file "$HERE/systemd/10-sentrix-watchdog.conf" \
        /etc/systemd/system.conf.d/10-sentrix-watchdog.conf 0644
    run systemctl daemon-reexec
    run systemctl restart systemd-journald
    run systemctl enable sentrix.service
fi

# ── 6. NetworkManager and polkit ──────────────────────────────────────────────
say "NetworkManager connectivity check (Wi-Fi -> 4G failover) and polkit rule"
install_file "$HERE/networkmanager/20-sentrix-connectivity.conf" \
    /etc/NetworkManager/conf.d/20-sentrix-connectivity.conf 0644
install_file "$HERE/polkit/50-sentrix-networkmanager.rules" \
    /etc/polkit-1/rules.d/50-sentrix-networkmanager.rules 0644
if [ -n "$MODEM_APN" ]; then
    install_file "$HERE/networkmanager/sentrix-modem-gsm.nmconnection.in" \
        /etc/NetworkManager/system-connections/sentrix-modem.nmconnection 0600
fi
if [ -n "$MODEM_ECM" ]; then
    install_file "$HERE/networkmanager/sentrix-modem-ecm.nmconnection.in" \
        /etc/NetworkManager/system-connections/sentrix-modem-ecm.nmconnection 0600
fi
run systemctl reload NetworkManager || warn "NetworkManager not reloaded"
run nmcli connection reload || true

# ── 7. optional: config.txt, firewall ─────────────────────────────────────────
if [ "$BOOT_CONFIG" = 1 ]; then
    say "config.txt"
    CONFIG=/boot/firmware/config.txt
    [ -f "$CONFIG" ] || CONFIG=/boot/config.txt
    if grep -q '>>> SENTRIX >>>' "$CONFIG" 2>/dev/null; then
        echo "SENTRIX block already in $CONFIG"
    elif [ "$DRY" = 1 ]; then
        echo "+ append config.txt.snippet (gpio=$SIREN_PIN=op,$SIREN_OFF) to $CONFIG"
    else
        cp "$CONFIG" "$CONFIG.sentrix-backup.$(date +%s)"
        sed -e "s|^gpio=17=op,dl|gpio=$SIREN_PIN=op,$SIREN_OFF|" "$HERE/config.txt.snippet" >> "$CONFIG"
        echo "appended to $CONFIG (backup next to it); reboot to apply"
    fi
fi
if [ "$UFW" = 1 ]; then
    say "firewall (LAN $LAN_CIDR)"
    run bash "$HERE/ufw.sh" "$LAN_CIDR"
fi

# ── 8. checks and next steps ──────────────────────────────────────────────────
say "checks"
command -v pinctrl  >/dev/null || warn "pinctrl missing: the unit cannot force the siren pin off after a crash (apt install raspi-utils)"
command -v vcgencmd >/dev/null || warn "vcgencmd missing: no throttle or EXT5V readings (apt install raspi-utils)"
if command -v rpi-eeprom-config >/dev/null; then
    if ! rpi-eeprom-config 2>/dev/null | grep -q '^PSU_MAX_CURRENT=5000'; then
        warn "PSU_MAX_CURRENT is not 5000: on a non-PD 5 V supply USB is limited to 600 mA and the modem browns out. Run: sudo rpi-eeprom-config --edit"
    fi
fi
if [ "$START" = 1 ] && [ "$SERVICE" = 1 ]; then
    run systemctl restart sentrix.service
fi
cat <<EOF

Done. Next (deploy/pi5/README.md):
  1. Relay test with an LED in place of the siren BEFORE connecting the siren.
  2. sudo -u $SERVICE_USER $APP_DIR/.venv/bin/python $APP_DIR/tools/doctor.py
  3. sudo systemctl start sentrix && journalctl -u sentrix -f
EOF
