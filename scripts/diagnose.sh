#!/bin/bash
# Full diagnostic for BT Controller Emulator on SteamOS (daemon architecture)
set -u

echo "=== BT Controller Emulator — diagnostics ==="
echo "User: $(whoami)  Groups: $(groups)"
echo

echo "--- Daemon (systemd) ---"
if systemctl is-active --quiet bt-controller-emulator; then
    echo "  OK bt-controller-emulator.service is active"
else
    echo "  NOT RUNNING bt-controller-emulator.service"
    echo "    Start it: sudo systemctl start bt-controller-emulator"
fi
systemctl is-enabled bt-controller-emulator 2>/dev/null | sed 's/^/  enabled: /'
echo
echo "  Recent daemon log:"
journalctl -u bt-controller-emulator -n 15 --no-pager 2>/dev/null | sed 's/^/    /' || echo "    (no journal access)"
echo

echo "--- Daemon D-Bus API ---"
if busctl --system introspect com.steamdeck.btemu /com/steamdeck/btemu 2>/dev/null | head -5 | sed 's/^/  /'; then
    true
else
    echo "  MISSING com.steamdeck.btemu (is the daemon running? re-run install.sh)"
fi
echo

echo "--- Install files ---"
for f in /etc/systemd/system/bt-controller-emulator.service /etc/dbus-1/system.d/bt-controller-emulator.conf /etc/udev/rules.d/99-steamdeck-hidg.rules; do
    if [ -f "$f" ]; then echo "  OK $f"; else echo "  MISSING $f (re-run install.sh)"; fi
done
echo

echo "--- Bluetooth (BlueZ) ---"
if command -v bluetoothctl >/dev/null; then
    bluetoothctl show 2>/dev/null | sed -n '1,12p' | sed 's/^/  /'
else
    echo "  bluetoothctl not found"
fi
echo "  GattManager on hci0:"
busctl --system tree org.bluez 2>/dev/null | grep -i GattManager | head -3 | sed 's/^/    /' || echo "    (check org.bluez)"
echo

echo "--- Gamepad input (evdev) ---"
if [ -x "$(command -v evtest)" ]; then
    echo "  Run: evtest   (pick Valve / Steam Deck / Xbox 360 pad)"
else
    echo "  Install evtest: sudo pacman -S evtest"
fi
python3 - <<'PY' 2>/dev/null | sed 's/^/  /'
import evdev
from evdev import ecodes
found = False
for p in sorted(evdev.list_devices()):
    try:
        d = evdev.InputDevice(p)
    except Exception:
        continue
    keys = d.capabilities().get(ecodes.EV_KEY, [])
    if ecodes.BTN_SOUTH in keys and ecodes.BTN_EAST in keys:
        found = True
        print(f"{p}: {d.name!r} vid={d.info.vendor:#06x} pid={d.info.product:#06x}")
        d.close()
if not found:
    print("No gamepad evdev nodes found (Steam may be holding the Deck controls).")
PY
echo

echo "--- USB gadget ---"
bash "$(dirname "$0")/check-usb-gadget.sh" 2>/dev/null || true

echo
echo "If Bluetooth fails: check 'journalctl -u bt-controller-emulator -n 100',"
echo "then 'sudo systemctl restart bluetooth' and start the service again."
echo "If USB fails: BIOS DRD + data cable + 'sudo systemctl restart bt-controller-emulator'."
