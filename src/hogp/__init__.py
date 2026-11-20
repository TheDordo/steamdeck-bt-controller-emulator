"""
Steam Deck BT Controller Emulator (SteamOS 3.x "holo").

Turns a Steam Deck into a Bluetooth or wired-USB gamepad/keyboard/trackpad
peripheral:

    * Bluetooth + Gamepad profile  -> Nintendo Switch Pro Controller
      (BR/EDR HID over L2CAP; macOS GameController-native)
    * Bluetooth + Keyboard profile -> BLE HID-over-GATT gamepad/keyboard/
      mouse ("SteamDeckPad")
    * Wired USB + Xbox profile     -> Xbox Series X|S (045E:0B12, GIP/FunctionFS)
    * Wired USB + DS4 profile      -> DUALSHOCK 4 identity (054C:05C4)
    * Wired USB + Gamepad profile  -> Switch Pro Controller (057E:2009)
    * Wired USB + Keyboard profile -> generic HID gamepad/keyboard/mouse

Architecture
------------
A root systemd daemon (bt-controller-emulator.service, src/hogp/service.py)
owns every privileged operation — BlueZ GATT/advertisement/agent/SDP
registration, btmgmt adapter configuration, the HID L2CAP PSMs, evdev input
and the USB gadget. The GTK4 GUI (gui.py) and the CLI (main.py) are
unprivileged D-Bus clients (com.steamdeck.btemu, see api.py / client.py).
"""

__version__ = "2.0.0"
