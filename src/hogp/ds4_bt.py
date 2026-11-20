#!/usr/bin/env python3
"""
DualShock 4 (v2, CUH-ZCT2) emulation over classic Bluetooth (BR/EDR HID).

macOS's GameController framework and Steam (SDL's PS4 driver) both drive a
DS4 natively over Bluetooth. Like the Pro Controller, a Bluetooth DS4 is a
classic HID device (HIDP over L2CAP PSM 0x11/0x13), so this engine reuses
ProControllerEmulator's L2CAP server and swaps the protocol on top:

  * input: 0x01 "minimal" reports until the host asks for full ones
    (feature report read or an output report), then 0x11 reports with a
    CRC32 — the same switch a real pad makes;
  * feature reports (GET_REPORT on the control channel): calibration
    0x02/0x05, pairing info 0x12, MAC 0x81, firmware 0xA3;
  * output report 0x11 from the host: rumble -> Deck haptics, light bar
    RGB -> shown in the GUI, poll interval -> report rate;
  * touchpad: two contacts driven from the GUI's on-screen touchpad.

Layouts: linux drivers/hid/hid-playstation.c (dualshock4_* structs,
calibration parser, CRC seeds) and SDL's SDL_hidapi_ps4.c.
"""

import logging
import time
from typing import Callable, Optional

from .ds4 import (
    DS4_BT_INPUT_REPORT,
    DS4_FEATURE_CALIBRATION,
    DS4_FEATURE_CALIBRATION_BT,
    DS4_FEATURE_FIRMWARE,
    DS4_FEATURE_MAC_USB,
    DS4_FEATURE_PAIRING_INFO,
    Ds4ReportBuilder,
    build_feature_report,
    parse_output_report,
)
from .pro_controller import (
    HIDP_HSHK_ERR_INVALID_REPORT_ID,
    HIDP_REPORT_FEATURE,
    HIDP_REPORT_INPUT,
    HIDP_TRANS_DATA,
    HIDP_TRANS_HANDSHAKE,
    ProControllerEmulator,
    _parse_mac,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
DS4_BT_NAME = "Wireless Controller"
DS4_BT_VENDOR_ID = 0x054C       # Sony
DS4_BT_PRODUCT_ID = 0x09CC      # DualShock 4 v2 (CUH-ZCT2)
DS4_BT_VERSION = 0x0100
DS4_BT_CLASS_OF_DEVICE = 0x002508  # Peripheral / Gamepad

# Report rate: real pads default to ~1 ms on BT; hosts set it via
# hw_control (low 6 bits, ms). Clamp to what the Python stream sustains.
DS4_DEFAULT_INTERVAL_S = 0.004
DS4_MIN_INTERVAL_S = 0.004
DS4_FULL_MODE_FALLBACK_S = 2.0  # switch to 0x11 even if the host never asks


# ---------------------------------------------------------------------------
# HID report descriptor (built from parts so every size is consistent)
# ---------------------------------------------------------------------------
def _count(n: int) -> bytes:
    return bytes([0x95, n]) if n < 0x100 else bytes([0x96, n & 0xFF, n >> 8])


def _vendor_report(report_id: int, usage: int, size: int, main: int) -> bytes:
    """Vendor-page report: `size` data bytes (report ID excluded)."""
    return bytes([0x85, report_id, 0x09, usage, 0x75, 0x08]) + _count(size) + bytes([main, 0x02])


_INPUT, _OUTPUT, _FEATURE = 0x81, 0x91, 0xB1

DS4_BT_REPORT_DESCRIPTOR = (
    bytes([
        0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,       # Gamepad, Application
        0x85, 0x01,                               # Report 0x01 (minimal)
        0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,  # X Y Z Rz
        0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x04, 0x81, 0x02,
        0x09, 0x39, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46, 0x3B, 0x01,
        0x65, 0x14, 0x75, 0x04, 0x95, 0x01, 0x81, 0x42,  # hat (null state)
        0x65, 0x00,
        0x05, 0x09, 0x19, 0x01, 0x29, 0x0E, 0x15, 0x00, 0x25, 0x01,
        0x75, 0x01, 0x95, 0x0E, 0x81, 0x02,       # 14 buttons
        0x75, 0x06, 0x95, 0x01, 0x81, 0x01,       # 6-bit counter
        0x05, 0x01, 0x09, 0x33, 0x09, 0x34,       # Rx Ry (L2 R2)
        0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x02, 0x81, 0x02,
        0x06, 0x04, 0xFF,                         # vendor page for features
    ])
    + _vendor_report(DS4_FEATURE_CALIBRATION, 0x24, 36, _FEATURE)
    + _vendor_report(DS4_FEATURE_FIRMWARE, 0x25, 48, _FEATURE)
    + _vendor_report(DS4_FEATURE_CALIBRATION_BT, 0x26, 40, _FEATURE)
    + _vendor_report(DS4_FEATURE_PAIRING_INFO, 0x27, 15, _FEATURE)
    + _vendor_report(DS4_FEATURE_MAC_USB, 0x28, 6, _FEATURE)
    + bytes([0xC0])
    + bytes([0x06, 0x00, 0xFF, 0x09, 0x00, 0xA1, 0x01])  # vendor collection
    + _vendor_report(DS4_BT_INPUT_REPORT, 0x30, 77, _INPUT)
    + bytes([0x09, 0x31, 0x91, 0x02])                    # 0x11 output, same size
    + bytes([0xC0])
)

DS4_SDP_RECORD_XML = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
    <attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>
    <attribute id="0x0004">
        <sequence>
            <sequence><uuid value="0x0100"/><uint16 value="0x0011"/></sequence>
            <sequence><uuid value="0x0011"/></sequence>
        </sequence>
    </attribute>
    <attribute id="0x0005"><sequence><uuid value="0x1002"/></sequence></attribute>
    <attribute id="0x0006">
        <sequence><uint16 value="0x656e"/><uint16 value="0x006a"/><uint16 value="0x0100"/></sequence>
    </attribute>
    <attribute id="0x0009">
        <sequence><sequence><uuid value="0x1124"/><uint16 value="0x0101"/></sequence></sequence>
    </attribute>
    <attribute id="0x000d">
        <sequence><sequence>
            <sequence><uuid value="0x0100"/><uint16 value="0x0013"/></sequence>
            <sequence><uuid value="0x0011"/></sequence>
        </sequence></sequence>
    </attribute>
    <attribute id="0x0100"><text value="{DS4_BT_NAME}"/></attribute>
    <attribute id="0x0101"><text value="Game Controller"/></attribute>
    <attribute id="0x0102"><text value="Sony Interactive Entertainment"/></attribute>
    <attribute id="0x0201"><uint16 value="0x0111"/></attribute>
    <attribute id="0x0202"><uint8 value="0x08"/></attribute>
    <attribute id="0x0203"><uint8 value="0x00"/></attribute>
    <attribute id="0x0204"><boolean value="false"/></attribute>
    <attribute id="0x0205"><boolean value="true"/></attribute>
    <attribute id="0x0206">
        <sequence><sequence>
            <uint8 value="0x22"/>
            <text encoding="hex" value="{DS4_BT_REPORT_DESCRIPTOR.hex()}"/>
        </sequence></sequence>
    </attribute>
    <attribute id="0x0207">
        <sequence><sequence><uint16 value="0x0409"/><uint16 value="0x0100"/></sequence></sequence>
    </attribute>
    <attribute id="0x0209"><boolean value="true"/></attribute>
    <attribute id="0x020a"><boolean value="true"/></attribute>
    <attribute id="0x020c"><uint16 value="0x0c80"/></attribute>
    <attribute id="0x020d"><boolean value="false"/></attribute>
    <attribute id="0x020e"><boolean value="false"/></attribute>
    <attribute id="0x020f"><uint16 value="0x0640"/></attribute>
    <attribute id="0x0210"><uint16 value="0x0320"/></attribute>
</record>
"""


class Ds4BtEmulator(ProControllerEmulator):
    """Bluetooth DualShock 4 on top of the Pro Controller L2CAP server."""

    def __init__(self, adapter_address: str = "00:00:00:00:00:00", verbose: bool = False):
        super().__init__(adapter_address=adapter_address, verbose=verbose)
        self._ds4 = Ds4ReportBuilder()
        self._report_interval_s = DS4_DEFAULT_INTERVAL_S
        self._full_mode = False
        self._connected_at = 0.0
        self.lightbar = (0, 0, 64)        # DS4 default: player 1 blue
        # callable(r, g, b) — host changed the light bar
        self.on_lightbar: Optional[Callable[[int, int, int], None]] = None
        # callable(strong 0..1, weak 0..1, duration_ms) — host rumble
        self.on_rumble: Optional[Callable[[float, float, int], None]] = None

    # -- shared state: keep the base fields (visualizer) and the DS4 builder in sync

    def set_button(self, index: int, pressed: bool) -> None:
        self._ds4.set_button(index, pressed)
        super().set_button(index, pressed)

    def set_axis(self, index: int, value: int) -> None:
        self._ds4.set_axis(index, value)
        super().set_axis(index, value)

    def set_trigger(self, index: int, value: int) -> None:
        self._ds4.set_trigger(index, value)
        super().set_trigger(index, value)

    def set_hat(self, value: int) -> None:
        self._ds4.set_hat(value)
        super().set_hat(value)

    def set_touch(self, finger: int, active: bool, x: int, y: int) -> None:
        self._ds4.set_touch(finger, active, x, y)
        self._trigger_send()

    def set_touch_click(self, pressed: bool) -> None:
        self._ds4.set_touch_click(pressed)
        self._trigger_send()

    # -- reports -----------------------------------------------------------

    def _notify_state(self, connected: bool) -> None:
        if connected:
            self._connected_at = time.monotonic()
            self._full_mode = False
            self._report_interval_s = DS4_DEFAULT_INTERVAL_S
        super()._notify_state(connected)

    def _enter_full_mode(self, reason: str) -> None:
        if not self._full_mode:
            self._full_mode = True
            logger.info("DS4: switching to full 0x11 reports (%s)", reason)

    def _build_standard_report(self) -> bytes:
        if (
            not self._full_mode and self._connected_at
            and time.monotonic() - self._connected_at > DS4_FULL_MODE_FALLBACK_S
        ):
            self._enter_full_mode("host never requested it — fallback")
        if self._full_mode:
            return self._ds4.build_bt()
        return self._ds4.build_bt_minimal()

    def _get_report_reply(self, report_type: int, report_id: int) -> Optional[bytes]:
        if report_type == HIDP_REPORT_FEATURE:
            host = self._peer_address()
            feature = build_feature_report(
                report_id, bluetooth=True,
                device_mac=_parse_mac(self.adapter_address),
                host_mac=_parse_mac(host) if host else bytes(6),
            )
            logger.info("DS4: host read feature report 0x%02x%s", report_id,
                        "" if feature else " (unknown — rejected)")
            if feature is None:
                return bytes([HIDP_TRANS_HANDSHAKE | HIDP_HSHK_ERR_INVALID_REPORT_ID])
            if report_id in (DS4_FEATURE_CALIBRATION, DS4_FEATURE_CALIBRATION_BT):
                self._enter_full_mode(f"host read calibration 0x{report_id:02x}")
            return bytes([HIDP_TRANS_DATA | HIDP_REPORT_FEATURE]) + feature
        if report_type == HIDP_REPORT_INPUT:
            return bytes([HIDP_TRANS_DATA | HIDP_REPORT_INPUT]) + self._build_standard_report()
        return None

    def _handle_output_report(self, payload: bytes) -> None:
        if not payload:
            return
        out = parse_output_report(payload, bluetooth=True)
        if out is None:
            logger.debug("DS4: ignored output report %s", bytes(payload[:4]).hex())
            return
        self._enter_full_mode("host sent output report 0x11")
        poll = out.get("poll_ms")
        if poll and poll < 0x3F:
            self._report_interval_s = max(DS4_MIN_INTERVAL_S, poll / 1000.0)
        if "red" in out:
            rgb = (out["red"], out["green"], out["blue"])
            if rgb != self.lightbar:
                self.lightbar = rgb
                logger.info("DS4: light bar -> #%02x%02x%02x", *rgb)
                if self.on_lightbar:
                    try:
                        self.on_lightbar(*rgb)
                    except Exception as e:
                        logger.debug("lightbar callback: %s", e)
        if "motor_left" in out and self.on_rumble:
            # DS4 rumble is level-triggered: it runs until the host sends a
            # new value, so play "long" and let the next report replace it.
            try:
                self.on_rumble(out["motor_left"] / 255.0, out["motor_right"] / 255.0, 10000)
            except Exception as e:
                logger.debug("rumble callback: %s", e)
