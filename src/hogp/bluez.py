"""
BlueZ D-Bus helpers for GATT and advertising operations.

Provides async-friendly wrappers around BlueZ's D-Bus APIs to avoid
deadlocks with GDBus on older glib versions.
"""

import logging
import subprocess
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


def get_system_bus() -> Gio.DBusConnection:
    """Get a connection to the system D-Bus."""
    return Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


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
    """Unregister GATT application (blocking, for shutdown)."""
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
    """Unregister LE advertisement (blocking, for shutdown)."""
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


def ensure_adapter_powered_and_discoverable(bus: Gio.DBusConnection, adapter_path: str) -> bool:
    """Power on adapter and allow pairing; LE discoverability comes from our advertisement."""
    if not set_adapter_property(bus, adapter_path, "Powered", GLib.Variant("b", True)):
        return False
    set_adapter_property(bus, adapter_path, "Pairable", GLib.Variant("b", True))
    set_adapter_property(bus, adapter_path, "Bondable", GLib.Variant("b", True))  # may be unsupported on some stacks
    # BR/EDR discoverable off — LE advertisement sets Discoverable for centrals.
    set_adapter_property(bus, adapter_path, "Discoverable", GLib.Variant("b", False))
    return True


def acquire_app_bus_name(bus: Gio.DBusConnection, name: str = "com.steamdeck.hogp") -> bool:
    """Request a well-known D-Bus name (required by some BlueZ versions)."""
    try:
        flags = 0x1 | 0x4  # ALLOW_REPLACEMENT | DO_NOT_QUEUE
        result = bus.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (name, flags)),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        reply_code = result.get_child_value(0).get_uint32()
        if reply_code in (1, 2):
            logger.info(f"D-Bus name acquired: {name}")
            return True
        if reply_code == 3:
            logger.warning(f"D-Bus name {name} queued (unexpected)")
            return True
        logger.warning(f"RequestName for {name} returned code {reply_code}")
        return False
    except GLib.Error as e:
        logger.warning(f"Could not acquire D-Bus name {name}: {e}")
        return False


def get_adapter_index(adapter_name: str = "hci0") -> int:
    """Extract adapter index from adapter name (e.g., 'hci0' -> 0)."""
    if adapter_name.startswith("hci"):
        try:
            return int(adapter_name[3:])
        except ValueError:
            pass
    return 0


def check_static_address_set(adapter_index: int = 0) -> bool:
    """
    Check if a static BLE address is already configured.
    Returns True if a static address is set, False otherwise.
    """
    try:
        result = subprocess.run(
            ["btmgmt", "--index", str(adapter_index), "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Look for "static-addr" in the output
            return "static-addr" in result.stdout.lower()
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.debug(f"Could not check static address status: {e}")
        return False


def set_static_ble_address(
    adapter_index: int = 0,
    address: str = "C2:12:34:56:78:9A",
) -> bool:
    """
    Set a static BLE address for the adapter to prevent identity rotation.
    
    This prevents the device from appearing as a new controller on each connection.
    Uses sudo for btmgmt commands (passwordless via sudoers configuration).
    
    Args:
        adapter_index: BlueZ adapter index (0 for hci0, 1 for hci1, etc.)
        address: Static random address (must have bit 1 set in first octet for locally administered)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        # Check if already set
        if check_static_address_set(adapter_index):
            logger.info(f"Static BLE address already configured for adapter {adapter_index}")
            return True
        
        logger.info(f"Configuring static BLE address {address} for adapter {adapter_index}")
        
        # Power off adapter (use sudo)
        subprocess.run(
            ["sudo", "btmgmt", "--index", str(adapter_index), "power", "off"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        
        # Wait for power off to complete
        import time
        time.sleep(1)
        
        # Set static address (use sudo)
        result = subprocess.run(
            ["sudo", "btmgmt", "--index", str(adapter_index), "static-addr", address],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        
        # Wait for address to be set
        time.sleep(1)
        
        # Power back on (use sudo)
        subprocess.run(
            ["sudo", "btmgmt", "--index", str(adapter_index), "power", "on"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        
        if "successfully set" in result.stdout.lower() or result.returncode == 0:
            logger.info(f"Successfully set static BLE address: {address}")
            return True
        else:
            logger.warning(f"Static address command completed but may not have succeeded: {result.stdout}")
            return False
            
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to set static BLE address (requires sudo): {e}")
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"Could not set static BLE address: {e}")
        return False


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
                
                if iface_name == "org.bluez.Device1":
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


DEVICE_IFACE = "org.bluez.Device1"


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
) -> tuple[bool, Optional[str]]:
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
) -> tuple[Optional[Dict], Optional[str]]:
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


def _run_btmgmt(adapter_index: int, *args: str, ignore_errors: bool = True) -> bool:
    """Run btmgmt subcommand (passwordless sudo for bluetooth group)."""
    try:
        result = subprocess.run(
            ["sudo", "btmgmt", "--index", str(adapter_index), *args],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode != 0 and not ignore_errors:
            logger.warning(
                f"btmgmt {' '.join(args)} failed: {result.stderr or result.stdout}"
            )
            return False
        if result.returncode == 0:
            logger.info(f"btmgmt {' '.join(args)} ok")
        else:
            logger.debug(
                f"btmgmt {' '.join(args)}: {result.stderr or result.stdout or result.returncode}"
            )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"btmgmt {' '.join(args)}: {e}")
        return False


def configure_adapter_for_hog_peripheral(adapter_index: int = 0) -> bool:
    """
    LE-only peripheral posture while HoG runs.

    Stops macOS from treating the Deck as a BR/EDR laptop and opening the
    Bluetooth audio sink (A2DP) on the same bond.
    """
    logger.info("Configuring adapter for LE HID peripheral mode...")
    ok = True
    # Some controllers require power off before bredr off.
    _run_btmgmt(adapter_index, "power", "off")
    if not _run_btmgmt(adapter_index, "bredr", "off"):
        ok = False
    _run_btmgmt(adapter_index, "le", "on")
    # CoD: Peripheral / Gamepad (0x002508)
    _run_btmgmt(adapter_index, "class", "0x002508")
    _run_btmgmt(adapter_index, "connectable", "on")
    _run_btmgmt(adapter_index, "pairable", "on")
    _run_btmgmt(adapter_index, "power", "on")
    return ok


def restore_adapter_bluetooth_transport(adapter_index: int = 0) -> None:
    """Re-enable BR/EDR when HoG stops so normal Deck Bluetooth works again."""
    logger.info("Restoring dual BR/EDR + LE adapter mode...")
    _run_btmgmt(adapter_index, "power", "off")
    _run_btmgmt(adapter_index, "bredr", "on")
    _run_btmgmt(adapter_index, "le", "on")
    _run_btmgmt(adapter_index, "power", "on")


def reset_adapter_to_default_state(bus: Gio.DBusConnection, adapter_path: str) -> bool:
    """
    Reset adapter properties to default state for normal Bluetooth operation.
    
    This should be called when the HoG service stops to restore the adapter
    to its normal state, preventing interference with other Bluetooth devices
    (especially audio devices like AirPods that need specific profiles).
    
    Sets:
    - Discoverable: False (normal default)
    - Pairable: True (allow new pairings)
    - Powered: True (keep powered on)
    
    Returns:
        True if successful, False otherwise
    """
    logger.info("Resetting adapter to default state...")
    success = True
    
    # Set Discoverable to False (normal state)
    if not set_adapter_property(bus, adapter_path, "Discoverable", GLib.Variant("b", False)):
        logger.warning("Could not reset Discoverable property")
        success = False
    else:
        logger.info("✓ Discoverable set to False")
    
    # Set Pairable to True (allow new pairings)
    if not set_adapter_property(bus, adapter_path, "Pairable", GLib.Variant("b", True)):
        logger.warning("Could not reset Pairable property")
        success = False
    else:
        logger.info("✓ Pairable set to True")
    
    # Ensure adapter stays powered
    if not set_adapter_property(bus, adapter_path, "Powered", GLib.Variant("b", True)):
        logger.warning("Could not ensure adapter powered")
        success = False
    
    adapter_name = adapter_path.rsplit("/", 1)[-1] if adapter_path else "hci0"
    restore_adapter_bluetooth_transport(get_adapter_index(adapter_name))

    if success:
        logger.info("Adapter reset to default state successfully")

    return success


def clear_static_ble_address(adapter_index: int = 0) -> bool:
    """
    Clear/reset the static BLE address to allow normal address rotation.
    
    This restores normal Bluetooth behavior and should be called during
    uninstallation or when the service is no longer needed.
    
    Args:
        adapter_index: BlueZ adapter index (0 for hci0, 1 for hci1, etc.)
    
    Returns:
        True if successful or already cleared, False on error
    """
    try:
        # Check if a static address is set
        if not check_static_address_set(adapter_index):
            logger.info(f"No static BLE address set for adapter {adapter_index}")
            return True
        
        logger.info(f"Clearing static BLE address for adapter {adapter_index}...")
        
        # Power off adapter
        subprocess.run(
            ["sudo", "btmgmt", "--index", str(adapter_index), "power", "off"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        
        # Clear static address by setting it to 00:00:00:00:00:00
        result = subprocess.run(
            ["sudo", "btmgmt", "--index", str(adapter_index), "static-addr", "00:00:00:00:00:00"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        
        # Power back on
        subprocess.run(
            ["sudo", "btmgmt", "--index", str(adapter_index), "power", "on"],
            capture_output=True,
            timeout=5,
            check=True,
        )
        
        # Verify it was cleared
        if not check_static_address_set(adapter_index):
            logger.info(f"✓ Static BLE address cleared successfully")
            return True
        else:
            logger.warning("Static address may not have been fully cleared")
            return False
            
    except subprocess.CalledProcessError as e:
        logger.warning(f"Failed to clear static BLE address (requires sudo): {e}")
        return False
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        logger.warning(f"Could not clear static BLE address: {e}")
        return False


# ---------------------------------------------------------------------------
# Pro Controller (BR/EDR HID) gamepad mode helpers
# ---------------------------------------------------------------------------

PRO_CONTROLLER_NAME = "Pro Controller"
PRO_CONTROLLER_CLASS = 0x002508  # Peripheral / Gamepad
HID_SERVICE_UUID_FULL = "00001124-0000-1000-8000-00805f9b34fb"
HID_SDP_PROFILE_PATH = "/com/steamdeck/hogp/procon-profile"
_HID_SDP_STATE = {"object_reg": None, "last_error": None}  # Gio registration id while the profile is exported


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


def configure_adapter_for_pro_controller(adapter_index: int = 0) -> bool:
    """
    Classic BR/EDR posture for the Pro Controller gamepad mode.

    macOS pairs real Pro Controllers as BR/EDR HID peripherals, so unlike the
    LE HoG mode we keep BR/EDR enabled and advertise a gamepad Class of
    Device under the name "Pro Controller".
    """
    logger.info("Configuring adapter for Pro Controller (BR/EDR HID) mode...")
    ok = True
    _run_btmgmt(adapter_index, "power", "off")
    _run_btmgmt(adapter_index, "bredr", "on")
    _run_btmgmt(adapter_index, "le", "on")
    _run_btmgmt(adapter_index, "name", PRO_CONTROLLER_NAME)
    if not _run_btmgmt(adapter_index, "class", hex(PRO_CONTROLLER_CLASS)):
        ok = False
    _run_btmgmt(adapter_index, "ssp", "on")
    _run_btmgmt(adapter_index, "sc", "on")
    _run_btmgmt(adapter_index, "connectable", "on")
    _run_btmgmt(adapter_index, "pairable", "on")
    _run_btmgmt(adapter_index, "power", "on")
    return ok


def register_hid_sdp_profile(
    bus: Gio.DBusConnection,
    sdp_xml: str,
    profile_path: str = HID_SDP_PROFILE_PATH,
) -> bool:
    """
    Register the HID SDP record (L2CAP PSM 0x11/0x13 + Pro Controller report
    map) with BlueZ so the Mac can discover and pair the gamepad.

    Modern BlueZ (>=5.10) accepts a raw ServiceRecord XML via
    ProfileManager1.RegisterProfile; no sdptool needed. We implement a
    minimal org.bluez.Profile1 object because BlueZ requires the profile
    object to exist, but our own L2CAP sockets handle the connections
    (same design as joycontrol).
    """
    profile_xml = """
    <node>
        <interface name="org.bluez.Profile1">
            <method name="Release"/>
            <method name="NewConnection">
                <arg type="o" direction="in"/>
                <arg type="h" direction="in"/>
                <arg type="a{sv}" direction="in"/>
            </method>
            <method name="RequestDisconnection">
                <arg type="o" direction="in"/>
            </method>
        </interface>
    </node>
    """
    try:
        # Clear a stale record from a previous run/crash so a second Start in
        # the same session cannot fail with AlreadyExists.
        unregister_hid_sdp_profile(bus, profile_path)

        node_info = Gio.DBusNodeInfo.new_for_xml(profile_xml)
        _HID_SDP_STATE["object_reg"] = bus.register_object(
            profile_path,
            node_info.interfaces[0],
            lambda *args, **kwargs: None,  # method calls are no-ops
            None,
            None,
        )
        bus.call_sync(
            BLUEZ_SERVICE,
            "/org/bluez",
            "org.bluez.ProfileManager1",
            "RegisterProfile",
            GLib.Variant(
                "(osa{sv})",
                (
                    profile_path,
                    HID_SERVICE_UUID_FULL,
                    {
                        "ServiceRecord": GLib.Variant("s", sdp_xml),
                        "Role": GLib.Variant("s", "server"),
                        "RequireAuthentication": GLib.Variant("b", False),
                        "RequireAuthorization": GLib.Variant("b", False),
                    },
                ),
            ),
            None,
            Gio.DBusCallFlags.NONE,
            10000,
            None,
        )
        _HID_SDP_STATE["last_error"] = None
        logger.info("HID SDP record registered with BlueZ")
        return True
    except Exception as e:
        _HID_SDP_STATE["last_error"] = str(e)
        logger.error(f"Could not register HID SDP record: {e}")
        reg = _HID_SDP_STATE.get("object_reg")
        if reg is not None:
            try:
                bus.unregister_object(reg)
            except Exception:
                pass
        _HID_SDP_STATE["object_reg"] = None
        return False


def unregister_hid_sdp_profile(
    bus: Gio.DBusConnection,
    profile_path: str = HID_SDP_PROFILE_PATH,
) -> bool:
    """Remove the HID SDP record registered by register_hid_sdp_profile()."""
    try:
        bus.call_sync(
            BLUEZ_SERVICE,
            "/org/bluez",
            "org.bluez.ProfileManager1",
            "UnregisterProfile",
            GLib.Variant("(o)", (profile_path,)),
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
    finally:
        reg = _HID_SDP_STATE.get("object_reg")
        if reg is not None:
            try:
                bus.unregister_object(reg)
            except Exception:
                pass
        _HID_SDP_STATE["object_reg"] = None


def get_last_sdp_error() -> Optional[str]:
    """Last RegisterProfile failure detail (for UI display)."""
    return _HID_SDP_STATE.get("last_error")
