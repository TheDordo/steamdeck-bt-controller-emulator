"""
Privileged emulator daemon (systemd: bt-controller-emulator.service).

Runs as root so that Bluetooth "just works" on the SteamOS holo image with
zero runtime sudo:

    * btmgmt adapter configuration runs directly (no sudoers rules),
    * the HID L2CAP PSMs 0x11/0x13 are bound in-process (no bridge process,
      no setcap Python copy),
    * evdev and /dev/hidg* are opened directly (no group memberships),
    * the USB gadget is configured by running scripts/setup-usb-gadget.sh.

The GUI (src/hogp/gui.py) and the CLI (src/hogp/main.py) are unprivileged
clients that talk to this daemon over D-Bus (com.steamdeck.btemu).

Startup sequencing is fully asynchronous: GATT/advertisement registration
must not use blocking call_sync because BlueZ calls back into our exported
objects while the registration call is in flight.
"""

import logging
import os
import signal
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from gi.repository import Gio, GLib

from .api import (
    BUS_NAME, OBJECT_PATH, MANAGER_IFACE, MANAGER_XML, ERROR_FAILED,
    STATUS_STOPPED, STATUS_STARTING, STATUS_STOPPING, STATUS_ACTIVE,
    STATUS_ERROR, STATUS_DEGRADED,
    MODE_BLUETOOTH, MODE_USB, PROFILE_GAMEPAD, PROFILE_KEYBOARD, PROFILE_XBOX, HOGP_PROFILES,
    KEY_STATUS, KEY_MODE, KEY_PROFILE, KEY_DETAIL, KEY_ERROR, KEY_CONN,
    KEY_CONNECTED, KEY_PAIRED, KEY_TRUSTED, KEY_INPUT, KEY_BATTERY,
    KEY_DEVICE_NAME, KEY_DEVICE_ADDR, KEY_READY, KEY_KB_CAPABLE,
    KEY_BUTTONS, KEY_AXES, KEY_TRIGGERS, KEY_HAT,
)
from . import bluez
from .adv import Advertisement
from .battery import read_battery_percent
from .gatt_app import GattApplication
from .input_handler import InputHandler
from .pairing_agent import PairingAgent
from .pro_controller import (
    PRO_NAME,
    PRO_PRODUCT_ID,
    PRO_VENDOR_ID,
    PRO_VENDOR_ID_SOURCE,
    PRO_VERSION,
    SDP_RECORD_XML,
    ProControllerEmulator,
)
from .usb_gadget import USBGadgetHID
from .xbox import (
    XBOX_NAME,
    XBOX_PRODUCT_ID,
    XBOX_VENDOR_ID,
    XBOX_VERSION,
    XboxGattApplication,
)

logger = logging.getLogger(__name__)

ADAPTER_NAME = "hci0"
HOGP_DEVICE_NAME = "SteamDeckPad"
PRO_DEVICE_ID = bluez.pro_device_id_value(
    PRO_VENDOR_ID_SOURCE, PRO_VENDOR_ID, PRO_PRODUCT_ID, PRO_VERSION,
)
XBOX_DEVICE_ID = bluez.pro_device_id_value(0x0002, XBOX_VENDOR_ID, XBOX_PRODUCT_ID, XBOX_VERSION)
STATIC_BLE_ADDRESS = "C2:12:34:56:78:9A"
REPORT_RATE_HZ = 25

# Recovery bounds for the self-healing watchdog
MAX_REGISTRATION_RETRIES = 4
MAX_RECOVERIES = 3
ADAPTER_POLL_TRIES = 15        # 15 x 1 s waiting for hci0 after bluetoothd is up
BLUEZ_WAIT_TRIES = 30          # 30 x 1 s waiting for bluetoothd on Start
BOND_RETRY_S = 20.0            # minimum gap between Pair() attempts per device
INPUT_COALESCE_MS = 4          # stick/trigger coalesce (matches the old GUI)
WATCHDOG_INTERVAL_MS = 5000
BATTERY_INTERVAL_MS = 30000
USB_SETUP_TIMEOUT_S = 120


def _run_script(script_name: str, *args: str, timeout: float = 120.0) -> "subprocess.CompletedProcess":
    """Run a repo script (root daemon: no sudo/pkexec needed)."""
    script = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "scripts", script_name,
    )
    return subprocess.run(
        ["bash", script, *args],
        capture_output=True, text=True, timeout=timeout,
    )


class EmulatorService:
    """Root daemon: owns BlueZ registration, L2CAP sockets, evdev input and
    the USB gadget; exposes com.steamdeck.btemu on the system bus."""

    def __init__(self) -> None:
        self.bus: Optional[Gio.DBusConnection] = None
        self._main_loop: Optional[GLib.MainLoop] = None
        self._registrations = []
        self._shutting_down = False
        self._exit_after_stop = False

        # D-Bus-visible state
        self._status = STATUS_STOPPED
        self._mode = ""
        self._profile = ""
        self._detail = "Stopped"
        self._error = ""
        self._conn = "Not connected"
        self._connected = False
        self._paired = False
        self._trusted = False
        self._input_label = ""
        self._battery = read_battery_percent()
        self._device_name = ""
        self._device_addr = ""

        # Active session (None when stopped)
        self._session: Optional[Dict[str, Any]] = None
        self._generation = 0

        # Start-sequence state
        self._bluez_wait_left = 0
        self._bluez_wait_source: Optional[int] = None
        self._adapter_tries_left = ADAPTER_POLL_TRIES
        self._adapter_poll_source: Optional[int] = None

        # BlueZ owner watch (self-heal after bluetoothd restarts)
        self._bluez_watch_id: Optional[int] = None

        # Watchdog / battery timers
        self._watchdog_source: Optional[int] = None
        self._battery_source: Optional[int] = None

        # Input coalescing (evdev callbacks -> GLib main thread)
        self._pending_buttons: Dict[int, bool] = {}
        self._pending_axes: Dict[int, int] = {}
        self._pending_triggers: Dict[int, int] = {}
        self._pending_hat: Optional[int] = None
        self._coalesce_source: Optional[int] = None
        # Guards the pending dicts: the evdev reader thread writes them while
        # the main thread drains them (dicts must never be iterated during a
        # concurrent write — the drain swaps them out under this lock).
        self._input_lock = threading.Lock()

        # Bonding throttle
        self._bond_in_progress = False
        self._last_bond_attempt = 0.0
        self._last_bond_device = None
        # True while the watchdog's btmgmt identity/posture verification runs
        # in a worker thread (btmgmt can hang on prerelease images — it must
        # never block the main loop, which would freeze the GUI).
        self._identity_check_busy = False

        # Serializes adapter posture work (btmgmt) against stop-time restore.
        self._adapter_lock = threading.Lock()
        # Serializes USB keyboard key sends (usb_gadget.send_key sleeps).
        self._usb_key_lock = threading.Lock()

    # ------------------------------------------------------------------ run

    def run(self) -> int:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            handlers=[
                logging.StreamHandler(),
                # SteamOS journals are volatile and can look "empty"; always
                # mirror to a plain file too.
                logging.FileHandler("/tmp/bt-controller-emulator.log"),
            ],
        )
        logger.info("bt-controller-emulator daemon starting (pid %d)", os.getpid())
        if os.geteuid() != 0:
            logger.warning(
                "Daemon is NOT running as root (euid=%d) — btmgmt, the HID "
                "L2CAP PSMs and /dev/hidg* will fail. Start it via systemd: "
                "sudo systemctl start bt-controller-emulator",
                os.geteuid(),
            )

        self.bus = bluez.get_system_bus()
        if not self._acquire_bus_name():
            logger.error("D-Bus name %s is already owned — is another daemon running?", BUS_NAME)
            return 1

        self._register_manager_object()
        self._subscribe_bluez_owner()
        self._watchdog_source = GLib.timeout_add(WATCHDOG_INTERVAL_MS, self._health_check)
        self._battery_source = GLib.timeout_add(BATTERY_INTERVAL_MS, self._battery_tick)

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._main_loop = GLib.MainLoop()
        try:
            self._main_loop.run()
        except KeyboardInterrupt:
            pass
        logger.info("daemon stopped")
        return 0

    def _signal_handler(self, sig, frame) -> None:
        logger.info("Received signal %s — shutting down", sig)
        self._exit_after_stop = True
        GLib.idle_add(self._request_shutdown)

    def _request_shutdown(self) -> bool:
        if self._session is not None:
            self._handle_stop()
        else:
            self._quit()
        return False

    def _quit(self) -> None:
        if self._watchdog_source is not None:
            GLib.source_remove(self._watchdog_source)
            self._watchdog_source = None
        if self._battery_source is not None:
            GLib.source_remove(self._battery_source)
            self._battery_source = None
        if self._main_loop and self._main_loop.is_running():
            self._main_loop.quit()

    # ------------------------------------------------------- D-Bus plumbing

    def _acquire_bus_name(self) -> bool:
        try:
            result = self.bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "RequestName",
                GLib.Variant("(su)", (BUS_NAME, 0x1 | 0x4)),  # ALLOW_REPLACEMENT | DO_NOT_QUEUE
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            code = result.get_child_value(0).get_uint32()
            if code in (1, 2):
                logger.info("D-Bus name acquired: %s", BUS_NAME)
                return True
            logger.error("RequestName(%s) returned %d", BUS_NAME, code)
            return False
        except GLib.Error as e:
            logger.error("Could not acquire D-Bus name: %s", e)
            return False

    def _register_manager_object(self) -> None:
        node_info = Gio.DBusNodeInfo.new_for_xml(MANAGER_XML)

        def handler(conn, sender, path, iface, method, params, invoc):
            try:
                self._handle_method(iface, method, params, invoc)
            except Exception as e:
                logger.exception("Method %s failed", method)
                invoc.return_dbus_error(ERROR_FAILED, str(e))

        reg_id = self.bus.register_object(
            OBJECT_PATH,
            node_info.interfaces[0],
            handler,
            self._get_property,
            None,
        )
        self._registrations.append(reg_id)
        reg_id = self.bus.register_object(
            OBJECT_PATH,
            node_info.interfaces[1],
            handler,
            None,
            None,
        )
        self._registrations.append(reg_id)
        logger.info("Manager object exported at %s", OBJECT_PATH)

    def _get_property(self, connection, sender, path, interface_name, property_name):
        if interface_name == MANAGER_IFACE and property_name == "Status":
            return GLib.Variant("s", self._status)
        return None

    def _subscribe_bluez_owner(self) -> None:
        """Track bluetoothd restarts: degrade + auto-recover active sessions."""
        self._bluez_watch_id = self.bus.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            bluez.BLUEZ_SERVICE,
            Gio.DBusSignalFlags.NONE,
            self._on_name_owner_changed,
            None,
        )

    def _on_name_owner_changed(self, conn, sender, path, iface, signal, params, *user_data) -> None:
        """NameOwnerChanged handler. PyGObject appends the user_data argument
        passed to signal_subscribe (even when None) — accept and ignore it."""
        try:
            name, _old, new = params.unpack()
        except Exception:
            return
        if name != "org.bluez":
            return
        if not new:
            logger.warning("bluetoothd left the bus")
            # Only degrade established sessions. If bluetoothd dies while a
            # session is still STARTING, the in-flight registration calls
            # will fail and _fail_session reports the error; racing a
            # recovery against a half-built session would corrupt it.
            if (
                self._session is not None
                and self._session.get("mode") == MODE_BLUETOOTH
                and self._status == STATUS_ACTIVE
                and not self._session.get("cancelled")
            ):
                logger.warning("degrading active session")
                self._set_status(STATUS_DEGRADED, "Bluetooth daemon restarted — recovering…")
                self._session["registered"] = False
        else:
            logger.info("bluetoothd is back on the bus")
            if self._status == STATUS_DEGRADED and self._session is not None:
                GLib.idle_add(self._recover_after_bluez_return, self._session.get("generation"))

    # ----------------------------------------------------- D-Bus method impl

    def _handle_method(self, iface: str, method: str, params: GLib.Variant, invoc) -> None:
        if iface not in (MANAGER_IFACE, "org.freedesktop.DBus.Properties"):
            invoc.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownInterface",
                f"Unknown interface: {iface}",
            )
            return

        if iface == "org.freedesktop.DBus.Properties":
            if method == "Get":
                _iface_name, prop_name = params.unpack()
                if prop_name == "Status":
                    invoc.return_value(GLib.Variant("(v)", (GLib.Variant("s", self._status),)))
                    return
                invoc.return_dbus_error(
                    "org.freedesktop.DBus.Error.InvalidArgs",
                    f"Unknown property: {prop_name}",
                )
            elif method == "GetAll":
                invoc.return_value(GLib.Variant("(a{sv})", ({"Status": GLib.Variant("s", self._status)},)))
            else:
                invoc.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod",
                    f"Unknown method: {method}",
                )
            return

        if method == "Start":
            mode, profile = params.unpack()
            self._handle_start(mode, profile)
            invoc.return_value(None)
        elif method == "Stop":
            self._handle_stop()
            invoc.return_value(None)
        elif method == "GetStatus":
            invoc.return_value(GLib.Variant("(a{sv})", (self._status_variant_dict(),)))
        elif method == "SendKey":
            code, modifiers, hold_ms = params.unpack()
            self._send_key(code, modifiers, hold_ms)
            invoc.return_value(None)
        elif method == "SendMouse":
            dx, dy, buttons, wheel = params.unpack()
            self._send_mouse(dx, dy, buttons, wheel)
            invoc.return_value(None)
        elif method == "SetButton":
            index, pressed = params.unpack()
            self._set_button(index, pressed)
            invoc.return_value(None)
        elif method == "SetAxis":
            index, value = params.unpack()
            self._set_axis(index, value)
            invoc.return_value(None)
        elif method == "SetTrigger":
            index, value = params.unpack()
            self._set_trigger(index, value)
            invoc.return_value(None)
        elif method == "SetHat":
            direction = params.unpack()[0]
            self._set_hat(direction)
            invoc.return_value(None)
        else:
            invoc.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod",
                f"Unknown method: {method}",
            )

    # ----------------------------------------------------------- input API

    def _engine(self) -> Optional[Any]:
        """The active HID engine (GattApplication / USBGadgetHID / Pro)."""
        session = self._session
        if session is None:
            return None
        return session.get("gatt") or session.get("usb") or session.get("pro")

    def _kb_capable(self) -> bool:
        session = self._session
        if session is None:
            return False
        if session.get("mode") == MODE_USB:
            return True
        return session.get("profile") == PROFILE_KEYBOARD

    def _ready(self) -> bool:
        session = self._session
        if session is None or self._status != STATUS_ACTIVE:
            return False
        engine = self._engine()
        if engine is None:
            return False
        try:
            return bool(engine.notifying)
        except AttributeError:
            return True

    def _send_key(self, code: int, modifiers: int, hold_ms: int) -> None:
        session = self._session
        if session is None or not self._kb_capable():
            logger.debug("SendKey dropped: keyboard not available in this mode")
            return
        if session.get("mode") == MODE_USB:
            usb = session.get("usb")
            if usb is not None:
                # USB send_key sleeps for hold_ms; never block the mainloop.
                # The per-daemon lock serializes sends so overlapping key
                # presses cannot clobber each other's press/release state.
                threading.Thread(
                    target=self._usb_send_key_locked,
                    args=(usb, code, modifiers, hold_ms),
                    daemon=True,
                ).start()
            return
        gatt = session.get("gatt")
        if gatt is not None:
            gatt.send_key(code, modifiers, hold_ms)

    def _usb_send_key_locked(self, usb, code: int, modifiers: int, hold_ms: int) -> None:
        with self._usb_key_lock:
            session = self._session
            if session is None or session.get("usb") is not usb:
                return
            usb.send_key(code, modifiers, hold_ms)

    def _send_mouse(self, dx: int, dy: int, buttons: int, wheel: int) -> None:
        session = self._session
        if session is None or not self._kb_capable():
            logger.debug("SendMouse dropped: mouse not available in this mode")
            return
        if session.get("mode") == MODE_USB:
            usb = session.get("usb")
            if usb is not None:
                usb.send_mouse_movement(dx, dy, buttons, wheel)
            return
        gatt = session.get("gatt")
        if gatt is not None:
            gatt.send_mouse_movement(dx, dy, buttons, wheel)

    def _set_button(self, index: int, pressed: bool) -> None:
        engine = self._engine()
        if engine is not None:
            engine.set_button(index, pressed)

    def _set_axis(self, index: int, value: int) -> None:
        engine = self._engine()
        if engine is not None:
            engine.set_axis(index, value)

    def _set_trigger(self, index: int, value: int) -> None:
        engine = self._engine()
        if engine is not None:
            engine.set_trigger(index, value)

    def _set_hat(self, direction: int) -> None:
        engine = self._engine()
        if engine is not None:
            engine.set_hat(direction)

    # ------------------------------------------------------------ lifecycle

    def _handle_start(self, mode: str, profile: str) -> None:
        if mode not in (MODE_BLUETOOTH, MODE_USB):
            raise RuntimeError(f"invalid mode {mode!r} (use 'bluetooth' or 'usb')")
        if profile not in (PROFILE_GAMEPAD, PROFILE_KEYBOARD, PROFILE_XBOX):
            raise RuntimeError(f"invalid profile {profile!r} (use 'gamepad', 'xbox' or 'keyboard')")
        if profile == PROFILE_XBOX and mode != MODE_BLUETOOTH:
            raise RuntimeError("the Xbox profile is Bluetooth-only — use 'gamepad' (DUALSHOCK 4) for USB")
        if self._session is not None:
            if self._status == STATUS_ERROR:
                raise RuntimeError("previous session is still shutting down — try again in a moment")
            raise RuntimeError("a session is already active — call Stop first")
        if self._status in (STATUS_STARTING, STATUS_STOPPING, STATUS_ACTIVE, STATUS_DEGRADED):
            raise RuntimeError("a session is already active — call Stop first")

        self._mode = mode
        self._profile = profile
        self._error = ""
        self._adapter_tries_left = ADAPTER_POLL_TRIES
        self._generation += 1
        gen = self._generation
        self._session = {
            "generation": gen,
            "mode": mode,
            "profile": profile,
            "adapter_path": None,
            "adapter_snapshot": None,
            "agent": None,
            "gatt": None,
            "adv": None,
            "pro": None,
            "usb": None,
            "input": None,
            "registered": False,
            "recoveries": 0,
            "resume": False,
        }
        self._reset_connection_state()
        self._set_status(STATUS_STARTING, "Starting…")

        if mode == MODE_USB:
            self._spawn_worker(self._usb_start_worker, gen)
            return

        if not bluez.bluez_has_owner(self.bus):
            self._set_detail("Waiting for Bluetooth service (bluetoothd)…")
            self._bluez_wait_left = BLUEZ_WAIT_TRIES
            self._bluez_wait_source = GLib.timeout_add(1000, self._poll_bluez, gen)
            return
        self._start_bt_session(gen)

    def _poll_bluez(self, gen: int) -> bool:
        """Wait up to BLUEZ_WAIT_TRIES seconds for bluetoothd during Start."""
        self._bluez_wait_source = None
        if self._session is None or self._session.get("generation") != gen:
            return False
        if bluez.bluez_has_owner(self.bus):
            self._start_bt_session(gen)
            return False
        self._bluez_wait_left -= 1
        if self._bluez_wait_left <= 0:
            self._fail_session(
                gen,
                "Bluetooth service (bluetoothd) is not running — "
                "check 'systemctl status bluetooth'",
            )
            return False
        self._bluez_wait_source = GLib.timeout_add(1000, self._poll_bluez, gen)
        return False

    def _start_bt_session(self, gen: int) -> None:
        """Main-thread start sequence (resume after bluetoothd return skips
        the snapshot so the original adapter posture is restored on stop)."""
        session = self._session
        if session is None or session.get("generation") != gen:
            return

        adapter_path = bluez.find_adapter_path(self.bus, ADAPTER_NAME)
        if adapter_path is None:
            self._adapter_tries_left -= 1
            if self._adapter_tries_left <= 0:
                self._fail_session(
                    gen,
                    f"No Bluetooth adapter ({ADAPTER_NAME}) found — check "
                    "'systemctl status bluetooth' and the Bluetooth settings",
                )
                return
            self._adapter_poll_source = GLib.timeout_add(1000, self._poll_adapter, gen)
            return
        self._adapter_tries_left = ADAPTER_POLL_TRIES
        session["adapter_path"] = adapter_path
        logger.info("Using adapter: %s", adapter_path)

        if session.get("resume"):
            # Re-apply posture + re-register after a bluetoothd restart.
            self._spawn_worker(self._adapter_resume_worker, gen)
        else:
            self._spawn_worker(self._adapter_worker, gen)

    def _poll_adapter(self, gen: int) -> bool:
        self._adapter_poll_source = None
        self._start_bt_session(gen)
        return False

    def _adapter_worker(self, gen: int) -> None:
        """Worker: snapshot adapter + apply per-mode posture (btmgmt).
        Runs under _adapter_lock so stop-time restore can never interleave."""
        with self._adapter_lock:
            session = self._session
            if session is None or session.get("generation") != gen or session.get("cancelled"):
                return
            bus = self.bus
            adapter_path = session["adapter_path"]
            idx = bluez.get_adapter_index(ADAPTER_NAME)

            session["adapter_snapshot"] = bluez.snapshot_adapter_settings(bus, adapter_path, idx)

            if session.get("profile") == PROFILE_GAMEPAD:
                # Anything that may restart bluetoothd (Device ID config,
                # PSM bind last resort) runs BEFORE the posture/agent/SDP
                # registration — a restart wipes all of those.
                if not self._prepare_pro_worker(gen):
                    return
                bluez.configure_adapter_for_pro_controller(idx)
                bluez.set_adapter_alias(bus, adapter_path, PRO_NAME)
                # BR/EDR discoverable so hosts can find "Pro Controller".
                bluez.set_adapter_discoverable(bus, adapter_path, True)
            else:
                if session.get("profile") == PROFILE_XBOX and not self._apply_device_id_worker(
                    gen, XBOX_DEVICE_ID, "Xbox Wireless Controller",
                ):
                    return
                bluez.set_static_ble_address(idx, STATIC_BLE_ADDRESS)
                bluez.configure_adapter_for_hog_peripheral(idx)
                bluez.set_adapter_alias(bus, adapter_path, self._hogp_name(session))
                # LE mode: the advertisement handles visibility.
                bluez.set_adapter_discoverable(bus, adapter_path, False)
            bluez.ensure_adapter_powered(bus, adapter_path)
            GLib.idle_add(self._after_adapter_ready, gen)

    def _adapter_resume_worker(self, gen: int) -> None:
        """Worker: re-apply posture after bluetoothd returned (no snapshot)."""
        with self._adapter_lock:
            session = self._session
            if session is None or session.get("generation") != gen or session.get("cancelled"):
                return
            bus = self.bus
            adapter_path = session["adapter_path"]
            idx = bluez.get_adapter_index(ADAPTER_NAME)
            if session.get("profile") == PROFILE_GAMEPAD:
                if not self._prepare_pro_worker(gen):
                    return
                bluez.configure_adapter_for_pro_controller(idx)
                bluez.set_adapter_alias(bus, adapter_path, PRO_NAME)
                bluez.set_adapter_discoverable(bus, adapter_path, True)
            else:
                if session.get("profile") == PROFILE_XBOX and not self._apply_device_id_worker(
                    gen, XBOX_DEVICE_ID, "Xbox Wireless Controller",
                ):
                    return
                bluez.configure_adapter_for_hog_peripheral(idx)
                bluez.set_adapter_alias(bus, session["adapter_path"], self._hogp_name(session))
                bluez.set_adapter_discoverable(bus, session["adapter_path"], False)
            bluez.ensure_adapter_powered(bus, adapter_path)
            GLib.idle_add(self._after_adapter_ready, gen)

    def _wait_for_adapter(self, timeout: float = 15.0) -> Optional[str]:
        """Worker-thread wait for bluetoothd + the adapter to come back
        after a restart. Returns the adapter path or None."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if bluez.bluez_has_owner(self.bus):
                path = bluez.find_adapter_path(self.bus, ADAPTER_NAME)
                if path:
                    time.sleep(1.0)  # let bluetoothd finish adapter init
                    return path
            time.sleep(0.5)
        return None

    def _apply_device_id_worker(self, gen: int, did: str, label: str) -> bool:
        """Worker (under _adapter_lock): make bluetoothd publish `did`.

        bluetoothd advertises the main.conf DeviceID in THREE places the host
        can read: the classic SDP Device ID record, the EIR Device ID, and
        its own built-in GATT Device Information service (PnP ID). macOS
        prefers those over our GATT PnP ID, so every profile must set its
        own VID/PID here — a leftover Nintendo ID made the Xbox profile load
        macOS's Switch Pro driver. Restarts bluetoothd only when it changes.
        Returns False (and fails the session) if the adapter doesn't return."""
        session = self._session
        changed = bluez.ensure_device_id_config(did)
        if changed is None:
            logger.error(
                "Could not write DeviceID=%s to %s — the host may not recognise "
                "the %s (pairs but no input)", did, bluez.BLUEZ_MAIN_CONF, label,
            )
            return True
        if not changed:
            logger.info("bluetoothd Device ID already %s", did)
            return True
        logger.warning(
            "Restarting bluetooth.service to publish Device ID %s (%s). If the "
            "Mac paired with this Deck before, forget the device there and pair "
            "again — macOS caches the old vendor/product ID.", did, label,
        )
        subprocess.run(["systemctl", "restart", "bluetooth"], capture_output=True, timeout=30)
        path = self._wait_for_adapter()
        if path is None:
            GLib.idle_add(lambda: (self._fail_session(
                gen, f"Bluetooth adapter did not return after restarting "
                     f"bluetoothd to apply the {label} Device ID"), False)[1])
            return False
        session["adapter_path"] = path
        return True

    def _prepare_pro_worker(self, gen: int) -> bool:
        """Worker (under _adapter_lock): Device ID config + L2CAP PSM bind.

        Both can restart bluetooth.service, so they must run before the
        adapter posture, agent and SDP record are applied. On failure the
        session is failed (on the main loop) and False is returned."""
        session = self._session

        def fail(message: str) -> bool:
            GLib.idle_add(lambda: (self._fail_session(gen, message), False)[1])
            return False

        # 1) VID/PID: macOS's GameController framework only adopts a Pro
        #    Controller whose SDP Device ID record says 057E:2009.
        if not self._apply_device_id_worker(gen, PRO_DEVICE_ID, "Pro Controller"):
            return False

        # 2) HID L2CAP PSMs 0x11/0x13. Binding may itself restart
        #    bluetoothd as a last resort (see pro_controller.py).
        if session.get("cancelled") or session.get("generation") != gen:
            return False
        adapter_address = (
            bluez.get_adapter_address(self.bus, session["adapter_path"])
            or "00:00:00:00:00:00"
        )
        pro = ProControllerEmulator(adapter_address=adapter_address)
        pro.on_state_change = lambda connected: GLib.idle_add(self._on_pro_state, gen, connected)
        if not pro.start():
            return fail(
                "Could not bind HID L2CAP sockets: "
                f"{pro.last_error or 'PSM 0x11/0x13 busy'}"
            )
        if session.get("cancelled") or session.get("generation") != gen:
            pro.stop()
            return False
        session["pro"] = pro
        if pro.restarted_bluetoothd:
            path = self._wait_for_adapter()
            if path is None:
                return fail("Bluetooth adapter did not return after restarting "
                            "bluetoothd to free PSM 0x11/0x13")
            session["adapter_path"] = path
        return True

    def _after_adapter_ready(self, gen: int) -> bool:
        session = self._session
        if session is None or session.get("generation") != gen:
            return False
        if session.get("profile") == PROFILE_GAMEPAD:
            self._start_pro_session(gen)
        else:
            self._start_hogp_session(gen)
        return False

    # ------------------------------------------------ HOGP (keyboard) start

    def _start_hogp_session(self, gen: int) -> None:
        session = self._session
        if session is None or session.get("generation") != gen:
            return

        agent = PairingAgent(self.bus, verbose=True)
        if not agent.register():
            self._fail_session(
                gen,
                "Could not register Bluetooth pairing agent: "
                f"{agent.last_error or 'unknown error'} — re-run install.sh "
                "(D-Bus policy) or run 'sudo systemctl restart bluetooth'",
            )
            return
        session["agent"] = agent

        if session.get("profile") == PROFILE_XBOX:
            gatt = XboxGattApplication(self.bus)
            gatt.on_rumble = self._on_host_rumble
        else:
            gatt = GattApplication(self.bus, device_name=HOGP_DEVICE_NAME)
        gatt.set_report_rate(REPORT_RATE_HZ)
        if not gatt.register():
            self._fail_session(gen, "Failed to register GATT service")
            return
        gatt._on_notify_start = lambda: self._on_hogp_notify_start(gen)
        session["gatt"] = gatt

        adv = Advertisement(self.bus, self._hogp_name(session))
        if not adv.register():
            self._fail_session(gen, "Failed to register advertisement")
            return
        session["adv"] = adv

        session["reg_attempts"] = 0
        self._register_gatt_app(gen)

    def _register_gatt_app(self, gen: int) -> None:
        session = self._session
        if session is None or session.get("generation") != gen:
            return
        attempts = session.get("reg_attempts", 0)
        if attempts >= MAX_REGISTRATION_RETRIES:
            self._fail_session(
                gen,
                f"GATT registration failed after {attempts} attempts: "
                f"{session.get('reg_last_error', 'unknown BlueZ error')}",
            )
            return
        session["reg_attempts"] = attempts + 1

        def on_done(success, error):
            if self._session is None or self._session.get("generation") != gen:
                return
            if success:
                self._register_advertisement(gen, 0)
                return
            session["reg_last_error"] = error or "unknown BlueZ error"
            if "AlreadyExists" in error or "InProgress" in error:
                logger.warning("GATT registration collision (%s) — clearing and retrying", error)
                bluez.unregister_application_async(
                    self.bus, session["adapter_path"], GattApplication.APP_PATH,
                    lambda s, e: GLib.timeout_add(500, lambda: self._register_gatt_app(gen)),
                )
                return
            self._fail_session(gen, f"GATT registration failed: {error}")

        bluez.register_application_async(
            self.bus, session["adapter_path"], GattApplication.APP_PATH, on_done,
        )

    def _register_advertisement(self, gen: int, attempts: int, restart_input: bool = True) -> None:
        session = self._session
        if session is None or session.get("generation") != gen:
            return
        if attempts >= MAX_REGISTRATION_RETRIES:
            self._fail_session(
                gen,
                f"Advertisement registration failed after {attempts} attempts: "
                f"{session.get('adv_last_error', 'unknown BlueZ error')}",
            )
            return

        def on_done(success, error):
            if self._session is None or self._session.get("generation") != gen:
                return
            if success:
                if restart_input:
                    self._session_registered(gen)
                else:
                    # Health-recovery re-registration: the session (and the
                    # evdev input handler) is already up — just go active.
                    session["registered"] = True
                    session["recoveries"] = 0
                    self._set_status(STATUS_ACTIVE, "Active - Discoverable")
                    self._conn = "Waiting for connection..."
                    self._emit_status()
                return
            session["adv_last_error"] = error or "unknown BlueZ error"
            if "AlreadyExists" in error or "InProgress" in error:
                logger.warning("Advertisement collision (%s) — clearing and retrying", error)
                bluez.unregister_advertisement_async(
                    self.bus, session["adapter_path"], Advertisement.ADV_PATH,
                    lambda s, e: GLib.timeout_add(
                        500, lambda: self._register_advertisement(gen, attempts + 1, restart_input)
                    ),
                )
                return
            self._fail_session(gen, f"Advertisement failed: {error}")

        bluez.register_advertisement_async(
            self.bus, session["adapter_path"], Advertisement.ADV_PATH, on_done,
        )

    def _session_registered(self, gen: int) -> None:
        session = self._session
        if session is None or session.get("generation") != gen:
            return
        session["registered"] = True
        session["recoveries"] = 0
        active = bluez.get_le_advertising_active_instances(self.bus, session["adapter_path"])
        logger.info("Registration complete! ActiveInstances: %s", active)
        self._start_input(gen)
        self._set_status(STATUS_ACTIVE, "Active - Discoverable")
        self._conn = "Waiting for connection..."
        self._emit_status()

    @staticmethod
    def _hogp_name(session: Dict[str, Any]) -> str:
        return XBOX_NAME if session.get("profile") == PROFILE_XBOX else HOGP_DEVICE_NAME

    def _on_host_rumble(self, strong: float, weak: float, duration_ms: int) -> None:
        """Host rumble (Xbox output report) -> Deck haptics via evdev FF.
        Called on the main loop (GATT WriteValue); the FF ioctl is quick."""
        session = self._session
        ih = session.get("input") if session else None
        if ih is not None:
            ih.rumble(strong, weak, duration_ms)

    def _on_hogp_notify_start(self, gen: int) -> None:
        """macOS subscribed to HID notifications — complete the bond."""
        if self._session is None or self._session.get("generation") != gen:
            return
        self._maybe_complete_bond(gen)

    # ------------------------------------------------- Pro Controller start

    def _start_pro_session(self, gen: int) -> None:
        session = self._session
        if session is None or session.get("generation") != gen:
            return

        agent = PairingAgent(self.bus, verbose=True)
        if not agent.register():
            self._fail_session(
                gen,
                "Could not register Bluetooth pairing agent: "
                f"{agent.last_error or 'unknown error'} — re-run install.sh "
                "(D-Bus policy) or run 'sudo systemctl restart bluetooth'",
            )
            return
        session["agent"] = agent

        if not bluez.register_hid_sdp_profile(self.bus, SDP_RECORD_XML):
            self._fail_session(
                gen,
                "Could not register HID SDP record: "
                f"{bluez.get_last_sdp_error() or 'unknown BlueZ error'}",
            )
            return
        session["sdp_registered"] = True

        # Listeners were bound by the adapter worker (_prepare_pro_worker).
        pro = session.get("pro")
        if pro is None or not pro.running:
            self._fail_session(gen, "Pro Controller listeners are not running")
            return
        session["registered"] = True
        session["recoveries"] = 0

        self._start_input(gen)
        self._set_status(STATUS_ACTIVE, "Active - Pro Controller")
        self._conn = "Waiting for Mac to connect…"
        self._emit_status()

    def _on_pro_state(self, gen: int, connected: bool) -> bool:
        if self._session is None or self._session.get("generation") != gen:
            return False
        if connected:
            self._conn = "✓ Paired — streaming as Pro Controller"
        else:
            self._conn = "Waiting for Mac to connect…"
        self._connected = connected
        self._emit_status()
        return False

    # ------------------------------------------------------- USB gadget start

    def _usb_start_worker(self, gen: int) -> None:
        session = self._session
        if session is None or session.get("generation") != gen:
            return

        def session_valid() -> bool:
            return (
                self._session is not None
                and self._session.get("generation") == gen
                and not self._session.get("cancelled")
            )

        try:
            profile_tag = "ds4" if session.get("profile") == PROFILE_GAMEPAD else "generic"
            need_setup = False
            for dev in ("/dev/hidg0", "/dev/hidg1", "/dev/hidg2"):
                if not os.path.exists(dev):
                    need_setup = True
                    break
            if not need_setup:
                try:
                    with open("/tmp/steamdeck_hid.profile") as f:
                        if f.read().strip() != profile_tag:
                            need_setup = True
                except OSError:
                    need_setup = True

            if need_setup:
                GLib.idle_add(self._set_detail, "Setting up USB gadget…")
                result = _run_script(
                    "setup-usb-gadget.sh", "--profile", profile_tag,
                    timeout=USB_SETUP_TIMEOUT_S,
                )
                if not session_valid():
                    logger.info("USB start aborted (session stopped during gadget setup)")
                    return
                if result.returncode != 0:
                    tail = (result.stderr or result.stdout or "").strip()[-400:]
                    self._fail_session(
                        gen,
                        "USB gadget setup failed. Enable BIOS USB DRD "
                        "(Advanced → USB Configuration → USB Dual Role Device), "
                        "connect a data cable, then retry. "
                        f"Details: {tail or 'no output'}",
                    )
                    return

            usb = USBGadgetHID(gamepad_profile=profile_tag)
            if not usb.open():
                self._fail_session(
                    gen,
                    "No /dev/hidg* devices. Run scripts/diagnose.sh in Konsole "
                    "(BIOS DRD + data USB cable).",
                )
                return
            if not session_valid():
                usb.close()
                logger.info("USB start aborted (session stopped while opening gadget)")
                return
            if usb._gamepad_fd is None:
                usb.close()
                self._fail_session(
                    gen,
                    "Gamepad HID function (/dev/hidg0) is missing. Run "
                    "scripts/cleanup-usb-gadget.sh in Konsole, then start "
                    "again so the gadget is rebuilt.",
                )
                return
            session["usb"] = usb
            session["registered"] = True

            if not session_valid():
                usb.close()
                session["usb"] = None
                return
            self._start_input(gen)
            self._set_status(STATUS_ACTIVE, "Active - Wired USB")
            self._conn = "Connected via USB (cable)"
            self._emit_status()
        except subprocess.TimeoutExpired:
            if session_valid():
                self._fail_session(gen, "USB gadget setup timed out")
        except Exception as e:
            logger.exception("USB start failed")
            if session_valid():
                self._fail_session(gen, f"Failed to run USB setup: {e}")

    # ------------------------------------------------------------- input path

    def _start_input(self, gen: int) -> None:
        """Start evdev forwarding (worker thread; callbacks marshal via GLib)."""
        session = self._session
        if session is None or session.get("generation") != gen:
            return

        def run():
            ih = InputHandler(
                device_path=None,
                on_button_change=self._on_button_cb,
                on_axis_change=self._on_axis_cb,
                on_trigger_change=self._on_trigger_cb,
                on_hat_change=self._on_hat_cb,
                verbose=False,
            )
            ok = ih.start()

            def done():
                if self._session is None or self._session.get("generation") != gen:
                    ih.stop()
                    return False
                if ok:
                    self._session["input"] = ih
                    self._input_label = ih.active_device_label
                    self._conn = "Input device connected"
                else:
                    self._input_label = ""
                    self._conn = "No gamepad evdev node — service up, input may be missing"
                self._emit_status()
                return False

            GLib.idle_add(done)

        self._spawn_worker(run)

    def _on_button_cb(self, index: int, pressed: bool) -> None:
        self._pending_buttons[index] = pressed
        GLib.idle_add(self._apply_pending_button, index)

    def _apply_pending_button(self, index: int) -> bool:
        if self._session is None:
            return False
        pressed = self._pending_buttons.pop(index, None)
        if pressed is not None:
            self._set_button(index, pressed)
        return False

    def _on_axis_cb(self, index: int, value: int) -> None:
        with self._input_lock:
            self._pending_axes[index] = value
        # GLib.timeout_add is only safe on the main thread — marshal there.
        GLib.idle_add(self._schedule_coalesce)

    def _on_trigger_cb(self, index: int, value: int) -> None:
        with self._input_lock:
            self._pending_triggers[index] = value
        GLib.idle_add(self._schedule_coalesce)

    def _on_hat_cb(self, direction: int) -> None:
        self._pending_hat = direction
        GLib.idle_add(self._apply_pending_hat)

    def _apply_pending_hat(self) -> bool:
        if self._session is None:
            return False
        direction = self._pending_hat
        self._pending_hat = None
        if direction is not None:
            self._set_hat(direction)
        return False

    def _schedule_coalesce(self) -> None:
        if self._coalesce_source is not None:
            return
        self._coalesce_source = GLib.timeout_add(INPUT_COALESCE_MS, self._flush_coalesced_input)

    def _flush_coalesced_input(self) -> bool:
        self._coalesce_source = None
        # Swap the pending dicts out under the lock instead of clearing in
        # place: the evdev thread may write new values at any moment, and
        # iterating a dict that a writer mutates raises RuntimeError. New
        # writes land in the fresh dicts and are picked up by the next flush
        # (the queued _schedule_coalesce callback re-arms the timer).
        with self._input_lock:
            axes = self._pending_axes
            self._pending_axes = {}
            triggers = self._pending_triggers
            self._pending_triggers = {}
        if self._session is not None:
            for index, value in axes.items():
                self._set_axis(index, value)
            for index, value in triggers.items():
                self._set_trigger(index, value)
        return False

    # ---------------------------------------------------------- stop/fail

    def _handle_stop(self) -> None:
        if self._status == STATUS_STOPPING:
            return  # teardown already in flight; a second Stop is a no-op
        session = self._session
        if session is None:
            if self._status != STATUS_STOPPED:
                self._set_status(STATUS_STOPPED, "Stopped")
            if self._exit_after_stop:
                self._quit()
            return

        gen = session.get("generation")
        session["cancelled"] = True
        self._cancel_start_waiters()
        self._set_status(STATUS_STOPPING, "Stopping…")

        # Main-thread teardown of objects whose D-Bus unregistration can
        # trigger callbacks into us (agent) or touch the mainloop (gatt).
        agent = session.get("agent")
        if agent is not None:
            try:
                agent.unregister()
            except Exception as e:
                logger.debug("agent unregister: %s", e)
        gatt = session.get("gatt")
        if gatt is not None:
            try:
                gatt.stop_notify()
            except Exception as e:
                logger.debug("gatt stop_notify: %s", e)
        pro = session.get("pro")
        if pro is not None:
            pro.stop()
        session["registered"] = False

        self._spawn_worker(self._stop_worker, gen)

    def _stop_worker(self, gen: int) -> None:
        """Worker: blocking teardown (D-Bus sync calls are thread-safe and
        safe to block here because BlueZ does not call back into us for
        unregister operations)."""
        session = self._session
        if session is None:
            GLib.idle_add(self._stop_finalize, gen)
            return
        if session.get("generation") != gen:
            return

        input_handler = session.get("input")
        if input_handler is not None:
            try:
                input_handler.stop()
            except Exception as e:
                logger.debug("input stop: %s", e)

        # Release the HID L2CAP PSMs. _handle_stop stops the Pro engine on
        # the main thread, but _fail_session skips that path — stopping here
        # too (idempotent) guarantees a failed session can never leave the
        # ports bound and make the next Start fail with "PSM 0x11/0x13 busy".
        pro = session.get("pro")
        if pro is not None:
            try:
                pro.stop()
            except Exception as e:
                logger.debug("pro stop: %s", e)

        if session.get("mode") == MODE_BLUETOOTH:
            adapter_path = session.get("adapter_path")
            if adapter_path:
                bluez.unregister_advertisement_sync(self.bus, adapter_path, Advertisement.ADV_PATH)
                bluez.unregister_application_sync(self.bus, adapter_path, GattApplication.APP_PATH)
            if session.get("sdp_registered"):
                bluez.unregister_hid_sdp_profile(self.bus)
        else:
            usb = session.get("usb")
            if usb is not None:
                try:
                    usb.close()
                except Exception as e:
                    logger.debug("usb close: %s", e)

        # Restore the adapter to its pre-session posture. The lock waits out
        # any in-flight posture worker; its own cancelled/gen re-check under
        # the same lock guarantees nothing re-applies the emulator posture
        # afterwards.
        snapshot = session.get("adapter_snapshot")
        adapter_path = session.get("adapter_path")
        if session.get("mode") == MODE_BLUETOOTH and adapter_path:
            with self._adapter_lock:
                if snapshot is not None:
                    bluez.restore_adapter_settings(
                        self.bus, adapter_path, bluez.get_adapter_index(ADAPTER_NAME), snapshot,
                    )
                else:
                    bluez.ensure_adapter_powered(self.bus, adapter_path)

        GLib.idle_add(self._stop_finalize, gen)

    def _stop_finalize(self, gen: int) -> bool:
        session = self._session
        keep_error = self._status == STATUS_ERROR
        if session is not None and session.get("generation") == gen:
            agent = session.get("agent")
            if agent is not None:
                try:
                    agent.unregister()
                except Exception as e:
                    logger.debug("agent unregister: %s", e)
            adv = session.get("adv")
            if adv is not None:
                try:
                    adv.unregister()
                except Exception as e:
                    logger.debug("adv unregister: %s", e)
            gatt = session.get("gatt")
            if gatt is not None:
                try:
                    gatt.unregister()
                except Exception as e:
                    logger.debug("gatt unregister: %s", e)
            self._session = None

        with self._input_lock:
            self._pending_buttons.clear()
            self._pending_axes.clear()
            self._pending_triggers.clear()
            self._pending_hat = None
        if self._coalesce_source is not None:
            GLib.source_remove(self._coalesce_source)
            self._coalesce_source = None

        if keep_error:
            self._emit_status()
        else:
            self._reset_connection_state()
            self._set_status(STATUS_STOPPED, "Stopped")
        if self._exit_after_stop:
            self._quit()
        return False

    def _fail_session(self, gen: int, message: str) -> None:
        """Record the error, tear the session down, keep the error visible."""
        session = self._session
        if session is None or session.get("generation") != gen:
            return
        logger.error("Session failed: %s", message)
        self._error = message
        session["cancelled"] = True
        self._cancel_start_waiters()
        self._set_status(STATUS_ERROR, message)
        session["registered"] = False
        self._spawn_worker(self._stop_worker, gen)

    def _cancel_start_waiters(self) -> None:
        if self._bluez_wait_source is not None:
            GLib.source_remove(self._bluez_wait_source)
            self._bluez_wait_source = None
        if self._adapter_poll_source is not None:
            GLib.source_remove(self._adapter_poll_source)
            self._adapter_poll_source = None

    def _reset_connection_state(self) -> None:
        self._connected = False
        self._paired = False
        self._trusted = False
        self._input_label = ""
        self._device_name = ""
        self._device_addr = ""
        self._bond_in_progress = False

    # -------------------------------------------------------- self-healing

    def _recover_after_bluez_return(self, gen: int) -> bool:
        session = self._session
        if session is None or session.get("generation") != gen:
            return False
        if self._status != STATUS_DEGRADED:
            return False
        if session.get("cancelled"):
            return False
        session["recoveries"] = session.get("recoveries", 0) + 1
        if session["recoveries"] > MAX_RECOVERIES:
            self._fail_session(
                gen,
                "Bluetooth daemon kept restarting — recovery failed. "
                "Run 'sudo systemctl restart bluetooth', then start again.",
            )
            return False
        logger.info("Recovering session after bluetoothd restart (%d/%d)",
                    session["recoveries"], MAX_RECOVERIES)
        self._reset_engines_for_recovery(gen)
        session["resume"] = True
        self._set_status(STATUS_STARTING, "Re-registering with BlueZ…")
        self._start_bt_session(gen)
        return False

    def _reset_engines_for_recovery(self, gen: int) -> None:
        """Drop the old BlueZ-facing objects (their registrations died with
        bluetoothd) so the resume path can re-create them on the same object
        paths. The evdev input handler keeps running throughout."""
        session = self._session
        if session is None or session.get("generation") != gen:
            return
        agent = session.get("agent")
        if agent is not None:
            try:
                agent.unregister()
            except Exception as e:
                logger.debug("agent unregister (recovery): %s", e)
        pro = session.get("pro")
        if pro is not None:
            pro.stop()
        adv = session.get("adv")
        if adv is not None:
            try:
                adv.unregister()
            except Exception as e:
                logger.debug("adv unregister (recovery): %s", e)
        gatt = session.get("gatt")
        if gatt is not None:
            try:
                gatt.unregister()
            except Exception as e:
                logger.debug("gatt unregister (recovery): %s", e)
        for key in ("agent", "gatt", "adv", "pro"):
            session[key] = None
        session["registered"] = False
        session["sdp_registered"] = False

    def _health_check(self) -> bool:
        session = self._session
        if session is None or self._status not in (STATUS_ACTIVE, STATUS_DEGRADED):
            return True

        gen = session.get("generation")
        mode = session.get("mode")

        if mode == MODE_USB:
            if not os.path.exists("/dev/hidg0"):
                self._fail_session(
                    gen,
                    "USB gadget disappeared — re-plug the USB-C cable, "
                    "then start again.",
                )
            return True

        # Bluetooth: keep device/bond status fresh + verify registrations.
        adapter_path = session.get("adapter_path")
        if adapter_path:
            device = bluez.get_primary_connected_device(self.bus, adapter_path)
            self._update_device_status(device)
            if device and not (device.get("paired") or device.get("bonded")):
                self._maybe_complete_bond(gen)

        if not bluez.bluez_has_owner(self.bus):
            return True  # degraded already handled by the owner watch

        if session.get("registered") and session.get("profile") in HOGP_PROFILES:
            active = bluez.get_le_advertising_active_instances(self.bus, adapter_path)
            if active == 0:
                logger.warning("Advertisement lost (ActiveInstances=0) — re-registering")
                session["registered"] = False
                session["recoveries"] = session.get("recoveries", 0) + 1
                if session["recoveries"] > MAX_RECOVERIES:
                    self._fail_session(
                        gen,
                        "Bluetooth registration was lost repeatedly — "
                        "run 'sudo systemctl restart bluetooth', then start again.",
                    )
                    return True
                self._register_advertisement(gen, 0, restart_input=False)

        if session.get("profile") == PROFILE_GAMEPAD:
            # Steam/SteamOS can re-assert the hostname identity (name "steamdeck"
            # + laptop class) and flip pairing settings. Keep the Pro Controller
            # identity and pairing posture pinned while the session runs — without
            # power-cycling, so an established HID connection is never dropped.
            # Run in a worker: btmgmt hangs for seconds on prerelease images and
            # must never block the main loop (that froze the GUI).
            if not self._identity_check_busy:
                self._identity_check_busy = True
                self._spawn_worker(self._identity_worker, gen)
            pro = session.get("pro")
            if pro is not None and not pro.running and not self._shutting_down:
                logger.warning("Pro Controller listener died — restarting session")
                self._fail_session(
                    gen, "Pro Controller listener died unexpectedly — start again",
                )
                return True

        self._emit_status()
        return True

    def _identity_worker(self, gen: int) -> None:
        """Watchdog worker: verify/re-assert the Pro Controller identity and
        pairing posture. Runs off the main loop because btmgmt can hang."""
        try:
            idx = bluez.get_adapter_index(ADAPTER_NAME)
            if self._session is not None and self._session.get("generation") == gen:
                bluez.ensure_pro_controller_identity(idx, allow_power_cycle=False)
                bluez.ensure_pro_controller_posture(idx)
        except Exception as e:
            logger.warning("identity worker: %s", e)
        finally:

            def done():
                self._identity_check_busy = False
                return False

            GLib.idle_add(done)

    def _update_device_status(self, device: Optional[Dict]) -> None:
        if device is None:
            self._connected = False
            self._device_name = ""
            self._device_addr = ""
            if self._status == STATUS_ACTIVE and self._profile == PROFILE_GAMEPAD:
                pass  # pro mode conn line is managed by on_state_change
            elif self._status == STATUS_ACTIVE and self._profile in HOGP_PROFILES:
                self._conn = "Waiting for connection..."
            return
        self._connected = True
        self._device_name = device.get("name", "")
        self._device_addr = device.get("address", "")
        self._paired = bool(device.get("paired") or device.get("bonded"))
        self._trusted = bool(device.get("trusted"))
        if self._profile in HOGP_PROFILES:
            notifying = False
            gatt = self._session.get("gatt") if self._session else None
            if gatt is not None:
                notifying = gatt.notifying
            if self._profile == PROFILE_XBOX and (self._paired or self._trusted):
                # A bonded host reconnects without re-subscribing (its CCC
                # persists); the Xbox engine streams regardless.
                notifying = True
            if notifying:
                if self._paired or self._trusted:
                    self._conn = "✓ Paired — sending HID data"
                elif self._bond_in_progress:
                    self._conn = "Finishing Bluetooth pairing…"
                else:
                    self._conn = "✓ Connected — completing pairing…"
            elif self._bond_in_progress:
                self._conn = "Finishing Bluetooth pairing…"
            else:
                self._conn = "✓ Connected — accept pairing on Mac…"

    def _maybe_complete_bond(self, gen: int) -> None:
        session = self._session
        if session is None or session.get("generation") != gen:
            return
        if self._bond_in_progress or self._status != STATUS_ACTIVE:
            return
        adapter_path = session.get("adapter_path")
        device = bluez.get_primary_connected_device(self.bus, adapter_path)
        if device is None:
            return
        now = time.monotonic()
        if (
            device["path"] == self._last_bond_device
            and now - self._last_bond_attempt < BOND_RETRY_S
        ):
            return
        self._bond_in_progress = True
        self._last_bond_device = device["path"]
        self._last_bond_attempt = now
        self._conn = "Finishing Bluetooth pairing…"
        self._emit_status()

        def run():
            try:
                updated, err = bluez.complete_bond_with_central(self.bus, adapter_path)
            except Exception as e:
                updated, err = None, str(e)

            def done():
                self._bond_in_progress = False
                if self._session is None or self._session.get("generation") != gen:
                    return False
                if err:
                    logger.warning("Bond attempt failed: %s", err)
                self._update_device_status(updated)
                self._emit_status()
                return False

            GLib.idle_add(done)

        self._spawn_worker(run)

    def _battery_tick(self) -> bool:
        self._battery = read_battery_percent()
        gatt = self._session.get("gatt") if self._session else None
        if gatt is not None and hasattr(gatt, "set_battery_level"):
            gatt.set_battery_level(self._battery)
        if self._session is not None:
            self._emit_status()
        return True

    # ----------------------------------------------------------- status I/O

    def _set_status(self, status: str, detail: str) -> None:
        self._status = status
        self._detail = detail
        self._emit_status()

    def _set_detail(self, detail: str) -> bool:
        self._detail = detail
        self._emit_status()
        return False

    def _visualizer_state(self) -> tuple:
        """(buttons, axes, triggers, hat) from the active engine."""
        engine = self._engine()
        if engine is None:
            return 0, [0, 0, 0, 0], [0, 0], 0x08
        try:
            return engine.buttons, list(engine.axes), list(engine.triggers), engine.hat
        except AttributeError:
            try:
                return engine._buttons, list(engine._axes), list(engine._triggers), engine._hat
            except AttributeError:
                return 0, [0, 0, 0, 0], [0, 0], 0x08

    def _status_dict(self) -> Dict[str, Any]:
        buttons, axes, triggers, hat = self._visualizer_state()
        return {
            KEY_STATUS: self._status,
            KEY_MODE: self._mode,
            KEY_PROFILE: self._profile,
            KEY_DETAIL: self._detail,
            KEY_ERROR: self._error,
            KEY_CONN: self._conn,
            KEY_CONNECTED: self._connected,
            KEY_PAIRED: self._paired,
            KEY_TRUSTED: self._trusted,
            KEY_INPUT: self._input_label,
            KEY_BATTERY: self._battery,
            KEY_DEVICE_NAME: self._device_name,
            KEY_DEVICE_ADDR: self._device_addr,
            KEY_READY: self._ready(),
            KEY_KB_CAPABLE: self._kb_capable(),
            KEY_BUTTONS: buttons,
            KEY_AXES: axes,
            KEY_TRIGGERS: triggers,
            KEY_HAT: hat,
        }

    def _status_variant_dict(self) -> Dict[str, GLib.Variant]:
        data = self._status_dict()
        variants = {}
        for key, value in data.items():
            if key in (KEY_BUTTONS,):
                variants[key] = GLib.Variant("u", value)
            elif key in (KEY_AXES,):
                variants[key] = GLib.Variant("ai", value)
            elif key in (KEY_TRIGGERS,):
                variants[key] = GLib.Variant("ai", value)
            elif key in (KEY_HAT, KEY_BATTERY):
                variants[key] = GLib.Variant("y", value)
            elif isinstance(value, bool):
                variants[key] = GLib.Variant("b", value)
            else:
                variants[key] = GLib.Variant("s", str(value))
        return variants

    def _emit_status(self) -> None:
        if self.bus is None:
            return
        try:
            self.bus.emit_signal(
                None,
                OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant(
                    "(sa{sv}as)",
                    (
                        MANAGER_IFACE,
                        {"Status": GLib.Variant("s", self._status)},
                        [],
                    ),
                ),
            )
            self.bus.emit_signal(
                None,
                OBJECT_PATH,
                MANAGER_IFACE,
                "StatusChanged",
                GLib.Variant("(s)", (self._status,)),
            )
        except Exception as e:
            logger.debug("emit status: %s", e)

    # ------------------------------------------------------------- helpers

    def _spawn_worker(self, target, *args) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()


def main() -> int:
    return EmulatorService().run()


if __name__ == "__main__":
    raise SystemExit(main())
