#!/bin/bash
# Uninstaller for BT Controller Emulator
# Usage: curl -fsSL https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/uninstall.sh | bash

set -euo pipefail

SERVICE_NAME="bt-controller-emulator"

echo "=== BT Controller Emulator - Uninstaller ==="
echo

if [ "$EUID" -eq 0 ]; then
    echo "Error: Don't run as root. Run as your regular user."
    exit 1
fi

INSTALL_DIR="$HOME/steamdeck-bt-controller-emulator"
DESKTOP_FILE="$HOME/.local/share/applications/bt-controller-emulator.desktop"
DESKTOP_SHORTCUT="$HOME/Desktop/bt-controller-emulator.desktop"

echo "Uninstalling BT Controller Emulator..."
echo "  - systemd daemon (${SERVICE_NAME}.service)"
echo "  - D-Bus policy, udev rule"
echo "  - Desktop shortcuts"
echo "  - Application files from: $INSTALL_DIR"
echo "  - Bluetooth adapter reset (static address cleared)"
echo

# Stop + disable + remove the daemon
echo "Stopping the daemon (this also restores the Bluetooth adapter)..."
sudo systemctl stop ${SERVICE_NAME} 2>/dev/null || true
sudo systemctl disable ${SERVICE_NAME} 2>/dev/null || true
if [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    sudo rm "/etc/systemd/system/${SERVICE_NAME}.service" || {
        echo "⚠ Warning: Could not remove /etc/systemd/system/${SERVICE_NAME}.service"
    }
    echo "✓ Removed systemd unit"
fi
sudo systemctl daemon-reload 2>/dev/null || true

# Remove desktop files
echo
echo "Removing desktop shortcuts..."
if [ -f "$DESKTOP_FILE" ]; then
    rm "$DESKTOP_FILE"
    echo "✓ Removed: $DESKTOP_FILE"
fi

if [ -f "$DESKTOP_SHORTCUT" ]; then
    rm "$DESKTOP_SHORTCUT"
    echo "✓ Removed: $DESKTOP_SHORTCUT"
fi

if command -v update-desktop-database &> /dev/null; then
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
fi

# Remove system configuration files
echo
echo "Removing system configuration files..."

DBUS_POLICY="/etc/dbus-1/system.d/bt-controller-emulator.conf"
if [ -f "$DBUS_POLICY" ]; then
    sudo rm "$DBUS_POLICY" || {
        echo "⚠ Warning: Could not remove D-Bus policy: $DBUS_POLICY"
    }
    echo "✓ Removed D-Bus policy"
fi

UDEV_RULE="/etc/udev/rules.d/99-steamdeck-hidg.rules"
if [ -f "$UDEV_RULE" ]; then
    sudo rm "$UDEV_RULE" || echo "⚠ Warning: Could not remove udev rule"
    sudo udevadm control --reload-rules 2>/dev/null || true
    echo "✓ Removed udev rule for hidg"
fi

# Reload D-Bus
echo
echo "Reloading D-Bus configuration..."
sudo systemctl reload dbus 2>/dev/null || {
    echo "⚠ Warning: Could not reload D-Bus"
}
echo "✓ D-Bus reloaded"

# Reset Bluetooth adapter to default state
echo
echo "Resetting Bluetooth adapter to default state..."
echo "  This ensures normal Bluetooth operation for audio devices, etc."
echo "  - Clearing static BLE address..."
sudo btmgmt --index 0 power off 2>/dev/null || true
sleep 1
sudo btmgmt --index 0 static-addr 00:00:00:00:00:00 2>/dev/null || true
sleep 1
sudo btmgmt --index 0 power on 2>/dev/null || true
echo "  - Resetting adapter properties..."
bluetoothctl discoverable off 2>/dev/null || true
bluetoothctl pairable on 2>/dev/null || true
echo "✓ Bluetooth adapter reset to default state"

# Remove application files
echo
echo "Removing application files..."
if [ -d "$INSTALL_DIR" ]; then
    CURRENT_DIR="$(pwd)"
    if [[ "$CURRENT_DIR" == "$INSTALL_DIR"* ]]; then
        cd "$HOME"
        echo "Changed directory to $HOME (was inside install directory)"
    fi

    rm -rf "$INSTALL_DIR"
    echo "✓ Removed: $INSTALL_DIR"
else
    echo "✓ Directory already removed or not found"
fi

echo
echo "=== Uninstall Complete ==="
echo
echo "BT Controller Emulator has been uninstalled."
echo "Your Bluetooth adapter has been reset to default state."
echo
