"""
Unprivileged D-Bus client for the emulator daemon (com.steamdeck.btemu).

Used by the GTK4 GUI and the CLI. Never touches BlueZ, btmgmt, L2CAP or
/dev/input directly — everything goes through the root daemon.
"""

import logging
from typing import Callable, Dict, Optional

from gi.repository import Gio, GLib

from .api import (
    BUS_NAME, OBJECT_PATH, MANAGER_IFACE, ERROR_FAILED,
    STATUS_STOPPED, STATUS_STARTING, STATUS_STOPPING, STATUS_ACTIVE,
    STATUS_ERROR, STATUS_DEGRADED,
    MODE_BLUETOOTH, MODE_USB, PROFILE_GAMEPAD, PROFILE_KEYBOARD,
    KEY_STATUS, KEY_MODE, KEY_PROFILE, KEY_DETAIL, KEY_ERROR, KEY_CONN,
    KEY_CONNECTED, KEY_PAIRED, KEY_TRUSTED, KEY_INPUT, KEY_BATTERY,
    KEY_DEVICE_NAME, KEY_DEVICE_ADDR, KEY_READY, KEY_KB_CAPABLE,
    KEY_BUTTONS, KEY_AXES, KEY_TRIGGERS, KEY_HAT,
)

logger = logging.getLogger(__name__)

CALL_TIMEOUT_MS = 15000


class BtEmuError(Exception):
    """Start/Stop failure reported by the daemon (message is user-facing)."""


def _unpack_variant(variant: GLib.Variant) -> object:
    """Unpack one GLib.Variant leaf (for a{sv} status dicts)."""
    type_string = variant.get_type_string()
    if type_string == "s":
        return variant.get_string()
    if type_string == "b":
        return variant.get_boolean()
    if type_string in ("y", "n", "q", "u", "i", "x", "t", "h", "d"):
        return variant.unpack()
    if type_string == "ai":
        return list(variant.unpack())
    try:
        return variant.unpack()
    except Exception:
        return None


class BtEmuClient:
    """Synchronous D-Bus client for com.steamdeck.btemu.Manager1."""

    def __init__(self) -> None:
        self.bus: Gio.DBusConnection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self._sub_ids = []

    # ------------------------------------------------------------- plumbing

    def _call(self, method: str, params: Optional[GLib.Variant] = None,
              out_type: Optional[str] = None, timeout_ms: int = CALL_TIMEOUT_MS) -> Optional[GLib.Variant]:
        try:
            return self.bus.call_sync(
                BUS_NAME,
                OBJECT_PATH,
                MANAGER_IFACE,
                method,
                params,
                GLib.VariantType(out_type) if out_type else None,
                Gio.DBusCallFlags.NONE,
                timeout_ms,
                None,
            )
        except GLib.Error as e:
            message = e.message or str(e)
            if ERROR_FAILED in message:
                # "GDBus.Error:com.steamdeck.btemu.Error.Failed: <reason>"
                # — keep the full reason (it may itself contain colons).
                parts = message.split(":", 2)
                if len(parts) > 2:
                    message = parts[2].strip()
                raise BtEmuError(message) from e
            if "GDBus.Error:org.freedesktop.DBus.Error.ServiceUnknown" in message:
                raise BtEmuError(
                    "The emulator daemon is not running. Start it with:\n"
                    "  sudo systemctl start bt-controller-emulator"
                ) from e
            if "NameHasNoOwner" in message or "NoReply" in message:
                raise BtEmuError(
                    "The emulator daemon is not responding. Try:\n"
                    "  sudo systemctl restart bt-controller-emulator"
                ) from e
            raise BtEmuError(message) from e

    def daemon_running(self) -> bool:
        try:
            result = self.bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (BUS_NAME,)),
                GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return result.get_child_value(0).get_boolean()
        except GLib.Error:
            return False

    # ------------------------------------------------------------ lifecycle

    def start(self, mode: str = MODE_BLUETOOTH, profile: str = PROFILE_KEYBOARD) -> None:
        self._call("Start", GLib.Variant("(ss)", (mode, profile)))

    def stop(self) -> None:
        self._call("Stop")

    def get_status(self) -> Dict[str, object]:
        result = self._call("GetStatus", None, "(a{sv})")
        status: Dict[str, object] = {}
        entries = result.get_child_value(0)
        for i in range(entries.n_children()):
            entry = entries.get_child_value(i)
            key = entry.get_child_value(0).get_string()
            value = _unpack_variant(entry.get_child_value(1))
            status[key] = value
        return status

    def subscribe_status(self, callback: Callable[[str], None]) -> int:
        """Subscribe to StatusChanged; returns the subscription id."""
        return self.bus.signal_subscribe(
            BUS_NAME,
            MANAGER_IFACE,
            "StatusChanged",
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda conn, sender, path, iface, signal, params: callback(params.unpack()[0]),
            None,
        )

    def unsubscribe_status(self, sub_id: int) -> None:
        try:
            self.bus.signal_unsubscribe(sub_id)
        except Exception:
            pass

    # ------------------------------------------------------------ HID input

    def send_key(self, code: int, modifiers: int = 0, hold_ms: int = 140) -> None:
        self._call("SendKey", GLib.Variant("(uuu)", (code, modifiers, hold_ms)))

    def send_mouse(self, dx: int, dy: int, buttons: int = 0, wheel: int = 0) -> None:
        self._call("SendMouse", GLib.Variant("(iiui)", (dx, dy, buttons, wheel)))

    def set_button(self, index: int, pressed: bool) -> None:
        self._call("SetButton", GLib.Variant("(ub)", (index, pressed)))

    def set_axis(self, index: int, value: int) -> None:
        self._call("SetAxis", GLib.Variant("(ui)", (index, value)))

    def set_trigger(self, index: int, value: int) -> None:
        self._call("SetTrigger", GLib.Variant("(uy)", (index, value)))

    def set_hat(self, direction: int) -> None:
        self._call("SetHat", GLib.Variant("(y)", (direction,)))
