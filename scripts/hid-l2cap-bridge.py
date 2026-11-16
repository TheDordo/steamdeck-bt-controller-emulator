#!/usr/bin/env python3
"""Root-only L2CAP holder for the Pro Controller HID profile.

The kernel requires CAP_NET_BIND_SERVICE to bind well-known L2CAP PSMs
(0x11 = HID control, 0x13 = HID interrupt), so the unprivileged app cannot
bind them directly. This bridge runs via passwordless sudo (see
config/bt-controller-emulator-sudoers), binds both PSMs, and relays every
accepted connection message-for-message over local SOCK_SEQPACKET unix
sockets (abstract namespace) to the app, which keeps all HIDP logic.

Usage (invoked by the app):
    sudo -n python3 hid-l2cap-bridge.py --ctrl <unix> --intr <unix> --bdaddr <AA:BB:...>
"""

import argparse
import errno
import os
import select
import socket
import subprocess
import sys
import threading
import time


def pump(a, b):
    """Relay messages between two SOCK_SEQPACKET sockets until one closes."""
    socks = [a, b]
    while True:
        readable, _, _ = select.select(socks, [], [])
        for s in readable:
            try:
                data = s.recv(4096)
            except OSError:
                data = b""
            if not data:
                return
            peer = b if s is a else a
            try:
                peer.sendall(data)
            except OSError:
                return


def try_free_hidp():
    """Best-effort unload of the kernel hidp module (HID host role)."""
    try:
        subprocess.run(
            ["modprobe", "-r", "hidp"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def diagnose_busy():
    """Collect evidence about who holds the well-known HID PSMs."""
    parts = []
    try:
        out = subprocess.run(
            ["cat", "/proc/net/l2cap"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l for l in out.stdout.splitlines() if "0011" in l or "0013" in l]
        parts.append(
            "L2CAP listeners on PSM 0x11/0x13 (kernel 'hidp' lines mean a "
            "Bluetooth keyboard/mouse session on this machine):\n"
            + ("\n".join(lines) if lines else "  (none listed)")
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
            ["ss", "-f", "bluetooth", "-lpn"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [
            l for l in out.stdout.splitlines()
            if " 17 " in l or " 19 " in l or "psm" in l.lower() or "0x11" in l or "0x13" in l
        ]
        parts.append(
            "ss bluetooth listeners (PIDs shown):\n"
            + ("\n".join(lines) if lines else "  (none listed)")
        )
    except Exception:
        parts.append("(ss -f bluetooth unsupported)")
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=5,
        )
        lines = [
            l for l in out.stdout.splitlines()
            if "hogp" in l or "hid-l2cap" in l
        ]
        parts.append(
            "matching processes:\n" + ("\n".join(lines) if lines else "  (none)")
        )
    except Exception:
        parts.append("(could not run ps)")
    return "\n".join(parts)


def bind_with_retries(listener, bdaddr, psm, attempts=30, delay=0.5):
    """Bind one PSM, tolerating a briefly-busy port (a previous bridge
    instance still dying) and freeing the kernel hidp module's well-known
    PSMs if it holds them."""
    last_err = None
    try_free_hidp()  # free the ports before the first attempt
    for attempt in range(attempts):
        try:
            listener.bind((bdaddr, psm))
            return True
        except OSError as e:
            last_err = e
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            if attempt == 3 or attempt == attempts - 2:
                # hidp can be re-loaded by BlueZ mid-flight; retry unloading.
                try_free_hidp()
            time.sleep(delay)
    print(
        "HID L2CAP bridge: PSM 0x%x is still busy (%s).\n%s\n"
        "Recovery: pgrep -af 'hogp|hid-l2cap' to find strays, then\n"
        "  sudo pkill -f 'hid-l2cap-bridg[e]'\n"
        "and/or disconnect Bluetooth keyboards/mice from this machine\n"
        "('bluetoothctl devices Connected' + 'bluetoothctl disconnect <MAC>'),\n"
        "then start the service again."
        % (psm, last_err, diagnose_busy()),
        file=sys.stderr,
    )
    return False


def serve_l2cap(psm, bdaddr, unix_name):
    """Bind one L2CAP PSM and relay accepted connections to the app."""
    listener = socket.socket(
        socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
    )
    if not bind_with_retries(listener, bdaddr, psm):
        os._exit(1)
    listener.listen(1)
    while True:
        conn, _addr = listener.accept()
        # The app owns the unix socket; retry briefly while it (re)starts.
        us = None
        for _ in range(50):
            try:
                us = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                us.connect("\0" + unix_name)  # abstract namespace
                break
            except OSError:
                if us is not None:
                    us.close()
                us = None
                time.sleep(0.1)
        if us is None:
            conn.close()
            continue
        pump(conn, us)
        try:
            conn.close()
        except OSError:
            pass
        try:
            us.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctrl", required=True, help="app unix socket for the control channel")
    ap.add_argument("--intr", required=True, help="app unix socket for the interrupt channel")
    ap.add_argument("--bdaddr", required=True, help="local adapter BD_ADDR")
    args = ap.parse_args()

    threads = [
        threading.Thread(target=serve_l2cap, args=(0x11, args.bdaddr, args.ctrl), daemon=True),
        threading.Thread(target=serve_l2cap, args=(0x13, args.bdaddr, args.intr), daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()