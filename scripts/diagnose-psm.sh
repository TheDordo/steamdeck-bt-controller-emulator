#!/bin/bash
# Who holds the HID L2CAP PSMs (0x11 control / 0x13 interrupt)?
echo "=== processes that may hold the PSMs ==="
pgrep -af 'hogp|hid-l2cap' || echo "(none)"
echo
echo "=== L2CAP listeners on 0x11/0x13 ==="
grep -E '0011|0013' /proc/net/l2cap || echo "(none listed)"
echo
echo "=== ss with PIDs (if supported) ==="
ss -f bluetooth -lpn 2>/dev/null | grep -E 'psm| 17 | 19 |0x11|0x13' || echo "(ss -f bluetooth unsupported)"
echo
echo "=== hidp kernel module ==="
lsmod | grep hidp || echo "hidp not loaded"
echo
echo "=== Bluetooth devices connected to this Deck ==="
bluetoothctl devices Connected 2>/dev/null || echo "(bluetoothctl unavailable)"
echo
echo "---"
echo "If hidp is loaded and devices are connected above, a Bluetooth"
echo "keyboard/mouse session on the Deck holds the ports:"
echo "    bluetoothctl disconnect <MAC>"
echo "    sudo modprobe -r hidp"
echo "If a userspace process shows up instead:"
echo "    sudo pkill -f 'hid-l2cap-bridg[e]'   (orphaned bridge)"
echo "    pkill -f 'hogp'                       (stray app instance)"