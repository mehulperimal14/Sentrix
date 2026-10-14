#!/usr/bin/env bash
# Run from a machine OUTSIDE the VM: only 80 and 443 may answer (reviews: a
# published Docker port bypasses ufw; Postgres must never be reachable).
#   ./check_ports.sh portal.example.in
set -euo pipefail
HOST="${1:?usage: check_ports.sh <host>}"
OPEN=$(nmap -Pn -p 1-65535 --open "$HOST" -oG - | sed -n 's/.*Ports: //p' | tr ',' '\n' \
       | awk -F/ '$2=="open"{print $1}' | sort -n | tr '\n' ' ')
echo "open: $OPEN"
for port in $OPEN; do
  case "$port" in 22|80|443) ;; *) echo "FAIL: port $port is open"; exit 1 ;; esac
done
echo "ok: only ${OPEN:-nothing} open (22 only if you left SSH open to your IP)"
