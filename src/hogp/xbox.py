#!/usr/bin/env python3
"""
Xbox Wireless Controller (Series X|S, Bluetooth LE) emulation over HID-over-GATT.

Why this exists
---------------
macOS's GameController framework (and Steam / SDL on the Mac) natively drive
Xbox Wireless Controllers over Bluetooth LE. Unlike the Switch Pro
Controller path this needs no classic-Bluetooth L2CAP ports and no
system-wide bluetoothd Device ID: over LE the host reads the VID/PID from
the PnP ID characteristic of OUR Device Information Service.

Wire format (verified against SDL's SDL_hidapi_xboxone.c Bluetooth parser,
which Steam uses; Series X firmware 5.x, 17-byte report incl. report ID):

    byte  0     report ID 0x01 (NOT part of the GATT characteristic value)
    1..2  LX    uint16 LE, 0..65535, center 0x8000
    3..4  LY    uint16 LE, grows DOWNWARD (same as evdev)
    5..6  RX
    7..8  RY
    9..10 LT    uint16 LE, 0..1023
    11..12 RT
    13    hat   0 = centered, 1 = N, 2 = NE ... 8 = NW (clockwise)
    14    A 0x01, B 0x02, X 0x08, Y 0x10, LB 0x40, RB 0x80
    15    View 0x04, Menu 0x08, Xbox 0x10, LS 0x20, RS 0x40
    16    Share 0x01

Rumble (host -> device): output report 0x03, 8 bytes after the report ID:
    [enable mask, LT, RT, strong(left), weak(right), duration*10ms,
     start delay*10ms, loop count] — magnitudes 0..100.
SDL sends 03 0F lt rt lo hi FF 00 EB.
"""

import logging
import struct
import time
from typing import Any, Callable, Dict, List, Optional

from gi.repository import Gio, GLib

from .battery import read_battery_percent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
XBOX_NAME = "Xbox Wireless Controller"
XBOX_VENDOR_ID = 0x045E    # Microsoft
XBOX_PRODUCT_ID = 0x0B13   # Xbox Series X|S controller, BLE
XBOX_VERSION = 0x0509      # firmware 5.x family (17-byte report)
XBOX_APPEARANCE = 0x03C4   # BLE Appearance: HID Gamepad

# PnP ID: vendor ID source 0x02 (USB-IF), VID, PID, version (all LE)
XBOX_PNP_ID = struct.pack("<BHHH", 0x02, XBOX_VENDOR_ID, XBOX_PRODUCT_ID, XBOX_VERSION)

# Fixed GATT service handles, far above BlueZ's own GAP/GATT/DIS services
# (which sit at the bottom of the table) with room to grow.
HID_SERVICE_HANDLE = 0x0100
DIS_SERVICE_HANDLE = 0x0180
BATTERY_SERVICE_HANDLE = 0x01C0

INPUT_REPORT_ID = 0x01
RUMBLE_REPORT_ID = 0x03
INPUT_REPORT_LEN = 16      # GATT value length (without the report ID)
RUMBLE_REPORT_LEN = 8

# ATT notifications: event-driven, latest-state-wins, with a floor matched
# to the requested LE connection interval (11.25-15 ms, see connparams.py).
# Sending faster than the link drains only queues STALE reports in the
# kernel/controller buffers, and every new input then waits behind them.
NOTIFY_MIN_INTERVAL_MS = 11
KEEPALIVE_INTERVAL_MS = 100

# fmt: off
XBOX_REPORT_MAP = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Game Pad)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID (1)
    # Left stick X/Y — 16-bit unsigned
    0x09, 0x01, 0xA1, 0x00,
    0x09, 0x30, 0x09, 0x31,
    0x15, 0x00, 0x27, 0xFF, 0xFF, 0x00, 0x00,
    0x95, 0x02, 0x75, 0x10, 0x81, 0x02,
    0xC0,
    # Right stick Z/Rz — 16-bit unsigned
    0x09, 0x01, 0xA1, 0x00,
    0x09, 0x32, 0x09, 0x35,
    0x15, 0x00, 0x27, 0xFF, 0xFF, 0x00, 0x00,
    0x95, 0x02, 0x75, 0x10, 0x81, 0x02,
    0xC0,
    # Left trigger (Brake) 10 bits + 6 pad
    0x05, 0x02, 0x09, 0xC5,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x95, 0x01, 0x75, 0x0A, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x06, 0x95, 0x01, 0x81, 0x03,
    # Right trigger (Accelerator) 10 bits + 6 pad
    0x05, 0x02, 0x09, 0xC4,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x95, 0x01, 0x75, 0x0A, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x06, 0x95, 0x01, 0x81, 0x03,
    # Hat switch 1..8 (null state 0) + 4 pad
    0x05, 0x01, 0x09, 0x39,
    0x15, 0x01, 0x25, 0x08, 0x35, 0x00, 0x46, 0x3B, 0x01, 0x66, 0x14, 0x00,
    0x75, 0x04, 0x95, 0x01, 0x81, 0x42,
    0x75, 0x04, 0x95, 0x01, 0x15, 0x00, 0x25, 0x00, 0x35, 0x00, 0x45, 0x00,
    0x65, 0x00, 0x81, 0x03,
    # 15 buttons + 1 pad
    0x05, 0x09, 0x19, 0x01, 0x29, 0x0F,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x0F, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x01, 0x95, 0x01, 0x81, 0x03,
    # Share (Consumer: Record) + 7 pad
    0x05, 0x0C, 0x0A, 0xB2, 0x00,
    0x15, 0x00, 0x25, 0x01, 0x95, 0x01, 0x75, 0x01, 0x81, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x07, 0x95, 0x01, 0x81, 0x03,
    # Rumble: PID Set Effect output report 3
    0x05, 0x0F, 0x09, 0x21, 0x85, 0x03, 0xA1, 0x02,
    0x09, 0x97, 0x15, 0x00, 0x25, 0x01, 0x75, 0x04, 0x95, 0x01, 0x91, 0x02,
    0x15, 0x00, 0x25, 0x00, 0x75, 0x04, 0x95, 0x01, 0x91, 0x03,
    0x09, 0x70, 0x15, 0x00, 0x25, 0x64, 0x75, 0x08, 0x95, 0x04, 0x91, 0x02,
    0x09, 0x50, 0x66, 0x01, 0x10, 0x55, 0x0E,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x01, 0x91, 0x02,
    0x09, 0xA7, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x01, 0x91, 0x02,
    0x65, 0x00, 0x55, 0x00,
    0x09, 0x7C, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x01, 0x91, 0x02,
    0xC0,
    0xC0,              # End Collection
])
# fmt: on

# HID Information: bcdHID 1.11, country 0, flags 0x01 (remote wake)
HID_INFO = bytes([0x11, 0x01, 0x00, 0x01])

# ---------------------------------------------------------------------------
# GATT constants
# ---------------------------------------------------------------------------
def _uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"

HID_SERVICE_UUID = _uuid16(0x1812)
HID_INFO_UUID = _uuid16(0x2A4A)
REPORT_MAP_UUID = _uuid16(0x2A4B)
HID_CONTROL_POINT_UUID = _uuid16(0x2A4C)
REPORT_UUID = _uuid16(0x2A4D)
PROTOCOL_MODE_UUID = _uuid16(0x2A4E)
REPORT_REFERENCE_UUID = _uuid16(0x2908)
DEVICE_INFO_SERVICE_UUID = _uuid16(0x180A)
MANUFACTURER_NAME_UUID = _uuid16(0x2A29)
MODEL_NUMBER_UUID = _uuid16(0x2A24)
PNP_ID_UUID = _uuid16(0x2A50)
BATTERY_SERVICE_UUID = _uuid16(0x180F)
BATTERY_LEVEL_UUID = _uuid16(0x2A19)

GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHAR_IFACE = "org.bluez.GattCharacteristic1"
GATT_DESC_IFACE = "org.bluez.GattDescriptor1"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Encrypted HID attributes make macOS bond (same posture as gatt_app.py).
ENC_READ = ["read", "encrypt-read"]
INPUT_REPORT_FLAGS = ["read", "notify", "encrypt-read", "encrypt-notify"]
OUTPUT_REPORT_FLAGS = ["read", "write", "write-without-response", "encrypt-read", "encrypt-write"]
CONTROL_POINT_FLAGS = ["write-without-response", "encrypt-write"]
PROTOCOL_MODE_FLAGS = ["read", "write-without-response"]
BATTERY_FLAGS = ["read", "notify"]

_CHAR_XML = f"""
<node>
  <interface name="{GATT_CHAR_IFACE}">
    <method name="ReadValue"><arg type="a{{sv}}" direction="in"/><arg type="ay" direction="out"/></method>
    <method name="WriteValue"><arg type="ay" direction="in"/><arg type="a{{sv}}" direction="in"/></method>
    <method name="StartNotify"/>
    <method name="StopNotify"/>
    <property name="UUID" type="s" access="read"/>
    <property name="Service" type="o" access="read"/>
    <property name="Flags" type="as" access="read"/>
    <property name="Descriptors" type="ao" access="read"/>
    <property name="Handle" type="q" access="readwrite"/>
  </interface>
</node>"""

_DESC_XML = f"""
<node>
  <interface name="{GATT_DESC_IFACE}">
    <method name="ReadValue"><arg type="a{{sv}}" direction="in"/><arg type="ay" direction="out"/></method>
    <property name="UUID" type="s" access="read"/>
    <property name="Characteristic" type="o" access="read"/>
    <property name="Flags" type="as" access="read"/>
    <property name="Handle" type="q" access="readwrite"/>
  </interface>
</node>"""

_SERVICE_XML = f"""
<node>
  <interface name="{GATT_SERVICE_IFACE}">
    <property name="UUID" type="s" access="read"/>
    <property name="Primary" type="b" access="read"/>
    <property name="Characteristics" type="ao" access="read"/>
    <property name="Handle" type="q" access="readwrite"/>
  </interface>
</node>"""

_PROPS_XML = f"""
<node>
  <interface name="{DBUS_PROPS_IFACE}">
    <method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
    <method name="GetAll"><arg type="s" direction="in"/><arg type="a{{sv}}" direction="out"/></method>
    <method name="Set"><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/></method>
    <signal name="PropertiesChanged">
      <arg type="s"/><arg type="a{{sv}}"/><arg type="as"/>
    </signal>
  </interface>
</node>"""

_OM_XML = f"""
<node>
  <interface name="{DBUS_OM_IFACE}">
    <method name="GetManagedObjects"><arg type="a{{oa{{sa{{sv}}}}}}" direction="out"/></method>
  </interface>
</node>"""


class _Attr:
    """One exported GATT object (service, characteristic or descriptor)."""

    def __init__(self, path: str, iface: str, props: Dict[str, GLib.Variant],
                 read: Optional[Callable[[], bytes]] = None,
                 write: Optional[Callable[[bytes], None]] = None,
                 start_notify: Optional[Callable[[], None]] = None,
                 stop_notify: Optional[Callable[[], None]] = None):
        self.path = path
        self.iface = iface
        self.props = props
        self.read = read
        self.write = write
        self.start_notify = start_notify
        self.stop_notify = stop_notify


# ---------------------------------------------------------------------------
# Report building (pure functions — unit-testable without D-Bus)
# ---------------------------------------------------------------------------
def _stick_u16(value: int) -> int:
    """int16 (-32768..32767) -> uint16 (0..65535, center 0x8000)."""
    return max(0, min(0xFFFF, int(value) + 0x8000))


def _trigger_10bit(value: int) -> int:
    """uint8 (0..255) -> 0..1023."""
    return max(0, min(1023, (int(value) * 1023 + 127) // 255))


# Shared state hat (0=N .. 7=NW, 8=center) -> Xbox hat (1=N .. 8=NW, 0=center)
def _xbox_hat(hat: int) -> int:
    return hat + 1 if 0 <= hat <= 7 else 0


# Shared button index -> (byte offset in the GATT value, bit)
# (shared model: 0 A/south, 1 B/east, 2 X, 3 Y, 4 LB, 5 RB, 6 Back,
#  7 Start, 8 Guide, 9 L3, 10 R3)
_BUTTON_BITS = {
    0: (13, 0x01),   # A
    1: (13, 0x02),   # B
    2: (13, 0x08),   # X
    3: (13, 0x10),   # Y
    4: (13, 0x40),   # LB
    5: (13, 0x80),   # RB
    6: (14, 0x04),   # View
    7: (14, 0x08),   # Menu
    8: (14, 0x10),   # Xbox
    9: (14, 0x20),   # LS
    10: (14, 0x40),  # RS
}


def build_input_report(buttons: int, axes: List[int], triggers: List[int], hat: int) -> bytes:
    """16-byte GATT value for input report 0x01 (report ID excluded).

    Offsets are SDL's minus one (SDL's buffer starts with the report ID)."""
    report = bytearray(INPUT_REPORT_LEN)
    struct.pack_into(
        "<HHHHHH", report, 0,
        _stick_u16(axes[0]), _stick_u16(axes[1]),
        _stick_u16(axes[2]), _stick_u16(axes[3]),
        _trigger_10bit(triggers[0]), _trigger_10bit(triggers[1]),
    )
    report[12] = _xbox_hat(hat)
    for index, (offset, bit) in _BUTTON_BITS.items():
        if buttons & (1 << index):
            report[offset] |= bit
    return bytes(report)


def parse_rumble(value: bytes) -> Optional[Dict[str, int]]:
    """Parse a rumble output report (with or without leading report ID).
    Returns magnitudes 0..100 and timing, or None if malformed."""
    data = bytes(value)
    if len(data) == RUMBLE_REPORT_LEN + 1 and data[0] == RUMBLE_REPORT_ID:
        data = data[1:]
    if len(data) < 6:
        return None
    enable = data[0]
    duration = data[5] if len(data) > 5 else 0xFF
    delay = data[6] if len(data) > 6 else 0
    loop = data[7] if len(data) > 7 else 0
    return {
        "left_trigger": data[1] if enable & 0x01 else 0,
        "right_trigger": data[2] if enable & 0x02 else 0,
        "strong": data[3] if enable & 0x04 else 0,
        "weak": data[4] if enable & 0x08 else 0,
        # Total play time: duration * (loops + 1), in ms
        "duration_ms": duration * 10 * (loop + 1),
        "delay_ms": delay * 10,
    }


# ---------------------------------------------------------------------------
# GATT application
# ---------------------------------------------------------------------------
class XboxGattApplication:
    """HoG peripheral that presents itself as an Xbox Wireless Controller.

    Drop-in for GattApplication from the daemon's point of view (same
    APP_PATH, register/unregister, set_* setters, notifying, _on_notify_start)."""

    APP_PATH = "/com/steamdeck/hogp"  # same root as GattApplication: the
    # daemon's (un)registration helpers use one path for both engines.

    def __init__(self, bus: Gio.DBusConnection, verbose: bool = False):
        self.bus = bus
        self.verbose = verbose
        self._registrations: List[int] = []
        self._attrs: Dict[str, _Attr] = {}

        self._buttons = 0
        self._axes = [0, 0, 0, 0]
        self._triggers = [0, 0]
        self._hat = 8
        self._protocol_mode = 0x01
        self._battery_level = 100

        self._notifying = False         # host called StartNotify (status only)
        self._streaming = False         # emit reports (from registration on)
        self._battery_notifying = False
        self._last_report: bytes = build_input_report(0, self._axes, self._triggers, 8)
        self._last_sent_ms = 0
        self._flush_source: Optional[int] = None
        self._batch_source: Optional[int] = None
        self._keepalive_source: Optional[int] = None

        self._on_notify_start: Optional[Callable[[], None]] = None
        self._on_notify_stop: Optional[Callable[[], None]] = None
        # callable(strong 0..1, weak 0..1, duration_ms) — forwarded to the Deck
        self.on_rumble: Optional[Callable[[float, float, int], None]] = None

        self._build_tree()

    # -- shared gamepad state setters ----------------------------------------

    def set_button(self, index: int, pressed: bool) -> None:
        if 0 <= index < 11:
            if pressed:
                self._buttons |= (1 << index)
            else:
                self._buttons &= ~(1 << index)
            self._state_changed()

    def set_axis(self, index: int, value: int) -> None:
        if 0 <= index < 4:
            self._axes[index] = max(-32768, min(32767, value))
            self._state_changed()

    def set_trigger(self, index: int, value: int) -> None:
        if 0 <= index < 2:
            self._triggers[index] = max(0, min(255, value))
            self._state_changed()

    def set_hat(self, direction: int) -> None:
        if direction == 0x0F:
            direction = 8
        self._hat = direction if 0 <= direction <= 8 else 8
        self._state_changed()

    def set_report_rate(self, hz: int) -> None:
        pass  # event-driven; kept for interface compatibility

    def set_battery_level(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if percent != self._battery_level:
            self._battery_level = percent
            if self._streaming or self._battery_notifying:
                self._emit_value(f"{self.APP_PATH}/service2/char0", bytes([percent]))

    # -- tree -----------------------------------------------------------------

    def _build_tree(self) -> None:
        root = self.APP_PATH
        hid = f"{root}/service0"
        dis = f"{root}/service1"
        bat = f"{root}/service2"
        p = lambda *parts: "/".join(parts)

        def svc(path, uuid, chars, handle):
            # Fixed service handles: BlueZ then allocates the characteristics
            # sequentially inside, so the attribute table is IDENTICAL on
            # every registration. macOS caches handles per bonded device and
            # does not reliably re-discover — after a bluetoothd restart
            # (e.g. Steam taking over Bluetooth in Game Mode) shifted handles
            # made it read garbage VID/PID, no battery and no input.
            self._add(_Attr(path, GATT_SERVICE_IFACE, {
                "UUID": GLib.Variant("s", uuid),
                "Primary": GLib.Variant("b", True),
                "Characteristics": GLib.Variant("ao", chars),
                "Handle": GLib.Variant("q", handle),
            }))

        def char(path, service, uuid, flags, descs=(), **handlers):
            self._add(_Attr(path, GATT_CHAR_IFACE, {
                "UUID": GLib.Variant("s", uuid),
                "Service": GLib.Variant("o", service),
                "Flags": GLib.Variant("as", flags),
                "Descriptors": GLib.Variant("ao", list(descs)),
                # 0 = let BlueZ allocate (sequentially inside the pinned
                # service) and write the result back via Properties.Set,
                # which we log so the real attribute table is visible.
                "Handle": GLib.Variant("q", 0),
            }, **handlers))

        def desc(path, char_path, uuid, value):
            self._add(_Attr(path, GATT_DESC_IFACE, {
                "UUID": GLib.Variant("s", uuid),
                "Characteristic": GLib.Variant("o", char_path),
                "Flags": GLib.Variant("as", ENC_READ),
                "Handle": GLib.Variant("q", 0),
            }, read=lambda: value))

        # HID service
        hid_chars = [p(hid, f"char{i}") for i in range(6)]
        svc(hid, HID_SERVICE_UUID, hid_chars, HID_SERVICE_HANDLE)
        char(hid_chars[0], hid, HID_INFO_UUID, ENC_READ, read=lambda: HID_INFO)
        char(hid_chars[1], hid, REPORT_MAP_UUID, ENC_READ, read=lambda: XBOX_REPORT_MAP)
        char(hid_chars[2], hid, HID_CONTROL_POINT_UUID, CONTROL_POINT_FLAGS,
             write=self._on_control_point)
        in_ref = p(hid_chars[3], "desc0")
        char(hid_chars[3], hid, REPORT_UUID, INPUT_REPORT_FLAGS, [in_ref],
             read=lambda: self._last_report,
             start_notify=self._start_report_notify,
             stop_notify=self._stop_report_notify)
        desc(in_ref, hid_chars[3], REPORT_REFERENCE_UUID, bytes([INPUT_REPORT_ID, 0x01]))
        out_ref = p(hid_chars[4], "desc0")
        char(hid_chars[4], hid, REPORT_UUID, OUTPUT_REPORT_FLAGS, [out_ref],
             read=lambda: bytes(RUMBLE_REPORT_LEN),
             write=self._on_rumble_write)
        desc(out_ref, hid_chars[4], REPORT_REFERENCE_UUID, bytes([RUMBLE_REPORT_ID, 0x02]))
        char(hid_chars[5], hid, PROTOCOL_MODE_UUID, PROTOCOL_MODE_FLAGS,
             read=lambda: bytes([self._protocol_mode]),
             write=self._on_protocol_mode)

        # Device Information service — the PnP ID here is what makes the
        # host load its Xbox driver.
        dis_chars = [p(dis, f"char{i}") for i in range(3)]
        svc(dis, DEVICE_INFO_SERVICE_UUID, dis_chars, DIS_SERVICE_HANDLE)
        char(dis_chars[0], dis, MANUFACTURER_NAME_UUID, ["read"], read=lambda: b"Microsoft")
        char(dis_chars[1], dis, MODEL_NUMBER_UUID, ["read"], read=lambda: b"1914")
        char(dis_chars[2], dis, PNP_ID_UUID, ["read"], read=lambda: XBOX_PNP_ID)

        # Battery service (Deck battery)
        bat_char = p(bat, "char0")
        svc(bat, BATTERY_SERVICE_UUID, [bat_char], BATTERY_SERVICE_HANDLE)
        char(bat_char, bat, BATTERY_LEVEL_UUID, BATTERY_FLAGS,
             read=lambda: bytes([self._battery_level]),
             start_notify=lambda: setattr(self, "_battery_notifying", True),
             stop_notify=lambda: setattr(self, "_battery_notifying", False))

    def _add(self, attr: _Attr) -> None:
        self._attrs[attr.path] = attr

    # -- D-Bus registration ---------------------------------------------------

    def register(self) -> bool:
        try:
            self._battery_level = read_battery_percent()
        except Exception:
            pass
        try:
            om = Gio.DBusNodeInfo.new_for_xml(_OM_XML).interfaces[0]
            self._registrations.append(self.bus.register_object(
                self.APP_PATH, om, self._handle_om, None, None))
            ifaces = {
                GATT_SERVICE_IFACE: Gio.DBusNodeInfo.new_for_xml(_SERVICE_XML).interfaces[0],
                GATT_CHAR_IFACE: Gio.DBusNodeInfo.new_for_xml(_CHAR_XML).interfaces[0],
                GATT_DESC_IFACE: Gio.DBusNodeInfo.new_for_xml(_DESC_XML).interfaces[0],
            }
            props_iface = Gio.DBusNodeInfo.new_for_xml(_PROPS_XML).interfaces[0]
            for attr in self._attrs.values():
                self._registrations.append(self.bus.register_object(
                    attr.path, ifaces[attr.iface], self._handle_call,
                    self._handle_get_property, None))
                self._registrations.append(self.bus.register_object(
                    attr.path, props_iface, self._handle_call, None, None))
            logger.info(
                "Xbox GATT objects registered under %s (pinned service handles "
                "HID 0x%04x, DIS 0x%04x, Battery 0x%04x — the per-attribute "
                "handle lines that follow come from BlueZ)",
                self.APP_PATH, HID_SERVICE_HANDLE, DIS_SERVICE_HANDLE, BATTERY_SERVICE_HANDLE,
            )
            # Stream from the start instead of waiting for StartNotify: a
            # bonded host does NOT re-write the CCC on reconnect (the spec
            # says the server persists it), and BlueZ keeps that CCC state
            # but never re-calls StartNotify on an app that re-registered
            # (daemon restart, Steam restarting bluetoothd in Game Mode).
            # Gating on StartNotify left such hosts connected but silent.
            # BlueZ only forwards notifications to subscribed clients, so
            # emitting with nobody subscribed costs one D-Bus signal.
            self._streaming = True
            self._ensure_keepalive()
            return True
        except Exception as e:
            logger.error("Failed to register Xbox GATT objects: %s", e)
            self.unregister()
            return False

    def unregister(self) -> None:
        self.stop_notify()
        for reg_id in self._registrations:
            try:
                self.bus.unregister_object(reg_id)
            except Exception as e:
                logger.debug("unregister_object %s: %s", reg_id, e)
        self._registrations.clear()

    def _handle_om(self, conn, sender, path, iface, method, params, invoc) -> None:
        if method != "GetManagedObjects":
            invoc.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
            return
        objects = {a.path: {a.iface: a.props} for a in self._attrs.values()}
        invoc.return_value(GLib.Variant("(a{oa{sa{sv}}})", (objects,)))

    def _handle_get_property(self, conn, sender, path, iface, prop):
        attr = self._attrs.get(path)
        return attr.props.get(prop) if attr else None

    def _handle_call(self, conn, sender, path, iface, method, params, invoc) -> None:
        attr = self._attrs.get(path)
        if attr is None:
            invoc.return_dbus_error("org.freedesktop.DBus.Error.UnknownObject", path)
            return
        try:
            if iface == DBUS_PROPS_IFACE:
                if method == "Get":
                    iface_name, prop = params.unpack()
                    if iface_name == attr.iface and prop in attr.props:
                        invoc.return_value(GLib.Variant("(v)", (attr.props[prop],)))
                    else:
                        invoc.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", prop)
                elif method == "GetAll":
                    invoc.return_value(GLib.Variant("(a{sv})", (attr.props,)))
                elif method == "Set":
                    _iface_name, prop, value = params.unpack()
                    if prop == "Handle":
                        handle = int(value)
                        attr.props["Handle"] = GLib.Variant("q", handle)
                        uuid = attr.props["UUID"].unpack()
                        logger.info("Xbox GATT: %s %s at handle 0x%04x",
                                    path.rsplit("/", 2)[-2:], uuid[4:8], handle)
                    invoc.return_value(None)
                else:
                    invoc.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
                return
            if method == "ReadValue" and attr.read is not None:
                invoc.return_value(GLib.Variant("(ay)", (bytes(attr.read()),)))
            elif method == "WriteValue" and attr.write is not None:
                value, _opts = params.unpack()
                attr.write(bytes(value))
                invoc.return_value(None)
            elif method == "StartNotify" and attr.start_notify is not None:
                attr.start_notify()
                invoc.return_value(None)
            elif method == "StopNotify" and attr.stop_notify is not None:
                attr.stop_notify()
                invoc.return_value(None)
            else:
                invoc.return_dbus_error("org.bluez.Error.NotSupported", f"{method} on {path}")
        except Exception as e:
            logger.warning("GATT %s on %s failed: %s", method, path, e)
            invoc.return_dbus_error("org.bluez.Error.Failed", str(e))

    # -- characteristic handlers ----------------------------------------------

    def _on_control_point(self, value: bytes) -> None:
        # 0x00 = Suspend, 0x01 = Exit Suspend
        logger.info("Xbox HID control point: %s", value.hex())

    def _on_protocol_mode(self, value: bytes) -> None:
        if value and value[0] in (0, 1):
            self._protocol_mode = value[0]

    def _on_rumble_write(self, value: bytes) -> None:
        rumble = parse_rumble(value)
        if rumble is None:
            logger.debug("Malformed rumble report: %s", value.hex())
            return
        if self.verbose:
            logger.debug("Rumble: %s", rumble)
        if self.on_rumble is not None:
            # Trigger motors have no Deck equivalent: fold them into the
            # nearest grip motor so trigger effects are still felt.
            strong = max(rumble["strong"], rumble["left_trigger"]) / 100.0
            weak = max(rumble["weak"], rumble["right_trigger"]) / 100.0
            try:
                self.on_rumble(min(strong, 1.0), min(weak, 1.0), rumble["duration_ms"])
            except Exception as e:
                logger.debug("rumble callback: %s", e)

    # -- notifications ----------------------------------------------------------

    def _start_report_notify(self) -> None:
        logger.info("Xbox: host subscribed to input reports")
        self._notifying = True
        self._streaming = True
        self._send_report(force=True)
        self._ensure_keepalive()
        if self._on_notify_start:
            self._on_notify_start()

    def _stop_report_notify(self) -> None:
        # Keep streaming: BlueZ calls StopNotify when the LAST subscriber
        # leaves, but a bonded host's CCC state survives disconnects and it
        # will expect reports again on reconnect without re-subscribing.
        logger.info("Xbox: host unsubscribed from input reports")
        self._notifying = False
        if self._on_notify_stop:
            self._on_notify_stop()

    def _ensure_keepalive(self) -> None:
        if self._keepalive_source is None:
            self._keepalive_source = GLib.timeout_add(KEEPALIVE_INTERVAL_MS, self._keepalive)

    def stop_notify(self) -> None:
        self._notifying = False
        self._streaming = False
        self._battery_notifying = False
        for attr in ("_keepalive_source", "_flush_source", "_batch_source"):
            src = getattr(self, attr)
            if src is not None:
                GLib.source_remove(src)
                setattr(self, attr, None)

    def _keepalive(self) -> bool:
        if not self._streaming:
            self._keepalive_source = None
            return False
        if self._now_ms() - self._last_sent_ms >= KEEPALIVE_INTERVAL_MS:
            self._send_report(force=True)
        return True

    def _state_changed(self) -> None:
        # Batch: several setters run back-to-back for one controller update
        # (4 axes + 2 triggers); build + send ONCE after they all landed, at
        # high priority so it beats the rest of the main loop's work.
        if self._batch_source is None:
            self._batch_source = GLib.idle_add(self._apply_batch, priority=GLib.PRIORITY_HIGH)

    def _apply_batch(self) -> bool:
        self._batch_source = None
        self._last_report = build_input_report(
            self._buttons, self._axes, self._triggers, self._hat,
        )
        if self._streaming:
            self._send_report()
        return False

    def _send_report(self, force: bool = False) -> None:
        """Rate-limited notify; a deferred flush makes sure the latest state
        always goes out even when changes arrive faster than the floor."""
        wait = NOTIFY_MIN_INTERVAL_MS - (self._now_ms() - self._last_sent_ms)
        if wait > 0 and not force:
            if self._flush_source is None:
                self._flush_source = GLib.timeout_add(max(1, wait), self._flush)
            return
        self._last_sent_ms = self._now_ms()
        self._emit_value(f"{self.APP_PATH}/service0/char3", self._last_report)

    def _flush(self) -> bool:
        self._flush_source = None
        if self._streaming:
            self._send_report(force=True)
        return False

    def _emit_value(self, path: str, value: bytes) -> None:
        try:
            self.bus.emit_signal(
                None, path, DBUS_PROPS_IFACE, "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (
                    GATT_CHAR_IFACE, {"Value": GLib.Variant("ay", value)}, [],
                )),
            )
        except Exception as e:
            logger.debug("notify %s failed: %s", path, e)

    @staticmethod
    def _now_ms() -> int:
        return time.monotonic_ns() // 1_000_000

    # -- status (visualizer) ----------------------------------------------------

    @property
    def notifying(self) -> bool:
        return self._notifying

    @property
    def buttons(self) -> int:
        return self._buttons

    @property
    def axes(self) -> list:
        return list(self._axes)

    @property
    def triggers(self) -> list:
        return list(self._triggers)

    @property
    def hat(self) -> int:
        return self._hat
