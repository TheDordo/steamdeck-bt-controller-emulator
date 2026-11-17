#!/usr/bin/env python3
"""One-shot diagnostic: who holds the HID L2CAP PSMs 0x11/0x13?

Run as root on the Deck:
    sudo python3 ~/steamdeck-bt-controller-emulator/scripts/check-psm.py

Each bind attempt prints the exact kernel errno, which tells us everything:
    OK                the PSM is free right now
    EADDRINUSE (98)   another socket/listener holds the PSM
    EACCES (13)       the process lacks CAP_NET_BIND_SERVICE (not root?)
    EADDRNOTAVAIL (99) adapter down / address problem
    EAFNOSUPPORT     Bluetooth socket family unavailable
"""

import errno
import os
import socket
import subprocess
import sys

HID_CTRL_PSM = 0x11
HID_INTR_PSM = 0x13

ERRNO_HINTS = {
    "EADDRINUSE": "another socket/listener holds the PSM (see the /proc view below)",
    "EACCES": "process lacks CAP_NET_BIND_SERVICE — the daemon must run as root",
    "EADDRNOTAVAIL": "address not available — adapter down or wrong BDADDR?",
    "EAFNOSUPPORT": "Bluetooth socket family unavailable",
}


def try_bind(bdaddr: str, psm: int) -> str:
    try:
        s = socket.socket(
            socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP
        )
        s.bind((bdaddr, psm))
        s.close()
        return "OK"
    except OSError as e:
        name = errno.errorcode.get(e.errno, f"E{e.errno}")
        hint = ERRNO_HINTS.get(name, "")
        return f"FAILED {name} ({e.errno}) — {e} {hint}".strip()


def adapter_address() -> str:
    try:
        with open("/sys/class/bluetooth/hci0/address") as f:
            return f.read().strip()
    except OSError:
        return "00:00:00:00:00:00"


def main() -> int:
    print("=== HID L2CAP PSM diagnostic ===")
    print(f"euid: {os.geteuid()}  (must be 0 — well-known PSMs need CAP_NET_BIND_SERVICE)")
    addr = adapter_address()
    print(f"adapter address (sysfs): {addr}")
    print()
    print(f"bind PSM 0x11 (BDADDR_ANY): {try_bind('00:00:00:00:00:00', HID_CTRL_PSM)}")
    print(f"bind PSM 0x11 (adapter)   : {try_bind(addr, HID_CTRL_PSM)}")
    print(f"bind PSM 0x13 (BDADDR_ANY): {try_bind('00:00:00:00:00:00', HID_INTR_PSM)}")
    print(f"bind PSM 0x13 (adapter)   : {try_bind(addr, HID_INTR_PSM)}")
    print()

    print("--- /proc/net/l2cap (kernel view: LISTEN or CONNECTED) ---")
    try:
        out = subprocess.run(
            ["cat", "/proc/net/l2cap"], capture_output=True, text=True, timeout=5,
        )
        lines = out.stdout.splitlines()
        if not lines:
            print("  (empty — no L2CAP sockets at all)")
        for line in lines:
            mark = "   <<< PSM 0x11/0x13" if ("0011" in line or "0013" in line) else ""
            print("  " + line + mark)
    except Exception as e:
        print(f"  (could not read: {e})")
    print()

    print("--- hidp kernel module ---")
    try:
        out = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if line.startswith("hidp"):
                print("  " + line)
                break
        else:
            print("  (not loaded)")
    except Exception as e:
        print(f"  (could not run lsmod: {e})")
    print()

    print("--- emulator / bridge processes ---")
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,user,args"], capture_output=True, text=True, timeout=5,
        )
        found = False
        for line in out.stdout.splitlines():
            if "hogp" in line or "hid-l2cap" in line or "btemu" in line:
                print("  " + line)
                found = True
        if not found:
            print("  (none)")
    except Exception as e:
        print(f"  (could not run ps: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
