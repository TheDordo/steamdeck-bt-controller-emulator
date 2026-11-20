#!/usr/bin/env python3
"""
Nintendo Switch Pro Controller over USB (HID gadget on /dev/hidg0).

Same protocol core as the Bluetooth engine (subcommands, SPI flash,
0x30 reports — inherited from ProControllerEmulator), different transport:

  * reports go to /dev/hidg0 WITHOUT the HIDP 0xA1 header, padded to the
    64-byte USB report length;
  * before talking subcommands the host runs the USB-only "proprietary"
    handshake on output report 0x80 (SDL SDL_hidapi_switch.c, linux
    hid-nintendo.c joycon_send_usb):

        host 80 01 (status)     -> 81 01 00 03 <MAC little-endian>
        host 80 02 (handshake)  -> 81 02
        host 80 03 (3 Mbit)     -> 81 03
        host 80 04 (force USB)  -> no reply; controller starts streaming
        host 80 05 (allow timeout) -> no reply

If the host never runs the handshake (some drivers just read), streaming
starts anyway after USB_HANDSHAKE_FALLBACK_S.
"""

import logging
import os
import select
import threading
import time
from typing import Optional

from .pro_controller import ProControllerEmulator, _parse_mac

logger = logging.getLogger(__name__)

USB_REPORT_LEN = 64
OUTPUT_PROPRIETARY = 0x80
INPUT_PROPRIETARY_ACK = 0x81
USB_CMD_STATUS = 0x01
USB_CMD_HANDSHAKE = 0x02
USB_CMD_HIGH_SPEED = 0x03
USB_CMD_FORCE_USB = 0x04
USB_CMD_ALLOW_TIMEOUT = 0x05
USB_HANDSHAKE_FALLBACK_S = 2.0
USB_PRO_MAC = "98:B6:E9:00:00:01"  # Nintendo OUI; shown as the serial


class _UsbLink:
    """Truthy stand-in for the L2CAP interrupt connection the base class
    checks (its stream loop stops when the 'connection' is None)."""


class UsbProController(ProControllerEmulator):
    """Pro Controller protocol on a USB HID gadget file descriptor."""

    def __init__(self, fd: int, verbose: bool = False):
        super().__init__(adapter_address=USB_PRO_MAC, verbose=verbose)
        self._fd = fd
        self._write_lock = threading.Lock()
        self._opened_at = 0.0
        self._handshake_seen = False

    # -- lifecycle (no L2CAP: the gadget wrapper owns the fd) -----------------

    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._opened_at = time.monotonic()
        self._itr_conn = _UsbLink()
        self._spawn(self._usb_read_loop)
        self._spawn(self._fallback_start)
        logger.info("USB Pro Controller ready on the HID gadget (waiting for host handshake)")
        return True

    def stop(self) -> None:
        self._running = False
        self._streaming = False
        self._connected = False
        self._itr_conn = None

    def _begin_streaming(self, reason: str) -> None:
        if self._streaming or not self._running:
            return
        logger.info("USB Pro Controller: streaming input (%s)", reason)
        self._connected = True
        self._streaming = True
        self._spawn(self._stream_loop)
        self._notify_state(True)

    def _fallback_start(self) -> None:
        while self._running and not self._streaming:
            if time.monotonic() - self._opened_at >= USB_HANDSHAKE_FALLBACK_S:
                self._begin_streaming(
                    "host never sent 80 04 — fallback"
                    if not self._handshake_seen else "handshake without force-USB"
                )
                return
            time.sleep(0.1)

    # -- transport ------------------------------------------------------------

    def _write_interrupt(self, data: bytes) -> bool:
        """Base class hands us HIDP-framed reports (0xA1 + report)."""
        report = bytes(data[1:]) if data and data[0] == 0xA1 else bytes(data)
        report = report[:USB_REPORT_LEN].ljust(USB_REPORT_LEN, b"\x00")
        with self._write_lock:
            try:
                return os.write(self._fd, report) == USB_REPORT_LEN
            except BlockingIOError:
                return False   # host not polling (cable out / suspended)
            except OSError as e:
                logger.debug("hidg write failed: %s", e)
                return False

    def _write_control(self, data: bytes) -> bool:
        return False  # no HIDP control channel on USB

    def _usb_read_loop(self) -> None:
        while self._running:
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.2)
                if not ready:
                    continue
                data = os.read(self._fd, USB_REPORT_LEN)
            except BlockingIOError:
                continue
            except OSError as e:
                if self._running:
                    logger.debug("hidg read failed: %s", e)
                break
            if data:
                try:
                    self._handle_usb_output(data)
                except Exception as e:
                    logger.warning("USB Pro: bad host report %s: %s", data[:4].hex(), e)

    def _handle_usb_output(self, data: bytes) -> None:
        if data[0] != OUTPUT_PROPRIETARY:
            # 0x01 rumble+subcommand / 0x10 rumble-only: same as Bluetooth
            self._handle_output_report(data)
            return
        cmd = data[1] if len(data) > 1 else 0
        self._handshake_seen = True
        logger.info("USB Pro: host proprietary command 0x80 0x%02x", cmd)
        if cmd == USB_CMD_STATUS:
            mac = bytes(reversed(_parse_mac(self.adapter_address)))
            self._write_interrupt(bytes([INPUT_PROPRIETARY_ACK, USB_CMD_STATUS, 0x00, 0x03]) + mac)
        elif cmd in (USB_CMD_HANDSHAKE, USB_CMD_HIGH_SPEED):
            self._write_interrupt(bytes([INPUT_PROPRIETARY_ACK, cmd]))
        elif cmd == USB_CMD_FORCE_USB:
            self._begin_streaming("host sent force-USB")
        # 0x05 (allow timeout) / 0x06 (reset MCU): nothing to answer
