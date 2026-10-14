#!/usr/bin/env bash
# =============================================================================
# Firewall for the SENTRIX unit: the Local Console and SSH on the LAN only.
#
# The device never accepts connections from the Internet: the Portal link is
# outbound (wss://, docs/7 §5.1). Everything inbound is denied except SSH and
# the console port from the LAN, and the setup hotspot's DHCP/DNS and console
# while it is up (10.42.0.0/24, NetworkManager's shared subnet).
#
#   sudo deploy/pi5/ufw.sh                  # LAN 192.168.0.0/16
#   sudo deploy/pi5/ufw.sh 10.0.0.0/24      # your LAN
#
# Existing rules are kept (no reset); running it twice adds nothing twice.
# =============================================================================
set -euo pipefail

LAN_CIDR="${1:-192.168.0.0/16}"
PORT="${SENTRIX_PORT:-8000}"
HOTSPOT_CIDR="10.42.0.0/24"

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo $0 [LAN_CIDR]" >&2
    exit 1
fi
if ! command -v ufw >/dev/null 2>&1; then
    echo "ufw is not installed: sudo apt install ufw" >&2
    exit 1
fi

ufw default deny incoming
ufw default allow outgoing
ufw allow from "$LAN_CIDR" to any port 22 proto tcp comment 'SSH (LAN)'
ufw allow from "$LAN_CIDR" to any port "$PORT" proto tcp comment 'SENTRIX Local Console (LAN)'
ufw allow from "$HOTSPOT_CIDR" to any port "$PORT" proto tcp comment 'SENTRIX setup hotspot'
ufw allow in on wlan0 to any port 67 proto udp comment 'setup hotspot DHCP'
ufw allow in on wlan0 from "$HOTSPOT_CIDR" to any port 53 comment 'setup hotspot DNS'
ufw --force enable
ufw status verbose
