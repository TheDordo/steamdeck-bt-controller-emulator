"""
BlueZ org.bluez.Agent1 — auto-accept pairing for HoG peripheral mode.

Without a default agent, centrals may see the Deck advertise but fail to
complete pairing/connection (no RequestConfirmation handler).
"""

import logging
from typing import List, Optional

from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

AGENT_IFACE = "org.bluez.Agent1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"
BLUEZ_SERVICE = "org.bluez"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"


class PairingAgent:
    """Auto-accept inbound BLE pairing (default agent for peripheral mode)."""

    AGENT_PATH = "/com/steamdeck/hogp/agent"
    # Just Works LE pairing — KeyboardDisplay can stall macOS when we auto-accept.
    CAPABILITY = "NoInputNoOutput"

    def __init__(self, bus: Gio.DBusConnection, verbose: bool = False):
        self.bus = bus
        self.verbose = verbose
        self._registrations: List[int] = []
        self._registered_with_manager = False

    def register(self) -> bool:
        try:
            self._register_agent_object()
            self._register_with_agent_manager()
            return True
        except Exception as e:
            logger.error(f"Failed to register pairing agent: {e}")
            self.unregister()
            return False

    def unregister(self) -> None:
        # Drop the agent object before UnregisterAgent — otherwise call_sync can
        # deadlock waiting for Release while the GLib main loop is blocked.
        reg_ids = list(self._registrations)
        self._registrations.clear()
        for reg_id in reg_ids:
            try:
                self.bus.unregister_object(reg_id)
            except Exception as e:
                logger.debug(f"Agent unregister object: {e}")

        if self._registered_with_manager:
            try:
                self.bus.call_sync(
                    BLUEZ_SERVICE,
                    "/org/bluez",
                    AGENT_MANAGER_IFACE,
                    "UnregisterAgent",
                    GLib.Variant("(o)", (self.AGENT_PATH,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                )
            except GLib.Error as e:
                logger.debug(f"UnregisterAgent: {e}")
            self._registered_with_manager = False

    def _register_with_agent_manager(self) -> None:
        self.bus.call_sync(
            BLUEZ_SERVICE,
            "/org/bluez",
            AGENT_MANAGER_IFACE,
            "RegisterAgent",
            GLib.Variant("(os)", (self.AGENT_PATH, self.CAPABILITY)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self.bus.call_sync(
            BLUEZ_SERVICE,
            "/org/bluez",
            AGENT_MANAGER_IFACE,
            "RequestDefaultAgent",
            GLib.Variant("(o)", (self.AGENT_PATH,)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self._registered_with_manager = True
        logger.info("Pairing agent registered as default (auto-accept)")

    def _register_agent_object(self) -> None:
        xml = f"""
        <node>
            <interface name="{AGENT_IFACE}">
                <method name="Release"/>
                <method name="RequestPinCode">
                    <arg type="o" direction="in"/>
                    <arg type="s" direction="out"/>
                </method>
                <method name="DisplayPinCode">
                    <arg type="o" direction="in"/>
                    <arg type="s" direction="in"/>
                </method>
                <method name="RequestPasskey">
                    <arg type="o" direction="in"/>
                    <arg type="u" direction="out"/>
                </method>
                <method name="DisplayPasskey">
                    <arg type="o" direction="in"/>
                    <arg type="u" direction="in"/>
                    <arg type="q" direction="in"/>
                </method>
                <method name="RequestConfirmation">
                    <arg type="o" direction="in"/>
                    <arg type="u" direction="in"/>
                </method>
                <method name="RequestAuthorization">
                    <arg type="o" direction="in"/>
                </method>
                <method name="AuthorizeService">
                    <arg type="o" direction="in"/>
                    <arg type="s" direction="in"/>
                </method>
                <method name="Cancel"/>
            </interface>
        </node>
        """
        node_info = Gio.DBusNodeInfo.new_for_xml(xml)

        def handler(conn, sender, path, iface, method, params, invoc):
            device = params.unpack()[0] if params.n_children() else ""
            if method == "Release":
                invoc.return_value(None)
            elif method in (
                "RequestConfirmation",
                "RequestAuthorization",
                "AuthorizeService",
                "DisplayPasskey",
                "DisplayPinCode",
            ):
                logger.info(f"Pairing agent {method} for {device} — accepting")
                invoc.return_value(None)
            elif method == "Cancel":
                logger.debug("Agent Cancel")
                invoc.return_value(None)
            elif method == "RequestPinCode":
                invoc.return_value(GLib.Variant("(s)", ("0000",)))
            elif method == "RequestPasskey":
                invoc.return_value(GLib.Variant("(u)", (0,)))
            else:
                invoc.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod",
                    method,
                )

        reg_id = self.bus.register_object(
            self.AGENT_PATH,
            node_info.interfaces[0],
            handler,
            None,
            None,
        )
        self._registrations.append(reg_id)