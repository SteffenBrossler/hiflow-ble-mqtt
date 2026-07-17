#!/bin/bash
# ble-watchdog.sh — detect and recover a hung onboard Bluetooth adapter (hci0).
#
# The Raspberry Pi's onboard UART-attached Bluetooth chip (BCM43xx via
# hci_uart/btbcm) occasionally hangs at the kernel/HCI level — even
# `hciconfig hci0 reset` times out. The only known recovery is to unload
# and reload the kernel modules. This must run on the HOST (requires root
# for rmmod/modprobe); it cannot run inside the hiflow-ble Docker container.
#
# Two independent triggers feed into this script:
#   1. ble-watchdog.path  — fires almost instantly when the hiflow-ble
#      container writes FLAG_FILE, i.e. when its own in-process recovery
#      (RemoveDevice + one retry) already failed with org.bluez.Error.InProgress.
#   2. ble-watchdog.timer — polls every 5 min as a fallback for silent
#      failures (e.g. the container itself crashed before it could write
#      the flag).
#
# Install (see README.md for details):
#   sudo ./scripts/install-ble-watchdog.sh /path/to/hoymiles-ble-mqtt
#
# COMPOSE_DIR is not hardcoded to a home directory — it is read from
# /etc/default/ble-watchdog (created by install-ble-watchdog.sh), so any
# freely chosen path works.

set -euo pipefail

LOG_TAG="ble-watchdog"

# COMPOSE_DIR is the directory containing docker-compose.yml and the
# ./state folder shared with the hiflow-ble container. Configure it in
# /etc/default/ble-watchdog (see scripts/ble-watchdog.env.example), or
# export COMPOSE_DIR before running this script manually.
ENV_FILE="${BLE_WATCHDOG_ENV_FILE:-/etc/default/ble-watchdog}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"

if [ -z "${COMPOSE_DIR:-}" ]; then
    echo "COMPOSE_DIR is not set — configure it in $ENV_FILE (see scripts/ble-watchdog.env.example)" >&2
    exit 1
fi

FLAG_FILE="$COMPOSE_DIR/state/restart_bluetooth.state"
LOCK_FILE="/run/ble-watchdog.lastrun"
MIN_INTERVAL=60  # seconds — avoid hammering rmmod/modprobe if the flag keeps reappearing

needs_recovery=false

if [ -f "$FLAG_FILE" ]; then
    logger -t "$LOG_TAG" "InProgress flag present — hiflow-ble's in-process recovery failed"
    needs_recovery=true
fi

# Check if adapter is genuinely running (not just "up" at the ioctl level).
# hciconfig hci0 up returns 0 even when BlueZ left the chip in PowerState:off
# (zombie state after a failed power cycle). "UP RUNNING" in the output means
# the HCI controller is fully initialised and ready. This catches silent
# failures where the container crashed before it could write the flag.
if ! timeout 5 hciconfig hci0 2>/dev/null | grep -q "UP RUNNING"; then
    logger -t "$LOG_TAG" "hci0 not UP RUNNING"
    needs_recovery=true
fi

if [ "$needs_recovery" = false ]; then
    logger -t "$LOG_TAG" "hci0 OK"
    exit 0
fi

# Restart the poll container so it doesn't keep hammering the old BlueZ state
if [ -d "$COMPOSE_DIR" ]; then
    (cd "$COMPOSE_DIR" && docker compose down) && \
        logger -t "$LOG_TAG" "hiflow-ble container stopped"
fi


now=$(date +%s)
if [ -f "$LOCK_FILE" ]; then
    last=$(cat "$LOCK_FILE" 2>/dev/null || echo 0)
    if [ $(( now - last )) -lt $MIN_INTERVAL ]; then
        logger -t "$LOG_TAG" "cooldown active ($(( MIN_INTERVAL - (now - last) ))s left) — skipping"
        exit 0
    fi
fi
echo "$now" > "$LOCK_FILE"

logger -t "$LOG_TAG" "reloading hci_uart/btbcm modules"

rmmod hci_uart 2>/dev/null || true
sleep 1
rmmod btbcm 2>/dev/null || true
sleep 1
modprobe hci_uart
sleep 3
systemctl restart bluetooth
sleep 2
sudo bluetoothctl power on
sleep 2
hciconfig hci0 up || true

logger -t "$LOG_TAG" "recovery done — hci0: $(hciconfig hci0 | head -1)"

rm -f "$FLAG_FILE"

# Restart the poll container so it doesn't keep hammering the old BlueZ state
if [ -d "$COMPOSE_DIR" ]; then
    (cd "$COMPOSE_DIR" && docker compose up -d) && \
        logger -t "$LOG_TAG" "hiflow-ble container started"
fi
