"""
Request a fast LE connection interval from the peripheral side.

On Bluetooth LE the CENTRAL (the Mac) picks the connection interval — the
fixed cadence at which the link carries data. Unless the peripheral asks,
macOS picks a power-saving interval (often 30 ms or more, sometimes with
slave latency), which dominates input latency. Real game controllers ask
for a short interval right after connecting; this module does the same.

How: an HCI LE Connection Update command on a raw HCI socket. On Bluetooth
4.1+ controllers, issuing it as the peripheral makes the controller run the
Connection Parameters Request procedure with the central, which may accept
or reject. The resulting interval comes back in the LE Connection Update
Complete event, which is logged.

Runs as root inside the daemon (raw HCI sockets need CAP_NET_RAW).
Pure Python + ioctl; no hcitool dependency (absent on newer images).
"""

import fcntl
import logging
import select
import socket
import struct
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# HCI constants (linux/include/net/bluetooth/hci.h)
SOL_HCI = 0
HCI_FILTER = 2
HCI_COMMAND_PKT = 0x01
HCI_EVENT_PKT = 0x04
EVT_CMD_STATUS = 0x0F
EVT_LE_META = 0x3E
LE_CONN_COMPLETE = 0x01
LE_CONN_UPDATE_COMPLETE = 0x03
LE_ENH_CONN_COMPLETE = 0x0A
OGF_LE = 0x08
OCF_LE_CONN_UPDATE = 0x0013
OPCODE_LE_CONN_UPDATE = (OGF_LE << 10) | OCF_LE_CONN_UPDATE
HCIGETCONNLIST = 0x800448D4  # _IOR('H', 212, int)
LE_LINK = 0x80
BT_CONNECTED = 1   # include/net/bluetooth/bluetooth.h
MAX_CONNS = 10

DEBUGFS_ROOT = "/sys/kernel/debug"

# Requested parameters. Units: interval 1.25 ms, timeout 10 ms.
# 11.25-15 ms is the fastest range Apple's accessory guidelines allow for
# HID devices; latency 0 so every interval carries fresh input.
FAST_MIN_INTERVAL = 9      # 11.25 ms
FAST_MAX_INTERVAL = 12     # 15 ms
FAST_LATENCY = 0
FAST_TIMEOUT = 300         # 3 s
# Fallback strictly within Apple's general rule (max >= min + 15 ms).
FALLBACK_MIN_INTERVAL = 12  # 15 ms
FALLBACK_MAX_INTERVAL = 24  # 30 ms


def _step(name: str, fn, *args):
    """Run one HCI step, tagging OSErrors with the step that failed."""
    try:
        return fn(*args)
    except OSError as e:
        raise OSError(e.errno, f"{name}: {e.strerror}") from e


def _hci_socket(dev_id: int) -> socket.socket:
    sock = _step("open raw HCI socket", socket.socket,
                 socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    try:
        _step(f"bind hci{dev_id}", sock.bind, (dev_id,))
    except OSError:
        sock.close()
        raise
    return sock


def list_le_connections(dev_id: int = 0) -> List[Dict]:
    """[{handle, address}] for current LE connections on hciN."""
    sock = _hci_socket(dev_id)
    try:
        # struct hci_conn_list_req { u16 dev_id; u16 conn_num;
        #   struct hci_conn_info { u16 handle; bdaddr_t bdaddr; u8 type;
        #   u8 out; u16 state; u32 link_mode; } conn_info[]; }  (16 bytes each)
        buf = bytearray(struct.pack("<HH", dev_id, MAX_CONNS) + bytes(16 * MAX_CONNS))
        _step("HCIGETCONNLIST ioctl", fcntl.ioctl, sock.fileno(), HCIGETCONNLIST, buf, True)
        _dev, num = struct.unpack_from("<HH", buf, 0)
        conns = []
        for i in range(num):
            handle, bdaddr, link_type, _out, _state, _mode = struct.unpack_from(
                "<H6sBBHI", buf, 4 + 16 * i,
            )
            # The list includes links still being set up — only tune live ones.
            if link_type == LE_LINK and _state == BT_CONNECTED:
                conns.append({
                    "handle": handle,
                    "address": ":".join(f"{b:02X}" for b in reversed(bdaddr)),
                })
        return conns
    finally:
        sock.close()


def _set_event_filter(sock: socket.socket) -> None:
    # struct hci_ufilter { u32 type_mask; u32 event_mask[2]; u16 opcode; }
    # is 16 bytes with tail padding. Newer kernels (copy_safe_from_sockptr)
    # reject a shorter optlen with EINVAL, so pad explicitly.
    type_mask = 1 << HCI_EVENT_PKT
    events = (1 << EVT_CMD_STATUS) | (1 << (EVT_LE_META - 32) << 32)
    sock.setsockopt(SOL_HCI, HCI_FILTER, struct.pack(
        "<IIIH2x", type_mask, events & 0xFFFFFFFF, events >> 32, 0,
    ))


def request_connection_update(
    handle: int, dev_id: int = 0,
    min_interval: int = FAST_MIN_INTERVAL, max_interval: int = FAST_MAX_INTERVAL,
    latency: int = FAST_LATENCY, timeout: int = FAST_TIMEOUT,
    wait_s: float = 4.0,
) -> Optional[float]:
    """Ask for new connection parameters on one LE connection.

    Returns the granted interval in ms (from LE Connection Update Complete),
    or None when the command/procedure failed or nothing came back in time."""
    sock = _hci_socket(dev_id)
    try:
        _step("set HCI event filter", _set_event_filter, sock)
        params = struct.pack(
            "<HHHHHHH", handle, min_interval, max_interval, latency, timeout, 0, 0,
        )
        _step("send LE Connection Update", sock.send,
              struct.pack("<BHB", HCI_COMMAND_PKT, OPCODE_LE_CONN_UPDATE, len(params)) + params)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            ready, _, _ = select.select([sock], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            pkt = sock.recv(260)
            if len(pkt) < 3 or pkt[0] != HCI_EVENT_PKT:
                continue
            evt = pkt[1]
            body = pkt[3:]
            if evt == EVT_CMD_STATUS and len(body) >= 4:
                status, _ncmd, opcode = struct.unpack_from("<BBH", body, 0)
                if opcode == OPCODE_LE_CONN_UPDATE and status != 0:
                    logger.warning(
                        "LE Connection Update rejected by the Deck's controller "
                        "(HCI status 0x%02x)", status,
                    )
                    return None
            elif evt == EVT_LE_META and body and body[0] == LE_CONN_UPDATE_COMPLETE and len(body) >= 10:
                status, h, interval, lat, sup = struct.unpack_from("<BHHHH", body, 1)
                if h != handle:
                    continue
                if status != 0:
                    logger.warning(
                        "Host rejected connection parameters %.2f-%.2f ms "
                        "(HCI status 0x%02x)",
                        min_interval * 1.25, max_interval * 1.25, status,
                    )
                    return None
                ms = interval * 1.25
                logger.info(
                    "LE connection 0x%04x: interval %.2f ms, latency %d, "
                    "supervision timeout %d ms", h, ms, lat, sup * 10,
                )
                return ms
        logger.info("No LE Connection Update Complete within %.1fs (host kept "
                    "its parameters)", wait_s)
        return None
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Kernel default range for PERIPHERAL connections
# ---------------------------------------------------------------------------
#
# net/bluetooth: for an incoming LE connection (we are the peripheral),
# hci_event.c copies hdev->le_conn_{min,max}_interval into the connection,
# and l2cap_le_conn_ready() sends an L2CAP Connection Parameter Update
# Request whenever the central's interval is OUTSIDE that range. The
# defaults (hci_core.c) are 0x0018-0x0028 = 30-50 ms: left alone, the kernel
# itself asks the Mac to slow a fast link down to 30-50 ms. The range is
# writable in debugfs (hci_debugfs.c), takes effect for new connections,
# and needs no bluetoothd restart.

def _debugfs_dir(dev_id: int) -> Optional[str]:
    import os
    import subprocess
    path = f"{DEBUGFS_ROOT}/bluetooth/hci{dev_id}"
    if not os.path.isdir(path):
        subprocess.run(["mount", "-t", "debugfs", "debugfs", DEBUGFS_ROOT],
                       capture_output=True, timeout=10)
    return path if os.path.isdir(path) else None


def _read_u(path: str) -> int:
    with open(path) as f:
        return int(f.read().strip())


def _write_u(path: str, value: int) -> None:
    with open(path, "w") as f:
        f.write(f"{value}\n")


def get_default_interval_range(dev_id: int = 0) -> Optional[tuple]:
    d = _debugfs_dir(dev_id)
    if d is None:
        return None
    try:
        return _read_u(f"{d}/conn_min_interval"), _read_u(f"{d}/conn_max_interval")
    except OSError:
        return None


def set_default_interval_range(min_interval: int, max_interval: int,
                               dev_id: int = 0) -> bool:
    """Set the kernel's peripheral interval range (units of 1.25 ms).
    The kernel enforces min <= max on every single write, so order the two
    writes so the intermediate state is always valid."""
    d = _debugfs_dir(dev_id)
    if d is None:
        logger.warning("debugfs unavailable — cannot change the kernel's LE "
                       "interval range (the kernel will request 30-50 ms)")
        return False
    lo, hi = f"{d}/conn_min_interval", f"{d}/conn_max_interval"
    try:
        current_max = _read_u(hi)
        if min_interval <= current_max:
            _write_u(lo, min_interval)
            _write_u(hi, max_interval)
        else:
            _write_u(hi, max_interval)
            _write_u(lo, min_interval)
        logger.info("Kernel LE peripheral interval range set to %.2f-%.2f ms",
                    min_interval * 1.25, max_interval * 1.25)
        return True
    except OSError as e:
        logger.warning("Could not set kernel LE interval range: %s", e)
        return False


def tune_connection(handle: int, dev_id: int = 0) -> Optional[float]:
    """Request the fast interval, falling back to an Apple-guideline range."""
    ms = request_connection_update(handle, dev_id)
    if ms is None:
        ms = request_connection_update(
            handle, dev_id,
            min_interval=FALLBACK_MIN_INTERVAL, max_interval=FALLBACK_MAX_INTERVAL,
        )
    return ms
