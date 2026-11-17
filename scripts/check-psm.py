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

A dynamic-PSM probe (0x1001) is included for comparison: if 0x1001 binds
while 0x11/0x13 fail, the conflict is specific to those ports (a real
squatter); if 0x1001 also fails, something blocks L2CAP binding entirely.
"""

import errno
import os
import socket
import subprocess
import sys

HID_CTRL_PSM = 0x11
HID_INTR_PSM = 0x13
DYN_PSM = 0x1001

ERRNO_HINTS = {
    "EADDRINUSE": "another socket/listener holds the PSM (see the evidence below)",
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


def socket_owners() -> dict:
    """Map socket inode -> 'pid (comm)' by walking /proc."""
    owners: dict = {}
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return owners
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except OSError:
            comm = "?"
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                owners.setdefault(target[8:-1], f"{pid} ({comm})")
    return owners


def debugfs_l2cap() -> str:
    mount_point = "/sys/kernel/debug"
    path = f"{mount_point}/bluetooth/l2cap"
    try:
        if not os.path.exists(path):
            os.makedirs(mount_point, exist_ok=True)
            subprocess.run(
                ["mount", "-t", "debugfs", "debugfs", mount_point],
                capture_output=True, timeout=10,
            )
        if os.path.exists(path):
            out = subprocess.run(
                ["cat", path], capture_output=True, text=True, timeout=5,
            )
            return out.stdout.strip() or "(empty debugfs l2cap table)"
        return "(debugfs mount failed)"
    except Exception as e:
        return f"(debugfs unavailable: {e})"


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
    print(f"bind PSM 0x1001 (dynamic) : {try_bind('00:00:00:00:00:00', DYN_PSM)}")
    print()

    print("--- /proc/net/l2cap (kernel view; newer kernels have no PSM column) ---")
    owners = socket_owners()
    try:
        out = subprocess.run(
            ["cat", "/proc/net/l2cap"], capture_output=True, text=True, timeout=5,
        )
        lines = out.stdout.splitlines()
        if not lines:
            print("  (empty — no L2CAP sockets at all)")
        for line in lines:
            mark = "   <<< contains 0011/0013" if ("0011" in line or "0013" in line) else ""
            owner = ""
            if not line.startswith("sk ") and line.strip():
                fields = line.split()
                if len(fields) >= 6:
                    owner = owners.get(fields[5], "")
                    if owner:
                        owner = "   <<< owned by " + owner
            print("  " + line + mark + owner)
    except Exception as e:
        print(f"  (could not read: {e})")
    print()

    print("--- debugfs L2CAP channel table (PSM + state per channel) ---")
    for line in debugfs_l2cap().splitlines():
        mark = "   <<< PSM 0x11/0x13" if ("0x0011" in line or "0x0013" in line) else ""
        print("  " + line + mark)
    print()

    print("--- hidp kernel module ---")
    try:
        out = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if line.startswith("hidp"):
                print("  " + line)
                break
        else:
            print("  (not loaded / built-in)")
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
