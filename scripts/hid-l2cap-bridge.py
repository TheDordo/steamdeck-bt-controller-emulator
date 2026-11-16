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
import select
import socket
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


def serve_l2cap(psm, bdaddr, unix_name):
    """Bind one L2CAP PSM and relay accepted connections to the app."""
    listener = socket.socket(
        socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
    )
    listener.bind((bdaddr, psm))
    listener.listen(1)
    while True:
        conn, _addr = listener.accept()
        # The app owns the unix socket; retry briefly while it (re)starts.
        us = None
        for _ in range(50):
            try:
                us = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                us.connect(unix_name)
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