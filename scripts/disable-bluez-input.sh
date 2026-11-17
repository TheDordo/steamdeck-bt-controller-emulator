#!/bin/bash
# SteamOS prerelease fix: bluetoothd's input plugin registers the HID L2CAP
# PSMs 0x11/0x13, which blocks the Pro Controller (BR/EDR gamepad) mode.
#
# This installs a systemd drop-in that starts bluetoothd with
# --noplugin=input. Trade-off: the Deck loses the ability to use Bluetooth
# keyboards/mice as a HOST (connecting its own BT input devices). When
# nothing is paired, that costs nothing.
#
# Revert with: scripts/disable-bluez-input.sh --undo
#
# Usage:
#   sudo bash scripts/disable-bluez-input.sh
#   sudo bash scripts/disable-bluez-input.sh --undo

set -euo pipefail

DROPIN_DIR="/etc/systemd/system/bluetooth.service.d"
DROPIN_FILE="$DROPIN_DIR/no-input-hid.conf"
BLUEZD="/usr/lib/bluetooth/bluetoothd"

if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo bash $0"
    exit 1
fi

if [ "${1:-}" = "--undo" ]; then
    rm -f "$DROPIN_FILE"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart bluetooth
    echo "✓ BlueZ input plugin re-enabled (Deck-side BT HID host restored)"
    exit 0
fi

if [ ! -x "$BLUEZD" ]; then
    echo "Error: bluetoothd not found at $BLUEZD"
    exit 1
fi

mkdir -p "$DROPIN_DIR"
cat > "$DROPIN_FILE" << EOF
# Disable BlueZ's input plugin so the HID L2CAP PSMs 0x11/0x13 stay free
# for BT Controller Emulator's Pro Controller mode (SteamOS prerelease).
[Service]
ExecStart=
ExecStart=$BLUEZD --noplugin=input
EOF

systemctl daemon-reload
systemctl restart bluetooth
echo "✓ BlueZ input plugin disabled — Pro Controller mode can bind PSM 0x11/0x13"
echo "  Revert with: sudo bash $0 --undo"
