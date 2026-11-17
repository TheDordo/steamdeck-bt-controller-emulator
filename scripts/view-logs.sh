#!/bin/bash
# View the emulator daemon's journal log

if [ "$1" == "-f" ] || [ "$1" == "--follow" ]; then
    echo "Following daemon log (Ctrl+C to stop)..."
    journalctl -u bt-controller-emulator -f
else
    echo "=== bt-controller-emulator daemon log (last 100 lines) ==="
    journalctl -u bt-controller-emulator -n 100 --no-pager
    echo
    echo "To follow live logs, run: $0 --follow"
fi
