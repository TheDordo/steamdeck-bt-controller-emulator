"""
Shared D-Bus API contract between the privileged daemon (src/hogp/service.py)
and the unprivileged clients (GUI + CLI).

Architecture
------------
All Bluetooth/USB/evdev work happens inside one root daemon managed by
systemd (bt-controller-emulator.service). Clients never touch BlueZ, L2CAP,
btmgmt or /dev/input directly: they call this small D-Bus interface and
subscribe to status changes.

    com.steamdeck.btemu            well-known bus name (owned by the daemon)
    /com/steamdeck/btemu           exported object path
    com.steamdeck.btemu.Manager1   control interface
"""

# D-Bus names
BUS_NAME = "com.steamdeck.btemu"
OBJECT_PATH = "/com/steamdeck/btemu"
MANAGER_IFACE = "com.steamdeck.btemu.Manager1"
ERROR_FAILED = "com.steamdeck.btemu.Error.Failed"
DBUS_PROPS_IFACE = "org.freedesktop.DBus.Properties"

# Session status values (property "Status" / StatusChanged signal payload)
STATUS_STOPPED = "stopped"
STATUS_STARTING = "starting"
STATUS_STOPPING = "stopping"
STATUS_ACTIVE = "active"
STATUS_ERROR = "error"
STATUS_DEGRADED = "degraded"   # active session, but bluetoothd vanished; auto-recovery pending

# Transport / profile values for Start(mode, profile)
MODE_BLUETOOTH = "bluetooth"
MODE_USB = "usb"
PROFILE_GAMEPAD = "gamepad"    # BT: Pro Controller (BR/EDR) | USB: DUALSHOCK 4
PROFILE_KEYBOARD = "keyboard"  # BT: BLE HOGP gamepad/keyboard/mouse | USB: generic HID

VALID_MODES = (MODE_BLUETOOTH, MODE_USB)
VALID_PROFILES = (PROFILE_GAMEPAD, PROFILE_KEYBOARD)

# Status dict keys returned by GetStatus()
KEY_STATUS = "status"
KEY_MODE = "mode"
KEY_PROFILE = "profile"
KEY_DETAIL = "detail"          # human-readable one-liner shown by the GUI
KEY_ERROR = "error"            # last start/run error or ""
KEY_CONN = "conn"              # connection-line label text
KEY_CONNECTED = "connected"
KEY_PAIRED = "paired"
KEY_TRUSTED = "trusted"
KEY_INPUT = "input"            # active evdev device label or ""
KEY_BATTERY = "battery"        # 0-100
KEY_DEVICE_NAME = "device_name"
KEY_DEVICE_ADDR = "device_addr"
KEY_READY = "ready"            # True while the active engine accepts HID input
KEY_KB_CAPABLE = "kb_capable"  # True when keyboard/mouse reports are supported
KEY_BUTTONS = "buttons"        # 11-bit button mask (visualizer)
KEY_AXES = "axes"              # 4 x int16
KEY_TRIGGERS = "triggers"      # 2 x uint8
KEY_HAT = "hat"                # 0-8

MANAGER_XML = f"""
<node>
    <interface name="{MANAGER_IFACE}">
        <method name="Start">
            <arg type="s" direction="in" name="mode"/>
            <arg type="s" direction="in" name="profile"/>
        </method>
        <method name="Stop"/>
        <method name="GetStatus">
            <arg type="a{{sv}}" direction="out"/>
        </method>
        <method name="SendKey">
            <arg type="u" direction="in" name="code"/>
            <arg type="u" direction="in" name="modifiers"/>
            <arg type="u" direction="in" name="hold_ms"/>
        </method>
        <method name="SendMouse">
            <arg type="i" direction="in" name="dx"/>
            <arg type="i" direction="in" name="dy"/>
            <arg type="u" direction="in" name="buttons"/>
            <arg type="i" direction="in" name="wheel"/>
        </method>
        <method name="SetButton">
            <arg type="u" direction="in" name="index"/>
            <arg type="b" direction="in" name="pressed"/>
        </method>
        <method name="SetAxis">
            <arg type="u" direction="in" name="index"/>
            <arg type="i" direction="in" name="value"/>
        </method>
        <method name="SetTrigger">
            <arg type="u" direction="in" name="index"/>
            <arg type="y" direction="in" name="value"/>
        </method>
        <method name="SetHat">
            <arg type="y" direction="in" name="direction"/>
        </method>
        <property name="Status" type="s" access="read"/>
        <signal name="StatusChanged">
            <arg type="s" name="status"/>
        </signal>
    </interface>
    <interface name="{DBUS_PROPS_IFACE}">
        <method name="Get">
            <arg type="s" direction="in" name="interface_name"/>
            <arg type="s" direction="in" name="property_name"/>
            <arg type="v" direction="out"/>
        </method>
        <method name="GetAll">
            <arg type="s" direction="in" name="interface_name"/>
            <arg type="a{{sv}}" direction="out"/>
        </method>
    </interface>
</node>
"""
