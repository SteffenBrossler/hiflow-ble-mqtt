#!/bin/bash
# install-ble-watchdog.sh — install the ble-watchdog scripts and systemd
# units for a freely chosen COMPOSE_DIR, instead of a hardcoded home
# directory. Must run on the HOST (not inside the hiflow-ble container).
#
# Usage:
#   sudo ./scripts/install-ble-watchdog.sh [/path/to/hoymiles-ble-mqtt]
#
# The path argument is the directory that contains docker-compose.yml and
# the ./state folder. Defaults to /opt/hoymiles-ble-mqtt if omitted.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${1:-/opt/hoymiles-ble-mqtt}"
ENV_FILE="/etc/default/ble-watchdog"

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo)." >&2
    exit 1
fi

if [ ! -f "$COMPOSE_DIR/docker-compose.yml" ]; then
    echo "Warning: $COMPOSE_DIR/docker-compose.yml not found — check the path." >&2
fi

echo "Installing ble-watchdog with COMPOSE_DIR=$COMPOSE_DIR"

install -m 755 "$SCRIPT_DIR/ble-watchdog.sh" /usr/local/bin/ble-watchdog.sh

printf 'COMPOSE_DIR=%s\n' "$COMPOSE_DIR" > "$ENV_FILE"
chmod 644 "$ENV_FILE"

install -m 644 "$SCRIPT_DIR/ble-watchdog.service" /etc/systemd/system/ble-watchdog.service
install -m 644 "$SCRIPT_DIR/ble-watchdog.timer" /etc/systemd/system/ble-watchdog.timer

FLAG_FILE="$COMPOSE_DIR/state/restart_bluetooth.state"
sed "s#@FLAG_FILE@#$FLAG_FILE#" "$SCRIPT_DIR/ble-watchdog.path" > /etc/systemd/system/ble-watchdog.path
chmod 644 /etc/systemd/system/ble-watchdog.path

systemctl daemon-reload
systemctl enable --now ble-watchdog.timer ble-watchdog.path

echo "Done. Check status with:"
echo "  systemctl status ble-watchdog.timer ble-watchdog.path"
