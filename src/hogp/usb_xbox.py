#!/usr/bin/env python3
"""
Wired Xbox Series X|S controller (045E:0B12) over USB — GIP on FunctionFS.

Wired Xbox One/Series controllers are NOT HID: they expose a vendor
interface (class 0xFF / subclass 0x47 / protocol 0xD0) with one interrupt
IN and one interrupt OUT endpoint and speak Microsoft's Game Input Protocol
(GIP). macOS drives exactly this with its built-in XboxGamepad.dext
(personality XboxSeriesXGamepad: 045E:0B12, class FF/47); Linux uses xpad,
Steam uses SDL's HIDAPI Xbox One driver.

The Linux USB gadget exposes a non-HID function through FunctionFS: we write
USB descriptors to ep0, then ep1 (IN) / ep2 (OUT) become plain files.

GIP (header: command, options, sequence, payload length):
  device -> host  0x02 announce   (MAC, VID/PID, firmware) until the host talks
                  0x01 ack        for host packets flagged GIP_OPT_ACK (0x10)
                  0x20 input      44-byte payload (firmware 5.5 layout)
                  0x07 virtual key  Xbox button
  host -> device  0x05 power on, 0x0A LED, 0x06 auth done, 0x04 identify,
                  0x09 rumble (-> Deck haptics)

Layouts: linux drivers/input/joystick/xpad.c (xpadone_process_packet, init
packets), SDL_hidapi_xboxone.c (announce format, init state machine,
Series X share-button offsets), uapi linux/usb/functionfs.h.
"""

import logging
import os
import queue
import struct
import threading
import time
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

FFS_MOUNT = "/dev/ffs-xbox"
GADGET_DIR = "/sys/kernel/config/usb_gadget/steamdeck_hid"

XBOX_USB_VID = 0x045E
XBOX_USB_PID = 0x0B12
XBOX_USB_MAC = bytes.fromhex("7ced8d000001")   # Microsoft OUI
# Firmware 5.5.2641.0 — the 44-byte input report layout below
XBOX_FW = bytes([0x05, 0x00, 0x05, 0x00, 0x51, 0x0A, 0x00, 0x00])

# GIP commands / options (xpad.c)
GIP_CMD_ACK = 0x01
GIP_CMD_ANNOUNCE = 0x02
GIP_CMD_IDENTIFY = 0x04
GIP_CMD_POWER = 0x05
GIP_CMD_AUTHENTICATE = 0x06
GIP_CMD_VIRTUAL_KEY = 0x07
GIP_CMD_RUMBLE = 0x09
GIP_CMD_LED = 0x0A
GIP_CMD_INPUT = 0x20
GIP_OPT_ACK = 0x10
GIP_OPT_INTERNAL = 0x20
GIP_INPUT_PAYLOAD = 44
GIP_PWR_ON = 0x00

ANNOUNCE_INTERVAL_S = 0.5
STREAM_FALLBACK_S = 1.5

# FunctionFS (uapi linux/usb/functionfs.h)
FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_STRINGS_MAGIC = 2
FUNCTIONFS_HAS_FS_DESC = 1
FUNCTIONFS_HAS_HS_DESC = 2
FFS_EVENT_BIND, FFS_EVENT_UNBIND, FFS_EVENT_ENABLE, FFS_EVENT_DISABLE, \
    FFS_EVENT_SETUP, FFS_EVENT_SUSPEND, FFS_EVENT_RESUME = range(7)
FFS_EVENT_SIZE = 12
USB_DIR_IN = 0x80


# ---------------------------------------------------------------------------
# Pure builders (unit-testable)
# ---------------------------------------------------------------------------
def _interface_desc() -> bytes:
    # bLength, INTERFACE, number, alt, numEP, class FF, subclass 47, proto D0, iInterface
    return bytes([9, 4, 0, 0, 2, 0xFF, 0x47, 0xD0, 0])


def _endpoint_desc(address: int, interval: int) -> bytes:
    # bLength, ENDPOINT, address, interrupt, wMaxPacketSize 64, bInterval
    return bytes([7, 5, address, 0x03]) + struct.pack("<H", 64) + bytes([interval])


def build_ffs_descriptors() -> bytes:
    """FunctionFS v2 descriptor blob: full speed (1 ms IN) + high speed
    (bInterval 4 = 2^(4-1) * 125 us = 1 ms IN). ep1 = IN, ep2 = OUT."""
    fs = _interface_desc() + _endpoint_desc(0x81, 1) + _endpoint_desc(0x02, 8)
    hs = _interface_desc() + _endpoint_desc(0x81, 4) + _endpoint_desc(0x02, 4)
    body = struct.pack("<II", 3, 3) + fs + hs        # fs_count, hs_count
    flags = FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
    return struct.pack("<III", FUNCTIONFS_DESCRIPTORS_MAGIC_V2, 12 + len(body), flags) + body


def build_ffs_strings() -> bytes:
    return struct.pack("<IIII", FUNCTIONFS_STRINGS_MAGIC, 16, 0, 0)


def _stick(value: int) -> int:
    return max(-32768, min(32767, int(value)))


def _trigger(value: int) -> int:
    return max(0, min(1023, (int(value) * 1023 + 127) // 255))


# Shared button index -> (payload byte, bit)
_BUTTONS = {
    0: (0, 0x10),  # A
    1: (0, 0x20),  # B
    2: (0, 0x40),  # X
    3: (0, 0x80),  # Y
    4: (1, 0x10),  # LB
    5: (1, 0x20),  # RB
    6: (0, 0x08),  # View
    7: (0, 0x04),  # Menu
    9: (1, 0x40),  # LS
    10: (1, 0x80),  # RS
}
# Shared hat (0=N..7=NW, 8=center) -> GIP d-pad bits (up 1, down 2, left 4, right 8)
_HAT_BITS = {0: 0x01, 1: 0x09, 2: 0x08, 3: 0x0A, 4: 0x02, 5: 0x06, 6: 0x04, 7: 0x05}


def build_input_payload(buttons: int, axes: List[int], triggers: List[int], hat: int,
                        share: bool = False) -> bytes:
    """44-byte GIP input payload (Series X|S firmware 5.5)."""
    p = bytearray(GIP_INPUT_PAYLOAD)
    for index, (byte, bit) in _BUTTONS.items():
        if buttons & (1 << index):
            p[byte] |= bit
    p[1] |= _HAT_BITS.get(hat, 0)
    # Sticks are signed, Y grows UPWARD (xpad inverts it for evdev): the
    # shared state uses evdev's downward Y, so invert here.
    struct.pack_into(
        "<HHhhhh", p, 2,
        _trigger(triggers[0]), _trigger(triggers[1]),
        _stick(axes[0]), _stick(-axes[1] - 1 if axes[1] < 0 else -axes[1]),
        _stick(axes[2]), _stick(-axes[3] - 1 if axes[3] < 0 else -axes[3]),
    )
    if share:
        p[18] |= 0x01
    return bytes(p)


def build_announce() -> bytes:
    payload = (XBOX_USB_MAC + bytes(2) + struct.pack("<HH", XBOX_USB_VID, XBOX_USB_PID)
               + XBOX_FW + bytes(8))
    return bytes([GIP_CMD_ANNOUNCE, GIP_OPT_INTERNAL, 0, len(payload)]) + payload


def build_ack(cmd: int, options: int, seq: int, length: int) -> bytes:
    """xone gip_pkt_acknowledge: unknown, command, options, length(le16),
    padding(2), remaining(le16)."""
    payload = bytes([0x00, cmd, options & GIP_OPT_INTERNAL]) + struct.pack("<H", length) + bytes(4)
    return bytes([GIP_CMD_ACK, GIP_OPT_INTERNAL, seq, len(payload)]) + payload


def parse_rumble(packet: bytes):
    """0x09 rumble: header + [0x00, motor mask, LT, RT, left(strong),
    right(weak), duration, delay, repeat] — magnitudes 0..100."""
    if len(packet) < 4 + 6 or packet[0] != GIP_CMD_RUMBLE:
        return None
    p = packet[4:]
    mask = p[1]
    lt = p[2] if mask & 0x08 else 0
    rt = p[3] if mask & 0x04 else 0
    left = p[4] if mask & 0x02 else 0
    right = p[5] if mask & 0x01 else 0
    return {"strong": max(left, lt) / 100.0, "weak": max(right, rt) / 100.0}


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------
class UsbXboxGip:
    """Wired Xbox Series controller on a FunctionFS gadget function."""

    def __init__(self, mount: str = FFS_MOUNT, gadget_dir: str = GADGET_DIR, verbose: bool = False):
        self.mount = mount
        self.gadget_dir = gadget_dir
        self.verbose = verbose
        self._buttons = 0
        self._axes = [0, 0, 0, 0]
        self._triggers = [0, 0]
        self._hat = 8
        self._guide = False

        self._ep0: Optional[int] = None
        self._ep_in: Optional[int] = None
        self._ep_out: Optional[int] = None
        self._running = False
        self._enabled = False       # host configured the interface
        self._host_spoke = False    # any packet from the host seen
        self._streaming = False
        self._enabled_at = 0.0
        self._seq = 0
        self._send_q: "queue.Queue[bytes]" = queue.Queue()
        self._input_dirty = threading.Event()
        self._lock = threading.Lock()
        self.on_rumble: Optional[Callable[[float, float, int], None]] = None
        self.last_error: Optional[str] = None

    # -- shared gamepad state -------------------------------------------------

    def set_button(self, index: int, pressed: bool) -> None:
        if index == 8:                       # Guide: its own GIP packet
            if pressed != self._guide:
                self._guide = pressed
                self._queue_guide()
            return
        if 0 <= index < 11:
            if pressed:
                self._buttons |= (1 << index)
            else:
                self._buttons &= ~(1 << index)
            self._input_dirty.set()

    def set_axis(self, index: int, value: int) -> None:
        if 0 <= index < 4:
            self._axes[index] = max(-32768, min(32767, value))
            self._input_dirty.set()

    def set_trigger(self, index: int, value: int) -> None:
        if 0 <= index < 2:
            self._triggers[index] = max(0, min(255, value))
            self._input_dirty.set()

    def set_hat(self, value: int) -> None:
        self._hat = value if 0 <= value <= 8 else 8
        self._input_dirty.set()

    # Visualizer / service compatibility
    @property
    def buttons(self) -> int:
        return self._buttons | ((1 << 8) if self._guide else 0)

    @property
    def axes(self) -> list:
        return list(self._axes)

    @property
    def triggers(self) -> list:
        return list(self._triggers)

    @property
    def hat(self) -> int:
        return self._hat

    _ds4 = None  # not a DS4 (service touch-capability check)

    # -- lifecycle --------------------------------------------------------------

    def open(self) -> bool:
        """Write FunctionFS descriptors, bind the UDC, start the GIP threads."""
        self.last_error = None
        try:
            self._ep0 = os.open(f"{self.mount}/ep0", os.O_RDWR)
            os.write(self._ep0, build_ffs_descriptors())
            os.write(self._ep0, build_ffs_strings())
        except OSError as e:
            self.last_error = f"FunctionFS setup failed ({self.mount}/ep0): {e}"
            logger.error(self.last_error)
            self.close()
            return False
        if not self._bind_udc():
            self.close()
            return False
        try:
            self._ep_in = os.open(f"{self.mount}/ep1", os.O_WRONLY)
            self._ep_out = os.open(f"{self.mount}/ep2", os.O_RDONLY)
        except OSError as e:
            self.last_error = f"FunctionFS endpoints unavailable: {e}"
            logger.error(self.last_error)
            self.close()
            return False
        self._running = True
        for target in (self._ep0_loop, self._reader_loop, self._writer_loop, self._timer_loop):
            threading.Thread(target=target, daemon=True).start()
        logger.info("Wired Xbox (GIP) gadget bound — waiting for the host to configure it")
        return True

    def close(self) -> None:
        self._running = False
        self._streaming = False
        self._input_dirty.set()
        self._send_q.put(b"")
        self._unbind_udc()
        for attr in ("_ep_in", "_ep_out", "_ep0"):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)

    def _bind_udc(self) -> bool:
        try:
            udcs = sorted(os.listdir("/sys/class/udc"))
        except OSError:
            udcs = []
        if not udcs:
            self.last_error = ("No UDC — enable BIOS USB Dual Role Device (DRD), "
                               "cold boot, and use a data USB-C cable")
            logger.error(self.last_error)
            return False
        try:
            with open(f"{self.gadget_dir}/UDC", "w") as f:
                f.write(udcs[0])
            logger.info("UDC %s bound for the Xbox gadget", udcs[0])
            return True
        except OSError as e:
            self.last_error = f"Could not bind UDC {udcs[0]}: {e}"
            logger.error(self.last_error)
            return False

    def _unbind_udc(self) -> None:
        try:
            with open(f"{self.gadget_dir}/UDC", "w") as f:
                f.write("\n")
        except OSError:
            pass

    # -- threads ----------------------------------------------------------------

    def _ep0_loop(self) -> None:
        """FunctionFS control events. ENABLE = the host selected our config."""
        while self._running and self._ep0 is not None:
            try:
                data = os.read(self._ep0, FFS_EVENT_SIZE * 4)
            except OSError as e:
                if self._running:
                    logger.debug("ep0 read: %s", e)
                    time.sleep(0.1)
                continue
            for off in range(0, len(data) - FFS_EVENT_SIZE + 1, FFS_EVENT_SIZE):
                event = data[off:off + FFS_EVENT_SIZE]
                etype = event[8]
                if etype == FFS_EVENT_ENABLE:
                    logger.info("Wired Xbox: host configured the controller")
                    with self._lock:
                        self._enabled = True
                        self._host_spoke = False
                        self._streaming = False
                        self._enabled_at = time.monotonic()
                        self._seq = 0
                    self._send_q.put(build_announce())
                elif etype in (FFS_EVENT_DISABLE, FFS_EVENT_UNBIND):
                    logger.info("Wired Xbox: host released the controller")
                    with self._lock:
                        self._enabled = False
                        self._streaming = False
                elif etype == FFS_EVENT_SETUP:
                    self._stall_setup(event)

    def _stall_setup(self, event: bytes) -> None:
        """Unknown class/vendor control request for our interface: stall it
        (FunctionFS stalls when the data phase goes the wrong direction)."""
        request_type = event[0]
        try:
            if request_type & USB_DIR_IN:
                os.read(self._ep0, 0)
            else:
                os.write(self._ep0, b"")
        except OSError:
            pass

    def _reader_loop(self) -> None:
        while self._running and self._ep_out is not None:
            try:
                packet = os.read(self._ep_out, 64)
            except OSError as e:
                if self._running:
                    logger.debug("ep2 read: %s", e)
                    time.sleep(0.05)
                continue
            if len(packet) >= 4:
                self._handle_host_packet(packet)

    def _writer_loop(self) -> None:
        """Single writer for ep1: protocol packets first, then the latest
        input state (never a backlog of stale inputs)."""
        while self._running:
            try:
                packet = self._send_q.get(timeout=0.004)
            except queue.Empty:
                packet = None
            if packet:
                self._write(packet)
                continue
            if self._streaming and self._input_dirty.is_set():
                self._input_dirty.clear()
                payload = build_input_payload(self._buttons, self._axes, self._triggers, self._hat)
                self._write(bytes([GIP_CMD_INPUT, 0x00, self._next_seq(), GIP_INPUT_PAYLOAD]) + payload)

    def _timer_loop(self) -> None:
        """Re-announce until the host talks; start streaming on power-on or
        after a fallback delay (hosts differ in what they send first)."""
        last_announce = 0.0
        while self._running:
            time.sleep(0.05)
            with self._lock:
                enabled, spoke, streaming, since = (
                    self._enabled, self._host_spoke, self._streaming, self._enabled_at,
                )
            if not enabled or streaming:
                continue
            now = time.monotonic()
            if not spoke and now - last_announce >= ANNOUNCE_INTERVAL_S:
                last_announce = now
                self._send_q.put(build_announce())
            if now - since >= STREAM_FALLBACK_S:
                self._start_streaming("fallback — no power-on from the host")

    # -- protocol ---------------------------------------------------------------

    def _next_seq(self) -> int:
        with self._lock:
            self._seq = (self._seq % 255) + 1
            return self._seq

    def _write(self, packet: bytes) -> None:
        if self._ep_in is None or not self._enabled:
            return
        if packet[0] != GIP_CMD_ACK and packet[2] == 0:
            packet = packet[:2] + bytes([self._next_seq()]) + packet[3:]
        try:
            os.write(self._ep_in, packet)
        except OSError as e:
            logger.debug("ep1 write: %s", e)

    def _start_streaming(self, reason: str) -> None:
        with self._lock:
            if self._streaming:
                return
            self._streaming = True
        logger.info("Wired Xbox: streaming input (%s)", reason)
        self._input_dirty.set()

    def _queue_guide(self) -> None:
        if self._streaming:
            self._send_q.put(bytes([GIP_CMD_VIRTUAL_KEY, GIP_OPT_INTERNAL, 0, 2,
                                    0x01 if self._guide else 0x00, 0x5B]))

    def _handle_host_packet(self, packet: bytes) -> None:
        cmd, options, seq, length = packet[0], packet[1], packet[2], packet[3]
        with self._lock:
            self._host_spoke = True
        if self.verbose:
            logger.debug("GIP host packet %s", packet.hex())
        if options & GIP_OPT_ACK:
            self._send_q.put(build_ack(cmd, options, seq, length))
        if cmd == GIP_CMD_POWER:
            if len(packet) > 4 and packet[4] == GIP_PWR_ON:
                self._start_streaming("host sent power-on")
        elif cmd == GIP_CMD_RUMBLE:
            rumble = parse_rumble(packet)
            if rumble and self.on_rumble:
                try:
                    self.on_rumble(rumble["strong"], rumble["weak"], 10000)
                except Exception as e:
                    logger.debug("rumble callback: %s", e)
        elif cmd in (GIP_CMD_LED, GIP_CMD_AUTHENTICATE, GIP_CMD_IDENTIFY):
            # LED/auth-done need nothing; identify is optional (SDL times out)
            self._start_streaming(f"host sent 0x{cmd:02x}")
