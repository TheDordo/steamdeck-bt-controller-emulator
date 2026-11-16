#!/bin/bash
# Full diagnostic for BT Controller Emulator on SteamOS
set -u

INSTALL_DIR="${INSTALL_DIR:-$HOME/steamdeck-bt-controller-emulator}"

echo "=== BT Controller Emulator — diagnostics ==="
echo "User: $(whoami)  Groups: $(groups)"
echo

echo "--- Permissions / install ---"
for f in /etc/sudoers.d/bt-controller-emulator /etc/dbus-1/system.d/bt-controller-emulator.conf; do
    if [ -f "$f" ]; then echo "  OK $f"; else echo "  MISSING $f (re-run install.sh)"; fi
done
if groups | grep -qw bluetooth; then echo "  OK bluetooth group"; else echo "  MISSING bluetooth group"; fi
if groups | grep -qw input; then echo "  OK input group"; else echo "  MISSING input group"; fi
echo

echo "--- Pairing agent (optional) ---"
if busctl --system status org.bluez 2>/dev/null | grep -q org.bluez; then
    busctl --system tree org.bluez 2>/dev/null | grep -i steamdeck | sed 's/^/  /' || echo "  (no com.steamdeck paths visible — start the app first)"
fi
echo

echo "--- Bluetooth (BlueZ) ---"
if command -v bluetoothctl >/dev/null; then
    bluetoothctl show 2>/dev/null | sed -n '1,12p' | sed 's/^/  /'
else
    echo "  bluetoothctl not found"
fi
if busctl --system call org.bluez /org/bluez/hci0 org.bluez.GattManager1 GetManagedObjects 2>/dev/null | head -1; then
    : # noop
fi
echo "  GattManager on hci0:"
if busctl --system tree org.bluez 2>/dev/null | grep -i GattManager | head -3 | sed 's/^/    /'; then
    true
else
    echo "    (check org.bluez)"
fi
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
bash "$INSTALL_DIR/scripts/check-usb-gadget.sh" 2>/dev/null || true

echo
echo "If Bluetooth hangs: log out/in after install, Desktop Mode, re-run install from your fork."
echo "If USB fails: BIOS DRD + data cable + passwd for sudo/pkexec."