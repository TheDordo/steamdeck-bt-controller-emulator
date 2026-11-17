#!/bin/bash
# Launch BT Controller Emulator GUI (unprivileged client of the root daemon).
# Install/update: curl -fsSL https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/install.sh

# Go to project root (parent of scripts directory)
cd "$(dirname "$0")/.."

# Check if running as root
if [ "$EUID" -eq 0 ]; then
    echo "Error: Don't run as root"
    echo "The GUI is an unprivileged client; the root daemon does the work."
    exit 1
fi

# Make sure the daemon is running so Start Service works right away.
if ! systemctl is-active --quiet bt-controller-emulator; then
    echo "The emulator daemon is not running."
    echo "Start it with:  sudo systemctl start bt-controller-emulator"
    echo "Continuing anyway — the GUI will show the daemon status."
fi

echo "Launching BT Controller Emulator..."
python3 -m src.hogp.gui
