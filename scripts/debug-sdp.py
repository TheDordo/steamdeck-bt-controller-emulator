#!/usr/bin/env python3
"""Isolate HID SDP registration failures on the Deck.

Run this and share the output:

    python3 scripts/debug-sdp.py

It tests every GVariant construction and D-Bus call the Pro Controller mode
uses, so a failing step is identified exactly.
"""

import sys
import traceback

from gi.repository import GLib, Gio

PROFILE_PATH = "/com/steamdeck/hogp/procon"
TEST_UUID = "00000000-0000-0000-0000-000000000000"
SDP_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>'
    "<record>"
    '<attribute id="0x0001"><sequence><uuid value="0x1124"/></sequence></attribute>'
    "</record>"
)


def check(name, fn):
    try:
        result = fn()
        print(f"OK    {name}: {result!r}")
        return True
    except BaseException as e:
        print(f"FAIL  {name}: {type(e).__name__}: {e}")
        traceback.print_exc()
        return False


print("PyGObject version:", __import__("gi").__version__)

check("repr(PROFILE_PATH)", lambda: repr(PROFILE_PATH))
check("GLib.Variant('o', PROFILE_PATH)", lambda: GLib.Variant("o", PROFILE_PATH))
check(
    "GLib.Variant.new_object_path(PROFILE_PATH)",
    lambda: GLib.Variant.new_object_path(PROFILE_PATH),
)
check("GLib.Variant('(o)', (PROFILE_PATH,))", lambda: GLib.Variant("(o)", (PROFILE_PATH,)))
check(
    "GLib.Variant.new_tuple(new_object_path)",
    lambda: GLib.Variant.new_tuple(GLib.Variant.new_object_path(PROFILE_PATH)),
)
check(
    "GLib.Variant('(osa{sv})', tuple form)",
    lambda: GLib.Variant(
        "(osa{sv})",
        (
            PROFILE_PATH,
            TEST_UUID,
            {
                "ServiceRecord": GLib.Variant("s", SDP_XML),
                "Role": GLib.Variant("s", "server"),
                "RequireAuthentication": GLib.Variant("b", False),
                "RequireAuthorization": GLib.Variant("b", False),
            },
        ),
    ),
)

bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
print("bus unique name:", bus.get_unique_name())


def unregister():
    params = GLib.Variant("(o)", (PROFILE_PATH,))
    bus.call_sync(
        "org.bluez", "/org/bluez", "org.bluez.ProfileManager1",
        "UnregisterProfile", params, None, Gio.DBusCallFlags.NONE, 5000, None,
    )
    return "unregistered"


check("UnregisterProfile (D-Bus)", unregister)


def register():
    params = GLib.Variant(
        "(osa{sv})",
        (
            PROFILE_PATH,
            TEST_UUID,
            {
                "ServiceRecord": GLib.Variant("s", SDP_XML),
                "Role": GLib.Variant("s", "server"),
                "RequireAuthentication": GLib.Variant("b", False),
                "RequireAuthorization": GLib.Variant("b", False),
            },
        ),
    )
    bus.call_sync(
        "org.bluez", "/org/bluez", "org.bluez.ProfileManager1",
        "RegisterProfile", params, None, Gio.DBusCallFlags.NONE, 10000, None,
    )
    return "registered"


check("RegisterProfile (D-Bus, minimal record)", register)

print()
print("done")