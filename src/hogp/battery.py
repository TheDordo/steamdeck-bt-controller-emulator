#!/usr/bin/env python3
"""
Steam Deck battery status helpers (sysfs).

Used by every HID identity that reports battery:
    * Pro Controller (BR/EDR)     - byte 2 of the 0x30 input report
    * HID-over-GATT Battery Service (0x180F / 0x2A19)
    * DualShock 4 USB input report

The Deck exposes the battery at /sys/class/power_supply/BAT1 (BAT0 on some
distros). All helpers fall back to a full battery when sysfs is unavailable.
"""

import glob
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_PERCENT = 100


def _battery_dir():
    for name in ("BAT1", "BAT0"):
        path = f"/sys/class/power_supply/{name}"
        if os.path.isdir(path):
            return path
    for path in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        if os.path.isdir(path):
            return path
    return None


def read_battery_percent() -> int:
    """Battery charge 0-100 (falls back to 100 when sysfs is unavailable)."""
    directory = _battery_dir()
    if directory is None:
        return _DEFAULT_PERCENT
    try:
        with open(os.path.join(directory, "capacity")) as f:
            value = int(f.read().strip())
        return max(0, min(100, value))
    except (OSError, ValueError):
        return _DEFAULT_PERCENT


def battery_charging() -> bool:
    """True while the battery is charging (or full on AC)."""
    directory = _battery_dir()
    if directory is None:
        return False
    try:
        with open(os.path.join(directory, "status")) as f:
            return f.read().strip().lower() in ("charging", "full")
    except OSError:
        return False


def pro_controller_battery_level(percent: int) -> int:
    """Map 0-100% to the Pro Controller 3-bit level (0=empty .. 4=full).

    Kernel hid-nintendo.c parses byte 2 bits 5-7 as this level, so on the
    wire the nibble is (level << 1): 0, 2, 4, 6, 8.
    """
    if percent >= 90:
        return 4
    if percent >= 50:
        return 3
    if percent >= 20:
        return 2
    if percent >= 5:
        return 1
    return 0