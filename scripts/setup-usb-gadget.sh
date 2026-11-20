#!/bin/bash
#
# Setup USB gadget HID for Steam Deck wired controller mode
#
# This script configures the Linux USB gadget framework (configfs) to create
# virtual USB HID devices. Two gamepad identities are supported:
#
#   --profile ds4      (default) DualShock 4 identity (VID 0x054C/PID 0x05C4,
#                      64-byte reports). macOS 27's GameController framework
#                      recognises this as a natively supported controller.
#   --profile generic  Plain HID gamepad (VID 0x1209, 13-byte reports).
#                      Works with Steam/games that accept generic HID pads.
#   --profile pro      Nintendo Switch Pro Controller (057E:2009, 64-byte
#                      reports, USB 0x80 handshake handled by the daemon).
#   --profile xbox     Xbox Series X|S controller (045E:0B12). Not HID: a
#                      FunctionFS vendor interface (class FF/47/D0) speaking
#                      GIP. macOS drives it with its built-in XboxGamepad
#                      driver. Gamepad-only, like the real controller. The
#                      daemon writes the FunctionFS descriptors and binds
#                      the UDC (this script stops before binding).
#
# Requires: configfs mounted, USB gadget drivers loaded, root/sudo access
#

set -euo pipefail

PROFILE="ds4"
if [ "${1:-}" = "--profile" ]; then
    PROFILE="${2:-ds4}"
    case "$PROFILE" in
        ds4|generic|pro|xbox) ;;
        *) echo "Error: profile must be 'ds4', 'generic', 'pro' or 'xbox'"; exit 1 ;;
    esac
fi

GADGET_NAME="steamdeck_hid"
GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME}"
UDC_DEVICE=""  # Will be auto-detected

# Get script directory before we cd elsewhere
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# USB IDs
if [ "$PROFILE" = "ds4" ]; then
    VENDOR_ID="0x054C"      # Sony Interactive Entertainment
    PRODUCT_ID="0x05C4"     # DualShock 4 (v1)
    DEVICE_BCD="0x0100"     # Device version 1.0
    MANUFACTURER="Sony Interactive Entertainment"
    PRODUCT="Wireless Controller"
    SERIAL="a01e0d000000"
    GAMEPAD_REPORT_LENGTH=64
elif [ "$PROFILE" = "pro" ]; then
    VENDOR_ID="0x057E"      # Nintendo
    PRODUCT_ID="0x2009"     # Switch Pro Controller
    DEVICE_BCD="0x0210"
    MANUFACTURER="Nintendo Co., Ltd."
    PRODUCT="Pro Controller"
    SERIAL="000000000001"
    GAMEPAD_REPORT_LENGTH=64
elif [ "$PROFILE" = "xbox" ]; then
    VENDOR_ID="0x045E"      # Microsoft
    PRODUCT_ID="0x0B12"     # Xbox Series X|S controller (USB)
    DEVICE_BCD="0x0509"
    MANUFACTURER="Microsoft"
    PRODUCT="Controller"
    SERIAL="3039373132333332"
    GAMEPAD_REPORT_LENGTH=0  # n/a (FunctionFS)
else
    VENDOR_ID="0x1209"      # pid.codes (Open Source)
    PRODUCT_ID="0x0001"     # Generic HID Device
    DEVICE_BCD="0x0100"     # Device version 1.0
    MANUFACTURER="SteamDeckPad"
    PRODUCT="USB HID Gamepad"
    SERIAL="steamdeck001"
    GAMEPAD_REPORT_LENGTH=13
fi
USB_BCD="0x0200"            # USB 2.0

# Configuration
CONFIG_NAME="c.1"
MAX_POWER="250"  # 250mA
CONFIG_STRING="HID Gadget Configuration"

echo "=== Setting up USB Gadget HID for Steam Deck (profile: ${PROFILE}) ==="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root (use sudo)"
    exit 1
fi

# Load modules if not loaded
echo "Loading USB gadget modules..."
modprobe libcomposite 2>/dev/null || echo "libcomposite already loaded"
modprobe usb_f_hid 2>/dev/null || echo "usb_f_hid already loaded"

# Mount configfs if not mounted
if ! mountpoint -q /sys/kernel/config; then
    echo "Mounting configfs..."
    mount -t configfs none /sys/kernel/config
fi

# Reuse or replace an existing gadget configuration
if [ -d "${GADGET_DIR}" ]; then
    echo "Existing gadget found — cleaning up before re-creating..."
    bash "${SCRIPT_DIR}/cleanup-usb-gadget.sh"
fi

if [ -z "$(ls -A /sys/class/udc 2>/dev/null)" ]; then
    echo "Error: No UDC in /sys/class/udc."
    echo "Enable BIOS: Advanced -> USB Configuration -> USB Dual Role Device -> DRD"
    echo "Then cold boot, connect a data-capable USB-C cable, and retry."
    exit 1
fi

# Create gadget directory
echo "Creating gadget: ${GADGET_NAME}"
mkdir -p "${GADGET_DIR}"
cd "${GADGET_DIR}"

# Set USB device descriptor
echo "${VENDOR_ID}" > idVendor
echo "${PRODUCT_ID}" > idProduct
echo "${DEVICE_BCD}" > bcdDevice
echo "${USB_BCD}" > bcdUSB

# Device class: real Xbox controllers declare the GIP class at device level;
# everything else defines it per interface.
if [ "$PROFILE" = "xbox" ]; then
    echo "0xFF" > bDeviceClass
    echo "0x47" > bDeviceSubClass
    echo "0xD0" > bDeviceProtocol
else
    echo "0x00" > bDeviceClass
    echo "0x00" > bDeviceSubClass
    echo "0x00" > bDeviceProtocol
fi
# FunctionFS descriptors are full/high speed only: never negotiate SuperSpeed.
if [ -f max_speed ]; then
    echo "high-speed" > max_speed 2>/dev/null || true
fi

# Create English strings
mkdir -p strings/0x409
echo "${SERIAL}" > strings/0x409/serialnumber
echo "${MANUFACTURER}" > strings/0x409/manufacturer
echo "${PRODUCT}" > strings/0x409/product

# Create configuration
echo "Creating configuration..."
mkdir -p "configs/${CONFIG_NAME}/strings/0x409"
echo "${CONFIG_STRING}" > "configs/${CONFIG_NAME}/strings/0x409/configuration"
echo "${MAX_POWER}" > "configs/${CONFIG_NAME}/MaxPower"

if [ "$PROFILE" = "xbox" ]; then
    # ---- Xbox: one FunctionFS function, no keyboard/mouse interfaces ----
    modprobe usb_f_fs 2>/dev/null || true
    FFS_MOUNT="/dev/ffs-xbox"
    mkdir -p functions/ffs.xbox
    ln -s functions/ffs.xbox "configs/${CONFIG_NAME}/"
    mkdir -p "${FFS_MOUNT}"
    if ! mountpoint -q "${FFS_MOUNT}"; then
        mount -t functionfs xbox "${FFS_MOUNT}"
    fi
    echo "${PROFILE}" > /tmp/steamdeck_hid.profile
    echo "✓ FunctionFS mounted at ${FFS_MOUNT} — the daemon writes the GIP"
    echo "  descriptors to ${FFS_MOUNT}/ep0 and binds the UDC."
    exit 0
fi

# Create HID functions (3 separate interfaces for gamepad, keyboard, mouse)
echo "Creating HID functions..."

# Function 1: Gamepad
mkdir -p functions/hid.usb0
echo 0 > functions/hid.usb0/protocol   # 0 = none (gamepad is not boot keyboard/mouse)
echo 0 > functions/hid.usb0/subclass  # 0 = no subclass
echo "${GAMEPAD_REPORT_LENGTH}" > functions/hid.usb0/report_length  # Must match exact write() size to /dev/hidg0

# Function 2: Keyboard
mkdir -p functions/hid.usb1
echo 1 > functions/hid.usb1/protocol  # 1 = keyboard
echo 1 > functions/hid.usb1/subclass  # 1 = boot interface
echo 8 > functions/hid.usb1/report_length  # Keyboard report size (8 bytes without Report ID)

# Function 3: Mouse
mkdir -p functions/hid.usb2
echo 2 > functions/hid.usb2/protocol  # 2 = mouse
echo 1 > functions/hid.usb2/subclass  # 1 = boot interface
echo 6 > functions/hid.usb2/report_length  # Mouse report size (6 bytes without Report ID)

# Reduce the interrupt endpoint polling interval where the kernel supports it
# (f_hid poll_interval sysfs attribute, ms; ignored on kernels without it).
for f in functions/hid.usb0/poll_interval functions/hid.usb1/poll_interval functions/hid.usb2/poll_interval; do
    if [ -f "$f" ]; then
        echo 1 > "$f" 2>/dev/null || true
    fi
done

# Write HID report descriptors using Python script
echo "Writing HID report descriptors..."
python3 "${SCRIPT_DIR}/write-hid-descriptors.py" --profile "${PROFILE}" \
    "${GADGET_DIR}/functions/hid.usb0/report_desc" \
    "${GADGET_DIR}/functions/hid.usb1/report_desc" \
    "${GADGET_DIR}/functions/hid.usb2/report_desc"

# Link functions to configuration
ln -s functions/hid.usb0 "configs/${CONFIG_NAME}/"
ln -s functions/hid.usb1 "configs/${CONFIG_NAME}/"
ln -s functions/hid.usb2 "configs/${CONFIG_NAME}/"

# Find available UDC
echo "Finding UDC (USB Device Controller)..."
UDC_DEVICE=$(ls /sys/class/udc | head -n1)

if [ -z "${UDC_DEVICE}" ]; then
    echo "Error: No UDC device found!"
    echo "Your system may not support USB gadget mode."
    exit 1
fi

echo "Using UDC: ${UDC_DEVICE}"

# Enable gadget by binding to UDC
echo "Enabling USB gadget..."
echo "${UDC_DEVICE}" > UDC

# Wait for devices to appear
sleep 1
echo "UDC bound to: $(cat UDC 2>/dev/null || echo 'NONE — check the cable and BIOS DRD')"

# Check if HID devices were created
if [ -c /dev/hidg0 ] && [ -c /dev/hidg1 ] && [ -c /dev/hidg2 ]; then
    echo "✓ USB HID gadget devices created successfully!"
    echo "  - /dev/hidg0 (Gamepad, ${PROFILE})"
    echo "  - /dev/hidg1 (Keyboard)"
    echo "  - /dev/hidg2 (Mouse)"
    ls -l /dev/hidg*
else
    echo "Warning: Not all HID devices created yet. They may take a few seconds."
fi

# Set permissions for hidg devices
echo "Setting permissions on /dev/hidg*..."
sleep 1
chmod 666 /dev/hidg* 2>/dev/null || echo "Note: Run 'sudo chmod 666 /dev/hidg*' after devices appear"

# Record the active profile so the app can detect stale gadget configs
echo "${PROFILE}" > /tmp/steamdeck_hid.profile

echo
echo "=== USB Gadget HID Setup Complete ==="
echo
echo "The Steam Deck will now appear as 3 separate USB HID devices when connected via USB-C."
echo "Write HID reports to:"
echo "  - /dev/hidg0 for gamepad input"
echo "  - /dev/hidg1 for keyboard input"
echo "  - /dev/hidg2 for mouse input"
if [ "$PROFILE" = "ds4" ]; then
    echo
    echo "Gamepad presents as a DUALSHOCK 4 (054C:05C4) — macOS Game Controllers"
    echo "should list it as a Wireless Controller / DUALSHOCK 4."
elif [ "$PROFILE" = "pro" ]; then
    echo
    echo "Gamepad presents as a Switch Pro Controller (057E:2009)."
fi
echo
echo "To disable: Run cleanup-usb-gadget.sh or:"
echo "  sudo echo \"\" > ${GADGET_DIR}/UDC"
echo

exit 0
