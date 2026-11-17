#!/bin/bash
# One-line installer for BT Controller Emulator
# Usage: curl -fsSL https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/install.sh | bash
#
# Architecture: one root systemd daemon does all privileged work (BlueZ
# registration, btmgmt, L2CAP PSMs, evdev, USB gadget). The GUI/CLI are
# unprivileged D-Bus clients. Sudo is needed ONCE here at install time —
# never at runtime, no group memberships, no sudoers, no setcap helpers.

set -euo pipefail

REPO_URL="https://github.com/TheDordo/steamdeck-bt-controller-emulator.git"
INSTALL_DIR="$HOME/steamdeck-bt-controller-emulator"
INSTALL_SCRIPT_URL="https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/install.sh"
UNINSTALL_SCRIPT_URL="https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/uninstall.sh"

SERVICE_NAME="bt-controller-emulator"

echo "=== BT Controller Emulator - Installer ==="
echo

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "Error: Don't run as root. Run as your regular user."
    exit 1
fi

# Clone or update repository
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR"
    git remote set-url origin "$REPO_URL"
    if git diff --quiet && git diff --cached --quiet && [ -z "$(git ls-files --others --exclude-standard)" ]; then
        git pull --rebase origin main
    else
        echo "⚠ Local changes detected — skipping the git update so your work"
        echo "  is not stashed away. Your current local files (including"
        echo "  config/) are what get installed below."
    fi
else
    echo "Installing to $INSTALL_DIR..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# Verify source files
if [ ! -f "$INSTALL_DIR/src/hogp/service.py" ] || [ ! -f "$INSTALL_DIR/src/hogp/gui.py" ]; then
    echo "Error: Installation failed - source files not found"
    exit 1
fi

# Check Python dependencies (GUI + daemon)
echo
echo "Checking dependencies..."
DEPS_OK=true

python3 -c "import gi" 2>/dev/null || DEPS_OK=false
python3 -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk" 2>/dev/null || DEPS_OK=false
python3 -c "import evdev" 2>/dev/null || DEPS_OK=false

if [ "$DEPS_OK" = false ]; then
    echo "Error: Missing Python dependencies (python-gobject, gtk4, python-evdev)"
    echo "These should be pre-installed on SteamOS 3.x"
    exit 1
fi
echo "✓ Dependencies OK"

# Install D-Bus policy
echo
echo "=== Installing D-Bus policy ==="
sudo mkdir -p /etc/dbus-1/system.d
sudo cp "$INSTALL_DIR/config/bt-controller-emulator-dbus.conf" /etc/dbus-1/system.d/bt-controller-emulator.conf || {
    echo "✗ Failed to install D-Bus policy"
    exit 1
}
echo "✓ D-Bus policy installed"

# Install + enable the systemd daemon
echo
echo "=== Installing systemd daemon ==="
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null << EOF
# BT Controller Emulator — root daemon (all privileged work lives here)
[Unit]
Description=Steam Deck BT Controller Emulator daemon
Documentation=https://github.com/TheDordo/steamdeck-bt-controller-emulator
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}/src
ExecStart=/usr/bin/python3 -m hogp.service
Restart=on-failure
RestartSec=2
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload || {
    echo "✗ systemctl daemon-reload failed"
    exit 1
}

# udev: /dev/hidg* permissions (USB gadget mode)
echo
echo "Installing udev rule for USB gadget HID nodes..."
sudo cp "$INSTALL_DIR/config/99-steamdeck-hidg.rules" /etc/udev/rules.d/99-steamdeck-hidg.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
echo "✓ udev rule installed"

# Reload D-Bus
echo "Reloading D-Bus..."
sudo systemctl reload dbus 2>/dev/null || {
    echo "⚠ Warning: Could not reload D-Bus (may need reboot)"
}
echo "✓ D-Bus reloaded"

# Start the daemon now (also enabled at boot)
echo
echo "Starting the daemon..."
sudo systemctl enable ${SERVICE_NAME} 2>/dev/null || true
if ! sudo systemctl restart ${SERVICE_NAME}; then
    echo "✗ Could not start the daemon. Check: journalctl -u ${SERVICE_NAME} -n 50"
    exit 1
fi
echo "✓ Daemon running (enabled at boot)"

# Create desktop launcher
echo
echo "Creating launcher..."
mkdir -p "$HOME/.local/share/applications"
mkdir -p "$HOME/Desktop"

DESKTOP_FILE="$HOME/.local/share/applications/bt-controller-emulator.desktop"
DESKTOP_SHORTCUT="$HOME/Desktop/bt-controller-emulator.desktop"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=BT Controller Emulator
Comment=Bluetooth/USB HID Controller Emulator for Steam Deck
Icon=input-gaming
Exec=$INSTALL_DIR/scripts/run-gui.sh
Terminal=false
Categories=Game;Utility;
Keywords=bluetooth;controller;gamepad;hid;usb;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"
cp "$DESKTOP_FILE" "$DESKTOP_SHORTCUT"
chmod +x "$DESKTOP_SHORTCUT"
echo "✓ Desktop launcher created"

chmod +x "$INSTALL_DIR/scripts/"*.sh 2>/dev/null || true

update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo
echo "=== Installation Complete ==="
echo
echo "No logout required — the daemon is already running."
echo
echo "Launch: Application menu → BT Controller Emulator   (or: $INSTALL_DIR/scripts/run-gui.sh)"
echo
echo "Quick start:"
echo "  1. Open the app in Desktop Mode"
echo "  2. Pick Bluetooth or Wired USB"
echo "  3. Start Service → connect from the other device"
echo
echo "Bluetooth just works now: the daemon configures the adapter, registers"
echo "with BlueZ, binds the HID channels and auto-recovers if bluetoothd restarts."
echo "Wired USB needs the BIOS DRD setting + a data cable (one-time)."
echo
echo "Diagnostics:  $INSTALL_DIR/scripts/diagnose.sh"
echo "Logs:         journalctl -u $SERVICE_NAME -f"
echo
echo "Update:       curl -fsSL $INSTALL_SCRIPT_URL | bash"
echo "Uninstall:    curl -fsSL $UNINSTALL_SCRIPT_URL | bash"
echo
