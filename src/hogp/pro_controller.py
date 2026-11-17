#!/usr/bin/env python3
"""
Nintendo Switch Pro Controller emulation over classic Bluetooth (BR/EDR HID).

Why this exists
---------------
macOS pairs and natively drives real Switch Pro Controllers through the
GameController framework (macOS 13+). The Pro Controller is NOT a BLE HOGP
device: it is a classic Bluetooth HID device speaking HIDP over L2CAP on
PSM 0x11 (control) and PSM 0x13 (interrupt), with an SDP record carrying its
vendor-defined HID report map. Games that use GCExtendedGamepad ignore a
generic LE HOGP gamepad entirely, which is why this emulator exists.

This module makes the Steam Deck's BR/EDR controller look like a real
"Pro Controller" so macOS treats it as a natively supported gamepad.

Protocol references:
    * dekuNukem/Nintendo_Switch_Reverse_Engineering (bluetooth_hid_notes.md)
    * mart1nro/joycontrol (proven L2CAP server + SDP record + HIDP framing)
    * linux hid-nintendo.c (report struct, calibration, subcommand replies)
    * esp-cpp/espp switch_pro (subcommand reply engine, ACK values)
"""

import errno
import logging
import os
import re
import socket
import struct
import subprocess
import threading
import time
from typing import Optional, Tuple

from .battery import battery_charging, pro_controller_battery_level, read_battery_percent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Identity constants (match a real Pro Controller)
# ---------------------------------------------------------------------------
PRO_NAME = "Pro Controller"
PRO_CLASS_OF_DEVICE = 0x002508  # Peripheral / Gamepad

# L2CAP PSMs used by the HID profile
HID_CTRL_PSM = 0x11
HID_INTR_PSM = 0x13

# HIDP transaction header byte for DATA packets
HIDP_DATA = 0xA0
HIDP_DATA_INPUT = HIDP_DATA | 0x01   # 0xA1  (device -> host, interrupt)
HIDP_DATA_OUTPUT = HIDP_DATA | 0x02  # 0xA2  (host -> device, control)

# Output report IDs (host -> device)
OUTPUT_RUMBLE_SUBCMD = 0x01
OUTPUT_RUMBLE_ONLY = 0x10
OUTPUT_REQUEST_NFC_MCU = 0x11

# Subcommand IDs (device <- host)
SUBCMD_MANUAL_BT_PAIRING = 0x01
SUBCMD_REQ_DEVICE_INFO = 0x02
SUBCMD_SET_REPORT_MODE = 0x03
SUBCMD_TRIGGERS_ELAPSED = 0x04
SUBCMD_SET_SHIPMENT = 0x08
SUBCMD_SPI_FLASH_READ = 0x10
SUBCMD_SET_NFC_IR_CONFIG = 0x21
SUBCMD_SET_NFC_IR_STATE = 0x22
SUBCMD_SET_PLAYER_LIGHTS = 0x30
SUBCMD_SET_HOME_LIGHT = 0x38
SUBCMD_ENABLE_IMU = 0x40
SUBCMD_SET_IMU_SENSITIVITY = 0x41
SUBCMD_WRITE_IMU_REG = 0x42
SUBCMD_READ_IMU_REG = 0x43
SUBCMD_ENABLE_VIBRATION = 0x48

# Input report IDs (device -> host)
INPUT_STANDARD_FULL = 0x30
INPUT_SUBCMD_REPLY = 0x21

INPUT_REPORT_INTERVAL_S = 0.008  # ~125 Hz baseline (real Pro streams at 120 Hz)
MIN_SEND_INTERVAL_S = 0.004     # event-driven sends are not throttled below this

# ---------------------------------------------------------------------------
# HID report map (the real Pro Controller's Bluetooth HID descriptor,
# byte-for-byte from the SDP record shipped by joycontrol).
# ---------------------------------------------------------------------------
PRO_HID_REPORT_DESCRIPTOR = bytes([
    0x05, 0x01, 0x15, 0x00, 0x09, 0x04, 0xA1, 0x01, 0x85, 0x30, 0x05, 0x01,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x0A, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01,
    0x95, 0x0A, 0x55, 0x00, 0x65, 0x00, 0x81, 0x02, 0x05, 0x09, 0x19, 0x0B,
    0x29, 0x0E, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x04, 0x81, 0x02,
    0x75, 0x01, 0x95, 0x02, 0x81, 0x03, 0x0B, 0x01, 0x00, 0x01, 0x00, 0xA1,
    0x00, 0x0B, 0x30, 0x00, 0x01, 0x00, 0x0B, 0x31, 0x00, 0x01, 0x00, 0x0B,
    0x32, 0x00, 0x01, 0x00, 0x0B, 0x35, 0x00, 0x01, 0x00, 0x15, 0x00, 0x27,
    0xFF, 0xFF, 0x00, 0x00, 0x75, 0x10, 0x95, 0x04, 0x81, 0x02, 0xC0, 0x0B,
    0x39, 0x00, 0x01, 0x00, 0x15, 0x00, 0x25, 0x07, 0x35, 0x00, 0x46, 0x3B,
    0x01, 0x65, 0x14, 0x75, 0x04, 0x95, 0x01, 0x81, 0x02, 0x05, 0x09, 0x19,
    0x0F, 0x29, 0x12, 0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x04, 0x81,
    0x02, 0x75, 0x08, 0x95, 0x34, 0x81, 0x03, 0x06, 0x00, 0xFF, 0x85, 0x21,
    0x09, 0x01, 0x75, 0x08, 0x95, 0x3F, 0x81, 0x03, 0x85, 0x81, 0x09, 0x02,
    0x75, 0x08, 0x95, 0x3F, 0x81, 0x03, 0x85, 0x01, 0x09, 0x03, 0x75, 0x08,
    0x95, 0x3F, 0x91, 0x83, 0x85, 0x10, 0x09, 0x04, 0x75, 0x08, 0x95, 0x3F,
    0x91, 0x83, 0x85, 0x80, 0x09, 0x05, 0x75, 0x08, 0x95, 0x3F, 0x91, 0x83,
    0x85, 0x82, 0x09, 0x06, 0x75, 0x08, 0x95, 0x3F, 0x91, 0x83, 0xC0,
])

# SDP record advertised for the HID profile (from joycontrol; proven record).
SDP_RECORD_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<record>
    <attribute id="0x0001">
        <sequence>
            <uuid value="0x1124"/>
        </sequence>
    </attribute>
    <attribute id="0x0004">
        <sequence>
            <sequence>
                <uuid value="0x0100"/>
                <uint16 value="0x0011"/>
            </sequence>
            <sequence>
                <uuid value="0x0011"/>
            </sequence>
        </sequence>
    </attribute>
    <attribute id="0x0005">
        <sequence>
            <uuid value="0x1002"/>
        </sequence>
    </attribute>
    <attribute id="0x0006">
        <sequence>
            <uint16 value="0x656e"/>
            <uint16 value="0x006a"/>
            <uint16 value="0x0100"/>
        </sequence>
    </attribute>
    <attribute id="0x0009">
        <sequence>
            <sequence>
                <uuid value="0x1124"/>
                <uint16 value="0x0100"/>
            </sequence>
        </sequence>
    </attribute>
    <attribute id="0x000d">
        <sequence>
            <sequence>
                <sequence>
                    <uuid value="0x0100"/>
                    <uint16 value="0x0013"/>
                </sequence>
                <sequence>
                    <uuid value="0x0011"/>
                </sequence>
            </sequence>
        </sequence>
    </attribute>
    <attribute id="0x0100">
        <text value="Wireless Gamepad"/>
    </attribute>
    <attribute id="0x0101">
        <text value="Gamepad"/>
    </attribute>
    <attribute id="0x0102">
        <text value="Nintendo"/>
    </attribute>
    <attribute id="0x0200">
        <uint16 value="0x0100"/>
    </attribute>
    <attribute id="0x0201">
        <uint16 value="0x0111"/>
    </attribute>
    <attribute id="0x0202">
        <uint8 value="0x08"/>
    </attribute>
    <attribute id="0x0203">
        <uint8 value="0x00"/>
    </attribute>
    <attribute id="0x0204">
        <boolean value="true"/>
    </attribute>
    <attribute id="0x0205">
        <boolean value="true"/>
    </attribute>
    <attribute id="0x0206">
        <sequence>
            <sequence>
                <uint8 value="0x22"/>
                <text encoding="hex" value="050115000904a1018530050105091901290a150025017501950a5500650081020509190b290e150025017501950481027501950281030b01000100a1000b300001000b310001000b320001000b35000100150027ffff0000751095048102c00b39000100150025073500463b0165147504950181020509190f2912150025017501950481027508953481030600ff852109017508953f8103858109027508953f8103850109037508953f9183851009047508953f9183858009057508953f9183858209067508953f9183c0"/>
            </sequence>
        </sequence>
    </attribute>
    <attribute id="0x0207">
        <sequence>
            <sequence>
                <uint16 value="0x0409"/>
                <uint16 value="0x0100"/>
            </sequence>
        </sequence>
    </attribute>
    <attribute id="0x020b">
        <uint16 value="0x0100"/>
    </attribute>
    <attribute id="0x020c">
        <uint16 value="0x0c80"/>
    </attribute>
    <attribute id="0x020d">
        <boolean value="false"/>
    </attribute>
    <attribute id="0x020e">
        <boolean value="true"/>
    </attribute>
    <attribute id="0x020f">
        <uint16 value="0x0640"/>
    </attribute>
    <attribute id="0x0210">
        <uint16 value="0x0320"/>
    </attribute>
</record>
"""

# ---------------------------------------------------------------------------
# Emulated SPI flash (calibration / serial / config the host may read)
# ---------------------------------------------------------------------------
_STICK_CENTER = 2000
_STICK_DELTA = 1500  # max_above / min_below

# Left stick factory calibration (kernel hid_field_extract packing, 9 bytes):
# max_above | center | min_below, 12-bit values nibble-packed little-endian.
LEFT_STICK_CAL = bytes([0xDC, 0xC5, 0x5D, 0xD0, 0x07, 0x7D, 0xDC, 0xC5, 0x5D])
# Right stick: center | min_below | max_above.
RIGHT_STICK_CAL = bytes([0xD0, 0x07, 0x7D, 0xDC, 0xC5, 0x5D, 0xDC, 0xC5, 0x5D])

FLASH_SEGMENTS = {
    0x6020: bytes(24),        # IMU factory calibration (unused: zeroed)
    0x603D: LEFT_STICK_CAL,   # left stick factory calibration
    0x6046: RIGHT_STICK_CAL,  # right stick factory calibration
    0x6000: bytes(16),        # serial number region (zeros)
    0x8010: bytes(2),         # user-cal magic left  (absent -> factory used)
    0x801B: bytes(2),         # user-cal magic right (absent -> factory used)
    0x8026: bytes(2),         # user IMU cal magic    (absent -> factory used)
}


def flash_read(offset: int, size: int) -> bytes:
    """Return emulated SPI flash content for [offset, offset+size)."""
    out = bytearray(size)
    for addr in range(offset, offset + size):
        for seg_start, seg_data in FLASH_SEGMENTS.items():
            if seg_start <= addr < seg_start + len(seg_data):
                out[addr - offset] = seg_data[addr - seg_start]
                break
    return bytes(out)


def _free_hidp() -> None:
    """Best-effort unload of the kernel hidp module (HID host role). The
    kernel module holds the well-known HID PSMs while a Bluetooth keyboard/
    mouse is connected to the Deck, which blocks our listener."""
    try:
        subprocess.run(["modprobe", "-r", "hidp"], capture_output=True, timeout=5)
    except Exception:
        pass


def _kill_orphaned_hid_bridges() -> None:
    """Kill leftover HID L2CAP bridge processes from pre-rewrite installs.
    The old fork spawned scripts/hid-l2cap-bridge.py via sudo; after a crash
    the orphan keeps PSM 0x11/0x13 bound forever. The daemon runs as root,
    so plain pkill works. The bracketed pattern avoids matching pkill's own
    command line."""
    try:
        result = subprocess.run(
            ["pkill", "-f", "hid-l2cap-bridg[e]"],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0:
            logger.info("Killed orphaned HID L2CAP bridge process(es) from an older install")
    except Exception as e:
        logger.debug("pkill hid-l2cap-bridge: %s", e)


def _restart_bluetoothd() -> bool:
    """Restart bluetooth.service as a last resort to free the well-known HID
    PSMs: it closes every L2CAP socket bluetoothd owns and drops Deck-side
    HID connections. The daemon's name-owner watch tolerates the brief bus
    outage (sessions only self-heal when already active)."""
    try:
        result = subprocess.run(
            ["systemctl", "restart", "bluetooth"],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("bluetooth.service restarted")
            return True
        logger.warning("systemctl restart bluetooth failed: %s", result.stderr.strip())
        return False
    except Exception as e:
        logger.warning("systemctl restart bluetooth failed: %s", e)
        return False


def _diagnose_busy_psms() -> str:
    """Collect evidence about who holds the well-known HID PSMs.

    Uses only /proc + bluetoothctl (some SteamOS images ship an iproute2
    without 'ss -f bluetooth' support)."""
    parts = []
    try:
        out = subprocess.run(
            ["cat", "/proc/net/l2cap"],
            capture_output=True, text=True, timeout=5,
        )
        all_lines = out.stdout.splitlines()
        relevant = [l for l in all_lines if "0011" in l or "0013" in l]
        parts.append(
            "Kernel L2CAP view — PSM 0x11/0x13 entries (LISTEN or CONNECTED):\n"
            + ("\n".join(relevant) if relevant else "  (none listed)")
        )
        if all_lines:
            parts.append(
                "Full kernel L2CAP view:\n" + "\n".join("  " + l for l in all_lines[:15])
            )
    except Exception:
        parts.append("(could not read /proc/net/l2cap)")
    try:
        out = subprocess.run(
            ["lsmod"], capture_output=True, text=True, timeout=5,
        )
        hidp = [l for l in out.stdout.splitlines() if l.startswith("hidp")]
        parts.append(
            "hidp module: " + ("LOADED: " + hidp[0] if hidp else "not loaded")
        )
    except Exception:
        parts.append("(could not run lsmod)")
    try:
        out = subprocess.run(
            ["bluetoothctl", "devices", "Connected"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [
            l.strip() for l in out.stdout.splitlines()
            if l.strip() and "devices Connected" not in l
        ]
        parts.append(
            "Bluetooth devices connected to this Deck:\n"
            + ("\n".join("  " + l for l in lines) if lines else "  (none)")
        )
    except Exception:
        parts.append("(bluetoothctl unavailable)")
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=5,
        )
        lines = [
            l for l in out.stdout.splitlines()
            if "hogp" in l or "hid-l2cap" in l or "btemu" in l
        ]
        parts.append(
            "emulator-related processes:\n"
            + ("\n".join(lines) if lines else "  (none)")
        )
    except Exception:
        parts.append("(could not run ps)")
    return "\n".join(parts)


def _bind_l2cap_with_retries(
    bdaddr: str, psm: int, attempts: int = 20, delay: float = 0.5,
) -> Tuple[Optional[socket.socket], Optional[str]]:
    """Bind one well-known HID L2CAP PSM, tolerating a briefly-busy port.

    Clearance steps, in order:
      1. kill orphaned bridge processes from pre-rewrite installs,
      2. unload the kernel hidp module (holds the PSMs while a Bluetooth
         keyboard/mouse is connected to the Deck),
      3. if the port is still busy after retries, restart bluetooth.service
         ONCE as a last resort (closes L2CAP sockets it owns, drops Deck-side
         HID connections) and retry briefly.

    Runs as root inside the daemon, so CAP_NET_BIND_SERVICE is not an issue.

    Returns (listener, error_detail): the listener socket, or None with a
    diagnostic describing who holds the port when all attempts fail."""

    def attempt_once() -> socket.socket:
        listener = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
        )
        listener.bind((bdaddr, psm))
        listener.listen(1)
        return listener

    # Well-known PSMs require CAP_NET_BIND_SERVICE. Running the daemon by
    # hand as a normal user fails every bind with EACCES — fail fast with a
    # clear message instead of retrying and reporting "PSM busy".
    if os.geteuid() != 0:
        message = (
            f"the daemon is not running as root (euid={os.geteuid()}) — "
            "well-known HID PSMs need CAP_NET_BIND_SERVICE. Start it via "
            "systemd: sudo systemctl start bt-controller-emulator"
        )
        logger.error("HID L2CAP PSM 0x%x bind: %s", psm, message)
        return None, message

    last_err = None
    _kill_orphaned_hid_bridges()
    _free_hidp()  # free the ports before the first attempt
    for attempt in range(attempts):
        try:
            return attempt_once(), None
        except OSError as e:
            last_err = e
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            if attempt == 3 or attempt == attempts - 2:
                # hidp can be re-loaded by BlueZ mid-flight; retry clearing.
                _kill_orphaned_hid_bridges()
                _free_hidp()
            time.sleep(delay)

    # Last resort: one bluetoothd restart frees everything BlueZ holds.
    if _restart_bluetoothd():
        logger.warning(
            "Restarted bluetooth.service to free PSM 0x%x — final bind attempts", psm,
        )
        time.sleep(2)
        for _ in range(8):
            try:
                return attempt_once(), None
            except OSError as e:
                last_err = e
                if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                    raise
                time.sleep(0.5)

    err_name = (
        errno.errorcode.get(last_err.errno, f"E{last_err.errno}")
        if last_err is not None else "?"
    )
    diagnosis = _diagnose_busy_psms()
    logger.error(
        "HID L2CAP PSM 0x%x bind failed with %s (%s) after clearing bridges, "
        "unloading hidp and restarting bluetooth.service.\n%s\n%s",
        psm, err_name, last_err, diagnosis,
        (
            "Remaining cause: a Bluetooth keyboard/mouse connected to the "
            "Deck itself (bluetoothctl devices Connected → bluetoothctl "
            "disconnect <MAC>), or another HID emulator process."
            if err_name == "EADDRINUSE" else
            "EACCES means the daemon lacks CAP_NET_BIND_SERVICE "
            "(not running as root?)."
        ),
    )
    return None, diagnosis


# ---------------------------------------------------------------------------
# Pro Controller emulator
# ---------------------------------------------------------------------------
class ProControllerEmulator:
    """BR/EDR HID peripheral: pairs as "Pro Controller", streams 0x30 reports,
    answers HIDP control traffic and the Pro subcommand protocol."""

    def __init__(self, adapter_address: str = "00:00:00:00:00:00", verbose: bool = False):
        self.adapter_address = adapter_address
        self.verbose = verbose

        # Shared gamepad state (same model as GattApplication/USBGadgetHID):
        # 11-button mask, 4 int16 axes, 2 uint8 triggers, hat 0..8.
        self._buttons = 0
        self._axes = [0, 0, 0, 0]
        self._triggers = [0, 0]
        self._hat = 0x08

        # Battery reporting (byte 2 of the input report)
        self._battery_level = 4      # 0=empty .. 4=full (kernel hid-nintendo)
        self._battery_charging = False
        self._battery_last_read = 0.0

        self._running = False
        self._connected = False
        self._streaming = False

        self._ctl_sock: Optional[socket.socket] = None   # L2CAP listener, control channel
        self._itr_sock: Optional[socket.socket] = None   # L2CAP listener, interrupt channel
        self._ctl_conn: Optional[socket.socket] = None   # accepted control conn
        self._itr_conn: Optional[socket.socket] = None   # accepted interrupt conn
        self.last_error: Optional[str] = None  # detailed start failure reason
        self._conn_lock = threading.Lock()

        self._threads = []
        self._timer = 0
        self._input_report_mode = None
        self._send_lock = threading.Lock()
        self._last_send_s = 0.0
        self._vibrator_cycle = [0x00, 0x70, 0xC0, 0xB0]
        self._vibrator_idx = 0

        # Callbacks for UI
        self.on_state_change = None  # callable(connected: bool)

    # -- state setters (called from the input handler / GUI) -----------------

    def set_button(self, index: int, pressed: bool) -> None:
        if 0 <= index < 11:
            if pressed:
                self._buttons |= (1 << index)
            else:
                self._buttons &= ~(1 << index)
            self._trigger_send()

    def set_axis(self, index: int, value: int) -> None:
        if 0 <= index < 4:
            self._axes[index] = max(-32768, min(32767, value))
            self._trigger_send()

    def set_trigger(self, index: int, value: int) -> None:
        if 0 <= index < 2:
            self._triggers[index] = max(0, min(255, value))
            self._trigger_send()

    def set_hat(self, value: int) -> None:
        if value == 0x0F:
            value = 0x08
        self._hat = value if 0 <= value <= 8 else 0x08
        self._trigger_send()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Bind the HID L2CAP PSMs (0x11/0x13) directly and start accepting
        connections. Runs inside the root daemon, so no bridge or sudo is
        needed. Adapter/SDP setup happens via bluez helpers before this."""
        self.last_error = None
        if self._running:
            return True
        try:
            self._ctl_sock, ctl_err = _bind_l2cap_with_retries(self.adapter_address, HID_CTRL_PSM)
            if self._ctl_sock is None:
                # Same conflict will block the interrupt PSM — fail fast.
                self._itr_sock, itr_err = None, None
            else:
                self._itr_sock, itr_err = _bind_l2cap_with_retries(self.adapter_address, HID_INTR_PSM)
        except OSError as e:
            self.last_error = f"local channel sockets: {e}"
            logger.error("Could not create L2CAP listeners: %s", e)
            self.stop()
            return False
        if self._ctl_sock is None or self._itr_sock is None:
            detail = (itr_err or ctl_err or "(no details)").replace("\n", " ")
            if "not running as root" in detail:
                # EACCES misdiagnosis guard: show the real reason verbatim.
                self.last_error = detail[:400]
            else:
                self.last_error = (
                    "PSM 0x11/0x13 busy — another HID service holds the ports. "
                    "The daemon cleared stray bridges, unloaded hidp, and "
                    "restarted bluetooth.service; if this persists, disconnect "
                    "Bluetooth keyboards/mice that are paired to the Deck "
                    "itself (bluetoothctl devices Connected → bluetoothctl "
                    "disconnect <MAC>), then Start again. "
                    "Details: " + detail[:400]
                )
            self.stop()
            return False

        self._running = True
        self._spawn(self._accept_loop, (self._ctl_sock, "control"))
        self._spawn(self._accept_loop, (self._itr_sock, "interrupt"))
        logger.info(
            "Pro Controller emulator listening (L2CAP PSM 0x%x/0x%x, direct bind)",
            HID_CTRL_PSM, HID_INTR_PSM,
        )
        return True

    def stop(self) -> None:
        self._running = False
        for sock in (self._ctl_sock, self._itr_sock, self._ctl_conn, self._itr_conn):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        self._ctl_sock = self._itr_sock = self._ctl_conn = self._itr_conn = None
        self._connected = False
        self._streaming = False
        self._threads = []

    def _spawn(self, target, args=()) -> None:
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        self._threads.append(t)

    def _accept_loop(self, sock: socket.socket, label: str) -> None:
        while self._running:
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            logger.info(f"Pro Controller: {label} channel connected from {addr}")
            with self._conn_lock:
                if label == "control":
                    old = self._ctl_conn
                    self._ctl_conn = conn
                else:
                    old = self._itr_conn
                    self._itr_conn = conn
                if old is not None:
                    try:
                        old.close()
                    except OSError:
                        pass
                became_connected = self._ctl_conn is not None and self._itr_conn is not None
            if label == "control":
                self._spawn(self._control_read_loop, (conn,))
            if became_connected and not self._streaming:
                self._connected = True
                self._streaming = True
                self._spawn(self._stream_loop)
                self._notify_state(True)
        logger.debug(f"Pro Controller: {label} accept loop ended")

    def _notify_state(self, connected: bool) -> None:
        if self.on_state_change:
            try:
                self.on_state_change(connected)
            except Exception:
                pass

    # -- HIDP control channel ------------------------------------------------

    def _control_read_loop(self, conn: socket.socket) -> None:
        """Read output reports + HIDP control transactions from the Mac."""
        while self._running:
            try:
                data = conn.recv(1024)
            except OSError:
                break
            if not data:
                break
            self._handle_control_packet(data)
        with self._conn_lock:
            if self._ctl_conn is conn:
                self._ctl_conn = None
            self._connected = self._ctl_conn is not None and self._itr_conn is not None
        if not self._connected:
            # Also end the stream loop so a reconnect starts a fresh one.
            self._streaming = False
            self._notify_state(False)

    def _handle_control_packet(self, data: bytes) -> None:
        if not data:
            return
        header = data[0]
        if header == HIDP_DATA_OUTPUT:
            self._handle_output_report(data[1:])
        else:
            # GET_REPORT / SET_REPORT / GET|SET_PROTOCOL / GET|SET_IDLE etc.
            # Reply with a successful HIDP handshake; macOS tolerates this the
            # same way it does with real HID peripherals that keep it minimal.
            if self.verbose:
                logger.debug(f"HIDP control packet {data.hex()} -> handshake")
            self._write_control(b"\x00")

    def _handle_output_report(self, payload: bytes) -> None:
        if len(payload) < 1:
            return
        report_id = payload[0]
        if report_id == OUTPUT_RUMBLE_SUBCMD:
            if len(payload) < 11:
                return
            subcmd = payload[10]
            subcmd_data = payload[11:]
            if self.verbose:
                logger.info(f"Pro Controller subcommand {subcmd:#04x} data={bytes(subcmd_data).hex()}")
            self._reply_to_subcommand(subcmd, subcmd_data)
        elif report_id == OUTPUT_RUMBLE_ONLY:
            pass  # rumble-only; nothing to do
        elif report_id == OUTPUT_REQUEST_NFC_MCU:
            logger.debug("NFC/IR MCU request ignored (no NFC hardware emulated)")
        else:
            logger.debug(f"Unknown output report {report_id:#04x} ignored")

    # -- subcommand replies ---------------------------------------------------

    def _reply_to_subcommand(self, subcmd: int, data: bytes) -> None:
        reply = bytearray(49)  # 0x21 input report, 49 data bytes
        ack, reply_id = 0x80, subcmd

        if subcmd == SUBCMD_REQ_DEVICE_INFO:
            ack = 0x82
            mac = _parse_mac(self.adapter_address)
            reply[15:17] = bytes([0x04, 0x00])  # firmware 4.00
            reply[17] = 0x03                     # controller type: Pro
            reply[18] = 0x02
            reply[19:25] = mac
            reply[25] = 0x01
            reply[26] = 0x01
        elif subcmd == SUBCMD_SET_REPORT_MODE:
            if data:
                self._input_report_mode = data[0]
                logger.info(f"Pro Controller input report mode set to {data[0]:#04x}")
        elif subcmd == SUBCMD_TRIGGERS_ELAPSED:
            ack = 0x83
            for i, ms in enumerate((3000, 3000, 0, 0, 0, 0, 0)):
                struct.pack_into("<H", reply, 15 + i * 2, ms // 10)
        elif subcmd == SUBCMD_SPI_FLASH_READ:
            ack = 0x90
            if len(data) >= 5:
                offset = struct.unpack("<I", data[0:4])[0]
                size = min(data[4], 0x1D)
                struct.pack_into("<I", reply, 15, offset)
                reply[19] = size
                reply[20:20 + size] = flash_read(offset, size)
        elif subcmd == SUBCMD_SET_NFC_IR_CONFIG:
            ack = 0xA0
            reply[15:15 + 34] = bytes([1, 0, 255, 0, 8, 0, 27, 1] + [0] * 25 + [200])
        elif subcmd == SUBCMD_SET_NFC_IR_STATE:
            ack = 0x80
        elif subcmd == SUBCMD_SET_PLAYER_LIGHTS:
            ack = 0x80
        elif subcmd == SUBCMD_SET_HOME_LIGHT:
            ack = 0x80
        elif subcmd == SUBCMD_ENABLE_IMU:
            ack = 0x80  # ACK; we stream zeroed IMU frames in 0x30 reports
        elif subcmd == SUBCMD_SET_IMU_SENSITIVITY:
            ack = 0x80
        elif subcmd == SUBCMD_WRITE_IMU_REG:
            ack = 0x80
        elif subcmd == SUBCMD_READ_IMU_REG:
            ack = 0x80
        elif subcmd == SUBCMD_ENABLE_VIBRATION:
            ack = 0x80
        elif subcmd == SUBCMD_SET_SHIPMENT:
            ack = 0x80
        elif subcmd == SUBCMD_MANUAL_BT_PAIRING:
            ack = 0x81
        else:
            logger.debug(f"Subcommand {subcmd:#04x} not implemented - generic ACK")

        reply[0] = INPUT_SUBCMD_REPLY
        reply[1] = self._timer & 0xFF
        reply[2] = ((self._battery_level << 5) & 0xE0) | (
            0x10 if self._battery_charging else 0x00
        )
        reply[13] = ack
        reply[14] = reply_id
        self._write_interrupt(bytes([HIDP_DATA_INPUT]) + bytes(reply))

    # -- input reports --------------------------------------------------------

    def _refresh_battery(self) -> None:
        """Re-read the Deck battery at most every 15 seconds."""
        now = time.monotonic()
        if now - self._battery_last_read < 15.0:
            return
        self._battery_last_read = now
        percent = read_battery_percent()
        self._battery_level = pro_controller_battery_level(percent)
        self._battery_charging = battery_charging()
        if self.verbose:
            logger.debug(
                f"Battery: {percent}% -> level {self._battery_level}, "
                f"charging={self._battery_charging}"
            )

    def _build_standard_report(self) -> bytes:
        """0x30 standard full input report (49 data bytes: 12 header +
        1 vibrator + 36 zeroed 6-axis frames)."""
        self._refresh_battery()
        report = bytearray(49)
        report[0] = INPUT_STANDARD_FULL
        report[1] = self._timer & 0xFF
        # byte 2: bits 5-7 = battery level (0..4), bit 4 = charging,
        # low nibble = connection info (0 = Pro Controller)
        report[2] = ((self._battery_level << 5) & 0xE0) | (
            0x10 if self._battery_charging else 0x00
        )

        # button bytes 3..5
        b3 = b4 = b5 = 0
        if self._buttons & (1 << 3):  b3 |= 0x01   # Y
        if self._buttons & (1 << 2):  b3 |= 0x02   # X
        if self._buttons & (1 << 1):  b3 |= 0x04   # B
        if self._buttons & (1 << 0):  b3 |= 0x08   # A
        if self._buttons & (1 << 5):  b3 |= 0x40   # RB -> R
        if self._buttons & (1 << 6):  b4 |= 0x01   # Back -> Minus
        if self._buttons & (1 << 7):  b4 |= 0x02   # Start -> Plus
        if self._buttons & (1 << 10): b4 |= 0x04   # R3 -> R stick
        if self._buttons & (1 << 9):  b4 |= 0x08   # L3 -> L stick
        if self._buttons & (1 << 8):  b4 |= 0x10   # Guide -> Home
        if self._buttons & (1 << 4):  b5 |= 0x40   # LB -> L
        if self._triggers[0] > 0:     b5 |= 0x80   # LT -> ZL
        if self._triggers[1] > 0:     b3 |= 0x80   # RT -> ZR
        # dpad bits (0=N,1=NE,2=E,3=SE,4=S,5=SW,6=W,7=NW)
        hat = self._hat if self._hat != 8 else -1
        if hat in (0, 1, 7):          b5 |= 0x02   # Up
        if hat in (3, 4, 5):          b5 |= 0x01   # Down
        if hat in (1, 2, 3):          b5 |= 0x04   # Right
        if hat in (5, 6, 7):          b5 |= 0x08   # Left
        report[3] = b3
        report[4] = b4
        report[5] = b5

        report[6:9] = self._pack_stick(self._axes[0], self._axes[1])
        report[9:12] = self._pack_stick(self._axes[2], self._axes[3])
        report[12] = self._vibrator_cycle[self._vibrator_idx]
        self._vibrator_idx = (self._vibrator_idx + 1) % len(self._vibrator_cycle)
        # bytes 13..48: zeroed 6-axis data (3 frames)
        return bytes(report)

    def _pack_stick(self, x: int, y: int) -> bytes:
        """int16 pair -> 3 nibble-packed bytes, calibrated around 2000."""
        raw_x = _STICK_CENTER + int(x / 32767.0 * _STICK_DELTA)
        raw_y = _STICK_CENTER + int(y / 32767.0 * _STICK_DELTA)
        raw_x = max(0, min(0xFFF, raw_x))
        raw_y = max(0, min(0xFFF, raw_y))
        return bytes([
            raw_x & 0xFF,
            ((raw_x >> 8) & 0x0F) | ((raw_y & 0x0F) << 4),
            (raw_y >> 4) & 0xFF,
        ])

    def _trigger_send(self) -> None:
        """Send the current state right away (buttons/hat/axes changed)."""
        if not self._streaming:
            return
        self._send_now(self._build_standard_report())

    def _send_now(self, report: bytes) -> bool:
        """Write one input report, rate-limited to MIN_SEND_INTERVAL_S."""
        with self._send_lock:
            now = time.monotonic()
            if now - self._last_send_s < MIN_SEND_INTERVAL_S:
                return True  # too soon; state goes out on the next tick
            if self._write_interrupt(bytes([HIDP_DATA_INPUT]) + report):
                self._last_send_s = now
                return True
            return False

    def _stream_loop(self) -> None:
        """Baseline 0x30 stream; state changes also trigger immediate sends."""
        logger.info("Pro Controller: streaming input reports")
        last = 0.0
        while self._running and self._streaming:
            now = time.monotonic()
            if now - last >= INPUT_REPORT_INTERVAL_S:
                last = now
                self._timer = (self._timer + 1) & 0xFF
                if not self._send_now(self._build_standard_report()):
                    break
            time.sleep(0.002)
        self._streaming = False
        logger.info("Pro Controller: stream loop ended")

    # -- channel writes -------------------------------------------------------

    def _write_control(self, data: bytes) -> bool:
        return self._write_to(self._ctl_conn, data)

    def _write_interrupt(self, data: bytes) -> bool:
        return self._write_to(self._itr_conn, data)

    def _write_to(self, conn: Optional[socket.socket], data: bytes) -> bool:
        if conn is None:
            return False
        try:
            conn.sendall(data)
            return True
        except OSError as e:
            logger.debug(f"L2CAP write failed: {e}")
            return False

    # -- status ---------------------------------------------------------------

    @property
    def notifying(self) -> bool:
        return self._connected

    @property
    def running(self) -> bool:
        return self._running

    @property
    def connected(self) -> bool:
        return self._connected

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


def _parse_mac(address: str) -> bytes:
    """'AA:BB:CC:DD:EE:FF' -> 6 bytes big-endian."""
    try:
        return bytes(int(part, 16) for part in address.split(":")[:6])
    except ValueError:
        return bytes(6)
