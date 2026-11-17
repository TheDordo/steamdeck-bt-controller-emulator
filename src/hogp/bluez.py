"""
BlueZ D-Bus + btmgmt helpers for the privileged emulator daemon.

The daemon runs as root (systemd service), so:

    * btmgmt is invoked directly (no sudo, no sudoers rules),
    * well-known L2CAP PSMs are bound in-process (see pro_controller.py),
    * GATT/advertisement registration stays ASYNC: BlueZ calls back into
      our exported ObjectManager/Properties objects while RegisterApplication
      is in flight, so a blocking call_sync on the GLib main thread deadlocks.

All GATT/advertisement registration helpers therefore take callbacks.
Adapter property reads/writes and btmgmt are safe to call synchronously.
"""

import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable, Any, List, Dict

from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

# BlueZ D-Bus constants
BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
LE_ADV_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"

# Default BLE identity for the HoG mode (static random address keeps the
# paired central from seeing a "new" device on every start).
DEFAULT_STATIC_BLE_ADDRESS = "C2:12:34:56:78:9A"


def get_system_bus() -> Gio.DBusConnection:
    """Get a connection to the system D-Bus."""
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


# ---------------------------------------------------------------------------
# Bus / adapter discovery
# ---------------------------------------------------------------------------

def bluez_has_owner(bus: Gio.DBusConnection) -> bool:
    """True while bluetoothd owns org.bluez on the system bus."""
    try:
        result = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (BLUEZ_SERVICE,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return result.get_child_value(0).get_boolean()
    except GLib.Error as e:
        logger.debug(f"NameHasOwner({BLUEZ_SERVICE}): {e}")
        return False


def find_adapter_path(bus: Gio.DBusConnection, adapter_name: str = "hci0") -> Optional[str]:
    """
    Find the BlueZ adapter object path for the given adapter name.

    Returns the path like /org/bluez/hci0 or None if not found.
    """
    try:
        result = bus.call_sync(
            BLUEZ_SERVICE,
            "/",
            DBUS_OM_IFACE,
            "GetManagedObjects",
            None,
            GLib.VariantType("(a{oa{sa{sv}}})"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        objects = result.get_child_value(0)
        for i in range(objects.n_children()):
            entry = objects.get_child_value(i)
            path = entry.get_child_value(0).get_string()
            if path.endswith(f"/{adapter_name}"):
                ifaces = entry.get_child_value(1)
                for j in range(ifaces.n_children()):
                    iface_entry = ifaces.get_child_value(j)
                    iface_name = iface_entry.get_child_value(0).get_string()
                    if iface_name == ADAPTER_IFACE:
                        return path
    except GLib.Error as e:
        logger.error(f"Error finding adapter: {e}")
    return None


def get_adapter_property(bus: Gio.DBusConnection, adapter_path: str, prop_name: str) -> Any:
    """Get a property from the adapter."""
    try:
        result = bus.call_sync(
            BLUEZ_SERVICE,
            adapter_path,
            DBUS_PROPS_IFACE,
            "Get",
            GLib.Variant("(ss)", (ADAPTER_IFACE, prop_name)),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return result.get_child_value(0).get_variant()
    except GLib.Error as e:
        logger.error(f"Error getting adapter property {prop_name}: {e}")
        return None


def set_adapter_property(bus: Gio.DBusConnection, adapter_path: str, prop_name: str, value: GLib.Variant) -> bool:
    """Set a property on the adapter."""
    try:
        bus.call_sync(
            BLUEZ_SERVICE,
            adapter_path,
            DBUS_PROPS_IFACE,
            "Set",
            GLib.Variant("(ssv)", (ADAPTER_IFACE, prop_name, value)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return True
    except GLib.Error as e:
        logger.error(f"Error setting adapter property {prop_name}: {e}")
        return False


def set_adapter_alias(bus: Gio.DBusConnection, adapter_path: str, alias: str) -> bool:
    """Set the adapter's Alias (visible Bluetooth name)."""
    return set_adapter_property(bus, adapter_path, "Alias", GLib.Variant("s", alias))


def ensure_adapter_powered(bus: Gio.DBusConnection, adapter_path: str) -> bool:
    """Power the adapter on and allow pairing."""
    if not set_adapter_property(bus, adapter_path, "Powered", GLib.Variant("b", True)):
        return False
    set_adapter_property(bus, adapter_path, "Pairable", GLib.Variant("b", True))
    return True


def get_adapter_address(bus: Gio.DBusConnection, adapter_path: str) -> Optional[str]:
    """Read the adapter's BR/EDR public address (used in device-info replies)."""
    try:
        result = bus.call_sync(
            BLUEZ_SERVICE,
            adapter_path,
            DBUS_PROPS_IFACE,
            "Get",
            GLib.Variant("(ss)", (ADAPTER_IFACE, "Address")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return result.get_child_value(0).get_variant().get_string()
    except GLib.Error as e:
        logger.warning(f"Could not read adapter address: {e}")
        return None


def get_adapter_index(adapter_name: str = "hci0") -> int:
    """Extract adapter index from adapter name (e.g., 'hci0' -> 0)."""
    if adapter_name.startswith("hci"):
        try:
            return int(adapter_name[3:])
        except ValueError:
            pass
    return 0


def get_le_advertising_active_instances(bus: Gio.DBusConnection, adapter_path: str) -> int:
    """Get the number of active advertising instances."""
    try:
        result = bus.call_sync(
            BLUEZ_SERVICE,
            adapter_path,
            DBUS_PROPS_IFACE,
            "Get",
            GLib.Variant("(ss)", (LE_ADV_MANAGER_IFACE, "ActiveInstances")),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return result.get_child_value(0).get_variant().get_byte()
    except GLib.Error as e:
        logger.error(f"Error getting ActiveInstances: {e}")
        return -1


# ---------------------------------------------------------------------------
# GATT application / advertisement registration (async — BlueZ calls back
# into our exported objects during registration, so call_sync deadlocks)
# ---------------------------------------------------------------------------

def register_application_async(
    bus: Gio.DBusConnection,
    adapter_path: str,
    app_path: str,
    callback: Callable[[bool, Optional[str]], None],
) -> None:
    """
    Register a GATT application with BlueZ asynchronously.

    The callback receives (success: bool, error_message: Optional[str]).
    """
    def on_done(connection, result, user_data):
        try:
            connection.call_finish(result)
            logger.info(f"GATT application registered at {app_path}")
            callback(True, None)
        except GLib.Error as e:
            logger.error(f"Failed to register GATT application: {e}")
            callback(False, str(e))

    bus.call(
        BLUEZ_SERVICE,
        adapter_path,
        GATT_MANAGER_IFACE,
        "RegisterApplication",
        GLib.Variant("(oa{sv})", (app_path, {})),
        None,
        Gio.DBusCallFlags.NONE,
        30000,  # 30 second timeout
        None,
        on_done,
        None,
    )


def unregister_application_async(
    bus: Gio.DBusConnection,
    adapter_path: str,
    app_path: str,
    callback: Optional[Callable[[bool, Optional[str]], None]] = None,
) -> None:
    """
    Unregister a GATT application from BlueZ asynchronously.
    """
    def on_done(connection, result, user_data):
        try:
            connection.call_finish(result)
            logger.info(f"GATT application unregistered from {app_path}")
            if callback:
                callback(True, None)
        except GLib.Error as e:
            logger.warning(f"Failed to unregister GATT application (may be normal on shutdown): {e}")
            if callback:
                callback(False, str(e))

    bus.call(
        BLUEZ_SERVICE,
        adapter_path,
        GATT_MANAGER_IFACE,
        "UnregisterApplication",
        GLib.Variant("(o)", (app_path,)),
        None,
        Gio.DBusCallFlags.NONE,
        5000,
        None,
        on_done,
        None,
    )


def register_advertisement_async(
    bus: Gio.DBusConnection,
    adapter_path: str,
    adv_path: str,
    callback: Callable[[bool, Optional[str]], None],
) -> None:
    """
    Register an LE advertisement with BlueZ asynchronously.
    """
    def on_done(connection, result, user_data):
        try:
            connection.call_finish(result)
            logger.info(f"Advertisement registered at {adv_path}")
            callback(True, None)
        except GLib.Error as e:
            logger.error(f"Failed to register advertisement: {e}")
            callback(False, str(e))

    bus.call(
        BLUEZ_SERVICE,
        adapter_path,
        LE_ADV_MANAGER_IFACE,
        "RegisterAdvertisement",
        GLib.Variant("(oa{sv})", (adv_path, {})),
        None,
        Gio.DBusCallFlags.NONE,
        30000,
        None,
        on_done,
        None,
    )


def unregister_application_sync(
    bus: Gio.DBusConnection,
    adapter_path: str,
    app_path: str,
    timeout_ms: int = 5000,
) -> bool:
    """Unregister GATT application (blocking, for teardown). BlueZ does not
    call back into our objects for unregister, so blocking is safe."""
    try:
        bus.call_sync(
            BLUEZ_SERVICE,
            adapter_path,
            GATT_MANAGER_IFACE,
            "UnregisterApplication",
            GLib.Variant("(o)", (app_path,)),
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
        logger.info(f"GATT application unregistered from {app_path}")
        return True
    except GLib.Error as e:
        logger.warning(f"UnregisterApplication: {e}")
        return False


def unregister_advertisement_sync(
    bus: Gio.DBusConnection,
    adapter_path: str,
    adv_path: str,
    timeout_ms: int = 5000,
) -> bool:
    """Unregister LE advertisement (blocking, for teardown)."""
    try:
        bus.call_sync(
            BLUEZ_SERVICE,
            adapter_path,
            LE_ADV_MANAGER_IFACE,
            "UnregisterAdvertisement",
            GLib.Variant("(o)", (adv_path,)),
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
        logger.info(f"Advertisement unregistered from {adv_path}")
        return True
    except GLib.Error as e:
        logger.warning(f"UnregisterAdvertisement: {e}")
        return False


def unregister_advertisement_async(
    bus: Gio.DBusConnection,
    adapter_path: str,
    adv_path: str,
    callback: Optional[Callable[[bool, Optional[str]], None]] = None,
) -> None:
    """
    Unregister an LE advertisement from BlueZ asynchronously.
    """
    def on_done(connection, result, user_data):
        try:
            connection.call_finish(result)
            logger.info(f"Advertisement unregistered from {adv_path}")
            if callback:
                callback(True, None)
        except GLib.Error as e:
            logger.warning(f"Failed to unregister advertisement (may be normal on shutdown): {e}")
            if callback:
                callback(False, str(e))

    bus.call(
        BLUEZ_SERVICE,
        adapter_path,
        LE_ADV_MANAGER_IFACE,
        "UnregisterAdvertisement",
        GLib.Variant("(o)", (adv_path,)),
        None,
        Gio.DBusCallFlags.NONE,
        5000,
        None,
        on_done,
        None,
    )


# ---------------------------------------------------------------------------
# btmgmt (direct — the daemon is root; no sudo, no sudoers)
# ---------------------------------------------------------------------------

def _run_btmgmt(adapter_index: int, *args: str, timeout: float = 8.0) -> "subprocess.CompletedProcess":
    """Run a btmgmt subcommand directly (root daemon)."""
    cmd = ["btmgmt", "--index", str(adapter_index), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def btmgmt_ok(adapter_index: int, *args: str, timeout: float = 15.0) -> bool:
    """Run btmgmt, returning True on success (logs failures). btmgmt can hang
    while bluetoothd restarts; a generous timeout keeps best-effort calls
    from stalling the daemon's teardown path."""
    try:
        result = _run_btmgmt(adapter_index, *args, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning(f"btmgmt {' '.join(args)}: {e}")
        return False
    if result.returncode == 0:
        logger.info(f"btmgmt {' '.join(args)} ok")
        return True
    logger.warning(
        f"btmgmt {' '.join(args)} failed: {result.stderr or result.stdout or result.returncode}"
    )
    return False


@dataclass
class AdapterSettings:
    """Adapter settings we touch, snapshotted before a session and restored
    when it stops so normal Deck Bluetooth (audio etc.) works again."""
    name: Optional[str] = None
    alias: Optional[str] = None
    class_of_device: Optional[str] = None
    powered: bool = True
    connectable: bool = True
    pairable: bool = True
    discoverable: bool = False
    ssp: bool = True
    secure_conn: bool = True
    bredr: bool = True
    le: bool = True
    # any settings tokens we did not recognise (kept to log on restore)
    unknown: List[str] = field(default_factory=list)


_SETTINGS_TOKEN_RE = re.compile(r"^\s*current settings:\s*(.*)$", re.IGNORECASE)
_NAME_RE = re.compile(r"^\s*name\s*(.*)$")
_CLASS_RE = re.compile(r"class\s+(0x[0-9a-fA-F]+)")
_TOKEN_RE = re.compile(r"[^\s]+")


def snapshot_adapter_settings(bus: Gio.DBusConnection, adapter_path: str, adapter_index: int) -> AdapterSettings:
    """
    Capture the current adapter posture (name/alias/class/settings) so it can
    be restored when the emulator session stops. Never raises; falls back to
    defaults so a session start cannot be blocked by a parse failure.
    """
    state = AdapterSettings()
    try:
        info = _run_btmgmt(adapter_index, "info", timeout=6.0).stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        info = ""

    m = _NAME_RE.search(info)
    if m:
        name = m.group(1).strip()
        state.name = name if name else None
    m = _CLASS_RE.search(info)
    if m:
        state.class_of_device = m.group(1)
    m = _SETTINGS_TOKEN_RE.search(info)
    if m:
        tokens = {t.lower() for t in _TOKEN_RE.findall(m.group(1))}
        state.powered = "powered" in tokens
        state.connectable = "connectable" in tokens
        state.pairable = "pairable" in tokens
        state.discoverable = "discoverable" in tokens
        state.ssp = "ssp" in tokens
        state.secure_conn = "secure-conn" in tokens
        state.bredr = "br/edr" in tokens
        state.le = "le" in tokens
        known = {
            "powered", "connectable", "pairable", "fast-connectable",
            "discoverable", "bondable", "link-security", "ssp", "br/edr",
            "le", "advertising", "secure-conn", "debug-keys", "privacy",
            "configuration", "static-addr", "phy-configuration",
            "wide-band-speech", "cis-central", "cis-peripheral",
            "iso-broadcaster", "iso-sync-receiver",
        }
        state.unknown = sorted(tokens - known)

    alias = get_adapter_property(bus, adapter_path, "Alias")
    if alias is not None:
        state.alias = alias.get_string()

    logger.info(
        "Adapter snapshot: name=%r class=%s bredr=%s le=%s ssp=%s sc=%s "
        "connectable=%s pairable=%s discoverable=%s",
        state.name, state.class_of_device, state.bredr, state.le, state.ssp,
        state.secure_conn, state.connectable, state.pairable, state.discoverable,
    )
    return state


def restore_adapter_settings(
    bus: Gio.DBusConnection,
    adapter_path: str,
    adapter_index: int,
    state: AdapterSettings,
) -> None:
    """
    Restore the adapter to its pre-session posture. Best effort: every step is
    independent so one failure cannot leave the adapter half-restored.
    """
    logger.info("Restoring adapter settings…")
    btmgmt_ok(adapter_index, "power", "off")

    if state.name is not None:
        btmgmt_ok(adapter_index, "name", state.name)
    if state.class_of_device is not None:
        btmgmt_ok(adapter_index, "class", state.class_of_device)
    btmgmt_ok(adapter_index, "bredr", "on" if state.bredr else "off")
    btmgmt_ok(adapter_index, "le", "on" if state.le else "off")
    btmgmt_ok(adapter_index, "ssp", "on" if state.ssp else "off")
    btmgmt_ok(adapter_index, "sc", "on" if state.secure_conn else "off")
    btmgmt_ok(adapter_index, "connectable", "on" if state.connectable else "off")
    btmgmt_ok(adapter_index, "pairable", "on" if state.pairable else "off")

    btmgmt_ok(adapter_index, "power", "on")

    set_adapter_property(
        bus, adapter_path, "Discoverable",
        GLib.Variant("b", state.discoverable),
    )
    if state.alias is not None:
        set_adapter_property(bus, adapter_path, "Alias", GLib.Variant("s", state.alias))
    else:
        set_adapter_property(bus, adapter_path, "Alias", GLib.Variant("s", "Steam Deck"))

    if state.unknown:
        logger.info("Not restoring unrecognised settings: %s", ", ".join(state.unknown))
    logger.info("Adapter restored")


# ---------------------------------------------------------------------------
# Per-mode adapter posture
# ---------------------------------------------------------------------------

def configure_adapter_for_hog_peripheral(adapter_index: int = 0) -> bool:
    """
    LE-only peripheral posture while HoG runs.

    Stops macOS from treating the Deck as a BR/EDR laptop and opening the
    Bluetooth audio sink (A2DP) on the same bond.
    """
    logger.info("Configuring adapter for LE HID peripheral mode...")
    ok = True
    btmgmt_ok(adapter_index, "power", "off")
    if not btmgmt_ok(adapter_index, "bredr", "off"):
        ok = False
    btmgmt_ok(adapter_index, "le", "on")
    btmgmt_ok(adapter_index, "class", "0x002508")   # Peripheral / Gamepad
    btmgmt_ok(adapter_index, "connectable", "on")
    btmgmt_ok(adapter_index, "pairable", "on")
    btmgmt_ok(adapter_index, "power", "on")
    return ok


def configure_adapter_for_pro_controller(adapter_index: int = 0) -> bool:
    """
    Classic BR/EDR posture for the Pro Controller gamepad mode.

    macOS pairs real Pro Controllers as BR/EDR HID peripherals, so unlike the
    LE HoG mode we keep BR/EDR enabled and advertise a gamepad Class of
    Device under the name "Pro Controller".
    """
    logger.info("Configuring adapter for Pro Controller (BR/EDR HID) mode...")
    ok = True
    btmgmt_ok(adapter_index, "power", "off")
    btmgmt_ok(adapter_index, "bredr", "on")
    btmgmt_ok(adapter_index, "le", "on")
    btmgmt_ok(adapter_index, "name", "Pro Controller")
    if not btmgmt_ok(adapter_index, "class", "0x002508"):
        ok = False
    btmgmt_ok(adapter_index, "ssp", "on")
    btmgmt_ok(adapter_index, "sc", "on")
    btmgmt_ok(adapter_index, "connectable", "on")
    btmgmt_ok(adapter_index, "pairable", "on")
    btmgmt_ok(adapter_index, "power", "on")
    return ok


# ---------------------------------------------------------------------------
# Static BLE address (identity stability across starts)
# ---------------------------------------------------------------------------

def check_static_address_set(adapter_index: int = 0) -> bool:
    """True when the adapter currently has a static (random) address set."""
    try:
        info = _run_btmgmt(adapter_index, "info", timeout=6.0).stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return "static-addr" in info.lower()


def set_static_ble_address(
    adapter_index: int = 0,
    address: str = DEFAULT_STATIC_BLE_ADDRESS,
) -> bool:
    """
    Set a static BLE address for the adapter to prevent identity rotation.

    This prevents the device from appearing as a new controller on each
    connection. The daemon runs as root, so btmgmt works directly.
    """
    if check_static_address_set(adapter_index):
        logger.info(f"Static BLE address already configured for adapter {adapter_index}")
        return True

    logger.info(f"Configuring static BLE address {address} for adapter {adapter_index}")
    btmgmt_ok(adapter_index, "power", "off")
    time.sleep(0.5)
    ok = btmgmt_ok(adapter_index, "static-addr", address)
    time.sleep(0.5)
    btmgmt_ok(adapter_index, "power", "on")
    if ok:
        logger.info(f"Successfully set static BLE address: {address}")
    return ok


def clear_static_ble_address(adapter_index: int = 0) -> bool:
    """Clear the static BLE address (restores normal address rotation)."""
    if not check_static_address_set(adapter_index):
        return True
    btmgmt_ok(adapter_index, "power", "off")
    time.sleep(0.5)
    ok = btmgmt_ok(adapter_index, "static-addr", "00:00:00:00:00:00")
    time.sleep(0.5)
    btmgmt_ok(adapter_index, "power", "on")
    return ok


# ---------------------------------------------------------------------------
# Connected device helpers (pairing/bonding status for the UI)
# ---------------------------------------------------------------------------

def get_connected_devices(bus: Gio.DBusConnection, adapter_path: str) -> List[Dict]:
    """
    Get list of devices currently connected to the adapter.

    Returns:
        List of dicts with 'path', 'name', 'address', 'connected' keys
    """
    devices = []
    try:
        result = bus.call_sync(
            BLUEZ_SERVICE,
            "/",
            DBUS_OM_IFACE,
            "GetManagedObjects",
            None,
            GLib.VariantType("(a{oa{sa{sv}}})"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        objects = result.get_child_value(0)
        for i in range(objects.n_children()):
            entry = objects.get_child_value(i)
            path = entry.get_child_value(0).get_string()

            # Only consider devices under our adapter
            if not path.startswith(adapter_path):
                continue

            ifaces = entry.get_child_value(1)
            device_props = None

            for j in range(ifaces.n_children()):
                iface_entry = ifaces.get_child_value(j)
                iface_name = iface_entry.get_child_value(0).get_string()

                if iface_name == DEVICE_IFACE:
                    device_props = iface_entry.get_child_value(1)
                    break

            if device_props:
                props_dict = {}
                for k in range(device_props.n_children()):
                    prop_entry = device_props.get_child_value(k)
                    prop_name = prop_entry.get_child_value(0).get_string()
                    prop_value = prop_entry.get_child_value(1).get_variant()
                    props_dict[prop_name] = prop_value

                connected = props_dict.get("Connected", GLib.Variant("b", False)).get_boolean()

                if connected:
                    devices.append({
                        "path": path,
                        "name": props_dict.get("Name", GLib.Variant("s", "Unknown")).get_string(),
                        "address": props_dict.get("Address", GLib.Variant("s", "")).get_string(),
                        "connected": connected,
                        "paired": props_dict.get("Paired", GLib.Variant("b", False)).get_boolean(),
                        "trusted": props_dict.get("Trusted", GLib.Variant("b", False)).get_boolean(),
                        "bonded": props_dict.get("Bonded", GLib.Variant("b", False)).get_boolean(),
                    })
    except GLib.Error as e:
        logger.error(f"Error getting connected devices: {e}")

    return devices


def get_device_property(bus: Gio.DBusConnection, device_path: str, prop_name: str) -> Any:
    """Read one org.bluez.Device1 property."""
    try:
        result = bus.call_sync(
            BLUEZ_SERVICE,
            device_path,
            DBUS_PROPS_IFACE,
            "Get",
            GLib.Variant("(ss)", (DEVICE_IFACE, prop_name)),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return result.get_child_value(0).get_variant()
    except GLib.Error as e:
        logger.debug(f"Get {prop_name} on {device_path}: {e}")
        return None


def set_device_trusted(bus: Gio.DBusConnection, device_path: str, trusted: bool = True) -> bool:
    """Mark remote central trusted (auto-reconnect on macOS/Linux)."""
    try:
        bus.call_sync(
            BLUEZ_SERVICE,
            device_path,
            DBUS_PROPS_IFACE,
            "Set",
            GLib.Variant("(ssv)", (DEVICE_IFACE, "Trusted", GLib.Variant("b", trusted))),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        logger.info(f"Set Trusted={trusted} on {device_path}")
        return True
    except GLib.Error as e:
        logger.warning(f"Could not set Trusted on {device_path}: {e}")
        return False


def pair_device_sync(
    bus: Gio.DBusConnection,
    device_path: str,
    timeout_ms: int = 25000,
) -> tuple:
    """Complete SMP pairing with connected central (e.g. Mac)."""
    try:
        bus.call_sync(
            BLUEZ_SERVICE,
            device_path,
            DEVICE_IFACE,
            "Pair",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
        logger.info(f"Paired with {device_path}")
        return True, None
    except GLib.Error as e:
        msg = str(e)
        if "AlreadyExists" in msg or "Already Paired" in msg:
            return True, None
        logger.warning(f"Pair failed for {device_path}: {e}")
        return False, msg


def complete_bond_with_central(
    bus: Optional[Gio.DBusConnection],
    adapter_path: str,
) -> tuple:
    """
    If a central is connected but not bonded, run Pair and Trust.
    Returns (updated device info dict or None, error message or None).
    """
    if bus is None:
        bus = get_system_bus()
    device = get_primary_connected_device(bus, adapter_path)
    if not device:
        return None, "No connected device"
    path = device["path"]
    paired = device.get("paired", False)
    bonded = device.get("bonded", False)
    err: Optional[str] = None
    set_device_trusted(bus, path, True)
    if not paired and not bonded:
        ok, pair_err = pair_device_sync(bus, path)
        if not ok:
            err = pair_err
            logger.warning(f"Bond incomplete: {pair_err}")
    updated = get_primary_connected_device(bus, adapter_path)
    if updated and (updated.get("paired") or updated.get("bonded")):
        set_device_trusted(bus, path, True)
        err = None
    return updated, err


def get_primary_connected_device(bus: Gio.DBusConnection, adapter_path: str) -> Optional[Dict]:
    """
    Get the primary (first) connected device info.

    Returns:
        Dict with 'name', 'address' keys or None if no device connected
    """
    devices = get_connected_devices(bus, adapter_path)
    return devices[0] if devices else None


# ---------------------------------------------------------------------------
# Pro Controller (BR/EDR HID) SDP record helpers
# ---------------------------------------------------------------------------

PRO_CONTROLLER_NAME = "Pro Controller"
PRO_CONTROLLER_CLASS = 0x002508  # Peripheral / Gamepad
HID_SDP_PROFILE_PATH = "/com/steamdeck/hogp/procon"  # D-Bus paths forbid '-' in elements
_HID_SDP_STATE = {"last_error": None}  # last RegisterProfile failure detail

_OBJECT_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]*$")


def _is_valid_object_path(path) -> bool:
    """D-Bus object path validation, used defensively before building
    GVariant('o') values (PyGObject aborts leaf constructors on bad paths)."""
    return isinstance(path, str) and bool(_OBJECT_PATH_RE.match(path))


def register_hid_sdp_profile(
    bus: Gio.DBusConnection,
    sdp_xml: str,
    profile_path: str = HID_SDP_PROFILE_PATH,
) -> bool:
    """
    Register the HID SDP record (L2CAP PSM 0x11/0x13 + Pro Controller report
    map) with BlueZ so the Mac can discover and pair the gamepad.

    Modern BlueZ (>=5.10) accepts a raw ServiceRecord XML via
    ProfileManager1.RegisterProfile; no sdptool needed. No Profile1 object
    is exported: BlueZ only consults it when a client connects to a PSM it
    manages, and we pass no PSM option, so our own L2CAP sockets own both
    HID channels (same design as joycontrol). The UUID argument is random
    so it never collides with BlueZ's built-in HID (input) profile.
    """
    if not _is_valid_object_path(profile_path):
        _HID_SDP_STATE["last_error"] = f"invalid profile object path: {profile_path!r}"
        logger.error("Invalid SDP profile object path: %r", profile_path)
        return False
    try:
        # Clear a stale record from a previous run/crash so a second Start in
        # the same session cannot fail with AlreadyExists.
        unregister_hid_sdp_profile(bus, profile_path)

        # Build the (osa{sv}) parameters with explicit leaf constructors.
        # PyGObject's tuple-form GLib.Variant() marshalling can fail on 'o'
        # members with an opaque "constructor returned NULL", so avoid it.
        params = GLib.Variant.new_tuple(
            GLib.Variant.new_object_path(profile_path),
            GLib.Variant.new_string(str(uuid.uuid4())),
            GLib.Variant.new_array(
                GLib.VariantType.new("{sv}"),
                [
                    GLib.Variant.new_dict_entry(
                        GLib.Variant.new_string("ServiceRecord"),
                        GLib.Variant.new_variant(GLib.Variant.new_string(sdp_xml)),
                    ),
                    GLib.Variant.new_dict_entry(
                        GLib.Variant.new_string("Role"),
                        GLib.Variant.new_variant(GLib.Variant.new_string("server")),
                    ),
                    GLib.Variant.new_dict_entry(
                        GLib.Variant.new_string("RequireAuthentication"),
                        GLib.Variant.new_variant(GLib.Variant.new_boolean(False)),
                    ),
                    GLib.Variant.new_dict_entry(
                        GLib.Variant.new_string("RequireAuthorization"),
                        GLib.Variant.new_variant(GLib.Variant.new_boolean(False)),
                    ),
                ],
            ),
        )
        bus.call_sync(
            BLUEZ_SERVICE,
            "/org/bluez",
            "org.bluez.ProfileManager1",
            "RegisterProfile",
            params,
            None,
            Gio.DBusCallFlags.NONE,
            10000,
            None,
        )
        _HID_SDP_STATE["last_error"] = None
        logger.info("HID SDP record registered with BlueZ")
        return True
    except Exception as e:
        _HID_SDP_STATE["last_error"] = (
            f"{type(e).__name__}: {e} (profile path {profile_path!r})"
        )
        logger.error("Could not register HID SDP record: %s", _HID_SDP_STATE["last_error"])
        return False


def unregister_hid_sdp_profile(
    bus: Gio.DBusConnection,
    profile_path: str = HID_SDP_PROFILE_PATH,
) -> bool:
    """Remove the HID SDP record registered by register_hid_sdp_profile()."""
    if not _is_valid_object_path(profile_path):
        return False
    try:
        params = GLib.Variant.new_tuple(
            GLib.Variant.new_object_path(profile_path)
        )
        bus.call_sync(
            BLUEZ_SERVICE,
            "/org/bluez",
            "org.bluez.ProfileManager1",
            "UnregisterProfile",
            params,
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        logger.info("HID SDP record unregistered")
        return True
    except GLib.Error as e:
        logger.debug(f"UnregisterProfile: {e}")
        return False


def get_last_sdp_error() -> Optional[str]:
    """Last RegisterProfile failure detail (for UI display)."""
    return _HID_SDP_STATE.get("last_error")
