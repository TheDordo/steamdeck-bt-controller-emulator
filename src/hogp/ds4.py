#!/usr/bin/env python3
"""
DualShock 4 (CUH-ZCT1) USB HID emulation for the wired gamepad interface.

macOS (10.15+) ships a native DualShock 4 driver in the GameController
framework. Games on the Mac only see a gamepad if the USB device presents
the VID/PID and HID report descriptor of a controller Apple recognises —
a generic HID gamepad is ignored by GCExtendedGamepad-based games.

This module reproduces the DS4 v1 USB identity:

    VID 0x054C / PID 0x05C4
    Single HID interface, 64-byte input report (Report ID 0x01)
    Output report 0x05 (rumble + lightbar) accepted and discarded

Descriptor + report layout verified against:
    * ds4drv (chrippa/ds4drv) report parsing offsets
    * orcadom/DS4 (Arduino DS4 emulator, public domain descriptor)
    * xpadneo docs / kernel hid-sony (hid-playstation) behaviour
"""

import logging
import struct
import time
import zlib
from typing import Dict, Optional

from .battery import read_battery_percent

logger = logging.getLogger(__name__)

# USB device identity for a DualShock 4 (v1).
DS4_VID = 0x054C
DS4_PID = 0x05C4
DS4_BCD_DEVICE = 0x0100
DS4_MANUFACTURER = "Sony Interactive Entertainment"
DS4_PRODUCT = "Wireless Controller"
DS4_SERIAL = "a01e0d000000"  # DS4 serials are 12 hex chars; stable value

# HID report descriptor (byte-for-byte from orcadom/DS4, public domain).
# Report ID 0x01: 63 data bytes -> 64 bytes total with the ID byte.
DS4_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x05, 0xA1, 0x01, 0x85, 0x01, 0x09, 0x30, 0x09, 0x31,
    0x09, 0x32, 0x09, 0x35, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95,
    0x04, 0x81, 0x02, 0x09, 0x39, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46,
    0x3B, 0x01, 0x65, 0x14, 0x75, 0x04, 0x95, 0x01, 0x81, 0x42, 0x65, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x0E, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01,
    0x95, 0x0E, 0x81, 0x02, 0x06, 0x00, 0xFF, 0x09, 0x20, 0x75, 0x06, 0x95,
    0x01, 0x15, 0x00, 0x25, 0x7F, 0x81, 0x02, 0x05, 0x01, 0x09, 0x33, 0x09,
    0x34, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x02, 0x81, 0x02,
    0x06, 0x00, 0xFF, 0x09, 0x21, 0x95, 0x36, 0x81, 0x02, 0x85, 0x05, 0x09,
    0x22, 0x95, 0x1F, 0x91, 0x02, 0x85, 0x04, 0x09, 0x23, 0x95, 0x24, 0xB1,
    0x02, 0x85, 0x02, 0x09, 0x24, 0x95, 0x24, 0xB1, 0x02, 0x85, 0x08, 0x09,
    0x25, 0x95, 0x03, 0xB1, 0x02, 0x85, 0x10, 0x09, 0x26, 0x95, 0x04, 0xB1,
    0x02, 0x85, 0x11, 0x09, 0x27, 0x95, 0x02, 0xB1, 0x02, 0x85, 0x12, 0x06,
    0x02, 0xFF, 0x09, 0x21, 0x95, 0x0F, 0xB1, 0x02, 0x85, 0x13, 0x09, 0x22,
    0x95, 0x16, 0xB1, 0x02, 0x85, 0x14, 0x06, 0x05, 0xFF, 0x09, 0x20, 0x95,
    0x10, 0xB1, 0x02, 0x85, 0x15, 0x09, 0x21, 0x95, 0x2C, 0xB1, 0x02, 0x06,
    0x80, 0xFF, 0x85, 0x80, 0x09, 0x20, 0x95, 0x06, 0xB1, 0x02, 0x85, 0x81,
    0x09, 0x21, 0x95, 0x06, 0xB1, 0x02, 0x85, 0x82, 0x09, 0x22, 0x95, 0x05,
    0xB1, 0x02, 0x85, 0x83, 0x09, 0x23, 0x95, 0x01, 0xB1, 0x02, 0x85, 0x84,
    0x09, 0x24, 0x95, 0x04, 0xB1, 0x02, 0x85, 0x85, 0x09, 0x25, 0x95, 0x06,
    0xB1, 0x02, 0x85, 0x86, 0x09, 0x26, 0x95, 0x06, 0xB1, 0x02, 0x85, 0x87,
    0x09, 0x27, 0x95, 0x23, 0xB1, 0x02, 0x85, 0x88, 0x09, 0x28, 0x95, 0x22,
    0xB1, 0x02, 0x85, 0x89, 0x09, 0x29, 0x95, 0x02, 0xB1, 0x02, 0x85, 0x90,
    0x09, 0x30, 0x95, 0x05, 0xB1, 0x02, 0x85, 0x91, 0x09, 0x31, 0x95, 0x03,
    0xB1, 0x02, 0x85, 0x92, 0x09, 0x32, 0x95, 0x03, 0xB1, 0x02, 0x85, 0x93,
    0x09, 0x33, 0x95, 0x0C, 0xB1, 0x02, 0x85, 0xA0, 0x09, 0x40, 0x95, 0x06,
    0xB1, 0x02, 0x85, 0xA1, 0x09, 0x41, 0x95, 0x01, 0xB1, 0x02, 0x85, 0xA2,
    0x09, 0x42, 0x95, 0x01, 0xB1, 0x02, 0x85, 0xA3, 0x09, 0x43, 0x95, 0x30,
    0xB1, 0x02, 0x85, 0xA4, 0x09, 0x44, 0x95, 0x0D, 0xB1, 0x02, 0x85, 0xA5,
    0x09, 0x45, 0x95, 0x15, 0xB1, 0x02, 0x85, 0xA6, 0x09, 0x46, 0x95, 0x15,
    0xB1, 0x02, 0x85, 0xF0, 0x09, 0x47, 0x95, 0x3F, 0xB1, 0x02, 0x85, 0xF1,
    0x09, 0x48, 0x95, 0x3F, 0xB1, 0x02, 0x85, 0xF2, 0x09, 0x49, 0x95, 0x0F,
    0xB1, 0x02, 0x85, 0xA7, 0x09, 0x4A, 0x95, 0x01, 0xB1, 0x02, 0x85, 0xA8,
    0x09, 0x4B, 0x95, 0x01, 0xB1, 0x02, 0x85, 0xA9, 0x09, 0x4C, 0x95, 0x08,
    0xB1, 0x02, 0x85, 0xAA, 0x09, 0x4E, 0x95, 0x01, 0xB1, 0x02, 0x85, 0xAB,
    0x09, 0x4F, 0x95, 0x39, 0xB1, 0x02, 0x85, 0xAC, 0x09, 0x50, 0x95, 0x39,
    0xB1, 0x02, 0x85, 0xAD, 0x09, 0x51, 0x95, 0x0B, 0xB1, 0x02, 0x85, 0xAE,
    0x09, 0x52, 0x95, 0x01, 0xB1, 0x02, 0x85, 0xAF, 0x09, 0x53, 0x95, 0x02,
    0xB1, 0x02, 0x85, 0xB0, 0x09, 0x54, 0x95, 0x3F, 0xB1, 0x02, 0xC0,
])

DS4_INPUT_REPORT_SIZE = 64  # includes Report ID byte

# Button bits of the 64-byte input report (see ds4drv parse_report).
# Byte 5: low nibble = dpad, then square/cross/circle/triangle.
# Byte 6: L1/R1/L2/R2/Share/Options/L3/R3. Byte 7: PS + touchpad + counter.
DS4_BTN_SQUARE = 0x10
DS4_BTN_CROSS = 0x20
DS4_BTN_CIRCLE = 0x40
DS4_BTN_TRIANGLE = 0x80

DS4_BTN_L1 = 0x01
DS4_BTN_R1 = 0x02
DS4_BTN_L2 = 0x04
DS4_BTN_R2 = 0x08
DS4_BTN_SHARE = 0x10
DS4_BTN_OPTIONS = 0x20
DS4_BTN_L3 = 0x40
DS4_BTN_R3 = 0x80

DS4_BTN_PS = 0x01
DS4_BTN_TOUCHPAD = 0x02

# Gamepad index -> DS4 bit (matches InputHandler BUTTON_MAP order:
# A, B, X, Y, LB, RB, Back, Start, Guide, L3, R3)
DS4_BUTTON_MAP = {
    0: DS4_BTN_CROSS,      # A
    1: DS4_BTN_CIRCLE,     # B
    2: DS4_BTN_SQUARE,     # X
    3: DS4_BTN_TRIANGLE,   # Y
    4: DS4_BTN_L1,         # LB
    5: DS4_BTN_R1,         # RB
    6: DS4_BTN_SHARE,      # Back
    7: DS4_BTN_OPTIONS,     # Start
    8: DS4_BTN_PS,         # Guide
    9: DS4_BTN_L3,         # L3
    10: DS4_BTN_R3,        # R3
}


# ---------------------------------------------------------------------------
# Constants shared by the USB and Bluetooth DualShock 4 identities
# (layouts: linux drivers/hid/hid-playstation.c dualshock4_* structs)
# ---------------------------------------------------------------------------
DS4_TOUCHPAD_WIDTH = 1920
DS4_TOUCHPAD_HEIGHT = 942
DS4_TOUCH_INACTIVE = 0x80      # contact byte bit 7: no finger
DS4_TRIGGER_DIGITAL_THRESHOLD = 32

# CRC32 seeds prepended before the report (HIDP transaction header byte)
DS4_CRC_SEED_INPUT = 0xA1
DS4_CRC_SEED_OUTPUT = 0xA2
DS4_CRC_SEED_FEATURE = 0xA3

DS4_BT_INPUT_REPORT = 0x11
DS4_BT_INPUT_SIZE = 78         # incl. report ID and CRC
DS4_BT_MINIMAL_REPORT = 0x01
DS4_BT_MINIMAL_SIZE = 10
DS4_FEATURE_CALIBRATION = 0x02         # USB (and some BT hosts)
DS4_FEATURE_CALIBRATION_SIZE = 37
DS4_FEATURE_CALIBRATION_BT = 0x05      # BT, with CRC
DS4_FEATURE_CALIBRATION_BT_SIZE = 41
DS4_FEATURE_PAIRING_INFO = 0x12
DS4_FEATURE_PAIRING_INFO_SIZE = 16
DS4_FEATURE_MAC_USB = 0x81
DS4_FEATURE_MAC_USB_SIZE = 7
DS4_FEATURE_FIRMWARE = 0xA3
DS4_FEATURE_FIRMWARE_SIZE = 49

# Plausible factory calibration (kernel divides by |plus-bias|+|minus-bias|
# and plus-minus: all-zero calibration would divide by zero on the host).
_GYRO_BIAS = (0, 0, 0)
_GYRO_PLUS = (8800, 8800, 8800)     # pitch, yaw, roll
_GYRO_MINUS = (-8800, -8800, -8800)
_GYRO_SPEED = (540, 540)            # plus, minus
_ACCEL = (8192, -8192, 8192, -8192, 8192, -8192)  # x+ x- y+ y- z+ z-


def ds4_crc32(seed: int, data: bytes) -> int:
    """CRC32 the way the DS4 computes it: over the HIDP header byte + data."""
    return zlib.crc32(bytes([seed]) + bytes(data)) & 0xFFFFFFFF


def _calibration_payload(bluetooth: bool) -> bytes:
    """34 calibration bytes after the report ID. Gyro plus/minus order
    differs between USB (pitch+ pitch- yaw+ yaw- roll+ roll-) and BT
    (pitch+ yaw+ roll+ pitch- yaw- roll-) — see dualshock4_get_calibration_data."""
    if bluetooth:
        gyro = (*_GYRO_PLUS, *_GYRO_MINUS)
    else:
        gyro = (_GYRO_PLUS[0], _GYRO_MINUS[0], _GYRO_PLUS[1], _GYRO_MINUS[1],
                _GYRO_PLUS[2], _GYRO_MINUS[2])
    return struct.pack("<3h6h2h6h", *_GYRO_BIAS, *gyro, *_GYRO_SPEED, *_ACCEL)


def build_feature_report(report_id: int, bluetooth: bool,
                         device_mac: bytes = bytes(6), host_mac: bytes = bytes(6)) -> Optional[bytes]:
    """Feature report contents INCLUDING the report ID, or None if unknown.
    MACs are big-endian (display order); the DS4 stores them reversed."""
    if report_id in (DS4_FEATURE_CALIBRATION, DS4_FEATURE_CALIBRATION_BT):
        size = DS4_FEATURE_CALIBRATION_BT_SIZE if report_id == DS4_FEATURE_CALIBRATION_BT else DS4_FEATURE_CALIBRATION_SIZE
        rep = bytearray(size)
        rep[0] = report_id
        rep[1:35] = _calibration_payload(bluetooth)
        if report_id == DS4_FEATURE_CALIBRATION_BT:
            struct.pack_into("<I", rep, size - 4, ds4_crc32(DS4_CRC_SEED_FEATURE, rep[:size - 4]))
        return bytes(rep)
    if report_id == DS4_FEATURE_PAIRING_INFO:
        rep = bytearray(DS4_FEATURE_PAIRING_INFO_SIZE)
        rep[0] = report_id
        rep[1:7] = bytes(reversed(device_mac))
        rep[7:10] = bytes([0x08, 0x25, 0x00])
        rep[10:16] = bytes(reversed(host_mac))
        return bytes(rep)
    if report_id == DS4_FEATURE_MAC_USB:
        return bytes([report_id]) + bytes(reversed(device_mac))
    if report_id == DS4_FEATURE_FIRMWARE:
        rep = bytearray(DS4_FEATURE_FIRMWARE_SIZE)
        rep[0] = report_id
        rep[1:12] = b"Sep 21 2018"
        rep[17:25] = b"04:50:51"
        struct.pack_into("<H", rep, 35, 0x0100)   # hardware version
        struct.pack_into("<H", rep, 41, 0x8001)   # firmware version
        return bytes(rep)
    return None


def parse_output_report(data: bytes, bluetooth: bool) -> Optional[Dict[str, int]]:
    """Rumble + light bar from a host output report (incl. report ID):
    USB 0x05 (common block at 1) or BT 0x11 (hw_control, audio_control,
    then common block at 3). Returns only the fields the host marked valid."""
    data = bytes(data)
    if bluetooth:
        if len(data) < 13 or data[0] != 0x11:
            return None
        off = 3
    else:
        if len(data) < 11 or data[0] != 0x05:
            return None
        off = 1
    flags = data[off]
    out: Dict[str, int] = {}
    if flags & 0x01:
        out["motor_right"] = data[off + 3]   # weak / high frequency
        out["motor_left"] = data[off + 4]    # strong / low frequency
    if flags & 0x02:
        out["red"], out["green"], out["blue"] = data[off + 5], data[off + 6], data[off + 7]
    if flags & 0x04:
        out["blink_on"], out["blink_off"] = data[off + 8], data[off + 9]
    if bluetooth:
        out["poll_ms"] = data[1] & 0x3F
    return out


class Ds4ReportBuilder:
    """Builds DualShock 4 input reports (USB 0x01, BT 0x11 / minimal 0x01)
    from the shared gamepad state model used across the app (11 buttons,
    4 axes, 2 triggers, hat) plus the DS4 touchpad."""

    DPAD_NEUTRAL = 8
    BATTERY_REFRESH_S = 15.0

    def __init__(self):
        self._buttons = 0              # 11-bit mask, same semantics as usb_gadget
        self._axes = [0, 0, 0, 0]      # -32768..32767
        self._triggers = [0, 0]        # 0..255
        self._hat = self.DPAD_NEUTRAL  # 0..7 directions, 8 = center
        self._counter = 0
        self._touch = [[False, 0, 0], [False, 0, 0]]  # active, x, y per finger
        self._touch_ids = [0, 1]
        self._next_touch_id = 2
        self._touch_timestamp = 0
        self._touch_click = False
        self._battery_byte = 0x1B      # cable + full until first read
        self._battery_read_at = 0.0
        self._t0 = time.monotonic()

    def set_button(self, index: int, pressed: bool) -> None:
        if 0 <= index < 11:
            if pressed:
                self._buttons |= (1 << index)
            else:
                self._buttons &= ~(1 << index)

    def set_axis(self, index: int, value: int) -> None:
        if 0 <= index < 4:
            self._axes[index] = max(-32768, min(32767, value))

    def set_trigger(self, index: int, value: int) -> None:
        if 0 <= index < 2:
            self._triggers[index] = max(0, min(255, value))

    def set_hat(self, value: int) -> None:
        if value == 0x0F:
            value = self.DPAD_NEUTRAL
        self._hat = value if 0 <= value <= 8 else self.DPAD_NEUTRAL

    def set_touch(self, finger: int, active: bool, x: int, y: int) -> None:
        """finger 0/1; x 0..1919, y 0..941. A new contact gets a new
        tracking ID (7-bit, wraps) like the real touchpad."""
        if finger not in (0, 1):
            return
        was_active = self._touch[finger][0]
        if active and not was_active:
            self._touch_ids[finger] = self._next_touch_id & 0x7F
            self._next_touch_id += 1
        self._touch[finger] = [
            bool(active),
            max(0, min(DS4_TOUCHPAD_WIDTH - 1, int(x))),
            max(0, min(DS4_TOUCHPAD_HEIGHT - 1, int(y))),
        ]
        self._touch_timestamp = (self._touch_timestamp + 1) & 0xFF

    def set_touch_click(self, pressed: bool) -> None:
        self._touch_click = bool(pressed)

    def _pack_axis(self, value: int) -> int:
        """-32768..32767 -> 0..255 (127/128 center)."""
        return int(((value + 32768) / 65535.0) * 255.0)

    def _battery_status(self) -> int:
        """status[0]: battery capacity (0..10, 11 = full when charging) +
        cable bit 4. Re-read at most every 15 s (sysfs, not per report)."""
        now = time.monotonic()
        if now - self._battery_read_at >= self.BATTERY_REFRESH_S:
            self._battery_read_at = now
            try:
                level = max(0, min(10, round(read_battery_percent() / 10.0)))
            except Exception:
                level = 10
            self._battery_byte = 0x10 | level
        return self._battery_byte

    def common_block(self) -> bytes:
        """The 32-byte dualshock4_input_report_common block (USB and BT)."""
        c = bytearray(32)
        c[0] = self._pack_axis(self._axes[0])   # left stick X
        c[1] = self._pack_axis(self._axes[1])   # left stick Y (down = 255)
        c[2] = self._pack_axis(self._axes[2])   # right stick X
        c[3] = self._pack_axis(self._axes[3])   # right stick Y

        b0 = self._hat & 0x0F
        if self._buttons & (1 << 2):
            b0 |= DS4_BTN_SQUARE
        if self._buttons & (1 << 0):
            b0 |= DS4_BTN_CROSS
        if self._buttons & (1 << 1):
            b0 |= DS4_BTN_CIRCLE
        if self._buttons & (1 << 3):
            b0 |= DS4_BTN_TRIANGLE
        c[4] = b0

        b1 = 0
        if self._buttons & (1 << 4):
            b1 |= DS4_BTN_L1
        if self._buttons & (1 << 5):
            b1 |= DS4_BTN_R1
        if self._triggers[0] >= DS4_TRIGGER_DIGITAL_THRESHOLD:
            b1 |= DS4_BTN_L2
        if self._triggers[1] >= DS4_TRIGGER_DIGITAL_THRESHOLD:
            b1 |= DS4_BTN_R2
        if self._buttons & (1 << 6):
            b1 |= DS4_BTN_SHARE
        if self._buttons & (1 << 7):
            b1 |= DS4_BTN_OPTIONS
        if self._buttons & (1 << 9):
            b1 |= DS4_BTN_L3
        if self._buttons & (1 << 10):
            b1 |= DS4_BTN_R3
        c[5] = b1

        b2 = 0
        if self._buttons & (1 << 8):
            b2 |= DS4_BTN_PS
        if self._touch_click:
            b2 |= DS4_BTN_TOUCHPAD
        c[6] = b2 | ((self._counter << 2) & 0xFC)

        c[7] = self._triggers[0]  # L2 analog
        c[8] = self._triggers[1]  # R2 analog

        # Sensor timestamp (5.33 us units) — hosts integrate motion with it.
        ts = int((time.monotonic() - self._t0) * 1_000_000 / 5.33) & 0xFFFF
        struct.pack_into("<H", c, 9, ts)
        # [11] temperature, [12..23] gyro/accel: at rest. Accelerometer
        # reports gravity on Y (controller lying flat, face up).
        struct.pack_into("<3h3h", c, 12, 0, 0, 0, 0, 8192, 0)
        c[29] = self._battery_status()
        self._counter = (self._counter + 1) & 0x3F
        return bytes(c)

    def touch_report(self) -> bytes:
        """One 9-byte dualshock4_touch_report (timestamp + 2 points)."""
        t = bytearray(9)
        t[0] = self._touch_timestamp
        for i, (active, x, y) in enumerate(self._touch):
            o = 1 + 4 * i
            t[o] = (self._touch_ids[i] & 0x7F) | (0 if active else DS4_TOUCH_INACTIVE)
            t[o + 1] = x & 0xFF
            t[o + 2] = ((x >> 8) & 0x0F) | ((y & 0x0F) << 4)
            t[o + 3] = (y >> 4) & 0xFF
        return bytes(t)

    def build(self) -> bytes:
        """64-byte USB input report 0x01."""
        report = bytearray(DS4_INPUT_REPORT_SIZE)
        report[0] = 0x01
        report[1:33] = self.common_block()
        report[33] = 1                       # one touch report follows
        report[34:43] = self.touch_report()
        # Remaining touch report slots: no contacts.
        for slot in (43, 52):
            report[slot + 1] = DS4_TOUCH_INACTIVE
            report[slot + 5] = DS4_TOUCH_INACTIVE
        return bytes(report)

    def build_bt(self) -> bytes:
        """78-byte Bluetooth input report 0x11 (incl. report ID and CRC32)."""
        report = bytearray(DS4_BT_INPUT_SIZE)
        report[0] = DS4_BT_INPUT_REPORT
        report[1] = 0xC0                     # HID + CRC present (as sent by real pads)
        report[3:35] = self.common_block()
        report[35] = 1
        report[36:45] = self.touch_report()
        for slot in (45, 54, 63):
            report[slot + 1] = DS4_TOUCH_INACTIVE
            report[slot + 5] = DS4_TOUCH_INACTIVE
        struct.pack_into("<I", report, DS4_BT_INPUT_SIZE - 4,
                         ds4_crc32(DS4_CRC_SEED_INPUT, report[:DS4_BT_INPUT_SIZE - 4]))
        return bytes(report)

    def build_bt_minimal(self) -> bytes:
        """10-byte Bluetooth report 0x01 a DS4 sends until the host asks
        for full reports (sticks, buttons, triggers only)."""
        return bytes([DS4_BT_MINIMAL_REPORT]) + self.common_block()[:9]
