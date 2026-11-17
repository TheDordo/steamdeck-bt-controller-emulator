#!/usr/bin/env python3
"""
GTK4 GUI for the BT Controller Emulator (unprivileged client).

The GUI talks only to the root daemon (com.steamdeck.btemu) over D-Bus.
It owns no BlueZ registrations, no btmgmt calls, no L2CAP sockets and no
/dev/input access — Start/Stop and every HID report go through the daemon.
"""

import logging
import os
import sys
import time

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib

# Allow running as `python3 -m src.hogp.gui` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hogp.api import (
    STATUS_STOPPED, STATUS_STARTING, STATUS_STOPPING, STATUS_ACTIVE,
    STATUS_ERROR, STATUS_DEGRADED,
    MODE_BLUETOOTH, MODE_USB, PROFILE_GAMEPAD, PROFILE_KEYBOARD,
    KEY_STATUS, KEY_MODE, KEY_PROFILE, KEY_DETAIL, KEY_ERROR, KEY_CONN,
    KEY_PAIRED, KEY_TRUSTED, KEY_INPUT, KEY_DEVICE_NAME, KEY_DEVICE_ADDR,
    KEY_READY, KEY_KB_CAPABLE, KEY_BUTTONS, KEY_AXES, KEY_TRIGGERS, KEY_HAT,
)
from hogp.client import BtEmuClient, BtEmuError

logger = logging.getLogger(__name__)

# HID Keyboard key codes
HID_KEY_CODES = {
    'a': 0x04, 'b': 0x05, 'c': 0x06, 'd': 0x07, 'e': 0x08, 'f': 0x09,
    'g': 0x0a, 'h': 0x0b, 'i': 0x0c, 'j': 0x0d, 'k': 0x0e, 'l': 0x0f,
    'm': 0x10, 'n': 0x11, 'o': 0x12, 'p': 0x13, 'q': 0x14, 'r': 0x15,
    's': 0x16, 't': 0x17, 'u': 0x18, 'v': 0x19, 'w': 0x1a, 'x': 0x1b,
    'y': 0x1c, 'z': 0x1d,
    '1': 0x1e, '2': 0x1f, '3': 0x20, '4': 0x21, '5': 0x22,
    '6': 0x23, '7': 0x24, '8': 0x25, '9': 0x26, '0': 0x27,
    'Enter': 0x28, 'Escape': 0x29, 'Backspace': 0x2a, 'Tab': 0x2b, 'Space': 0x2c,
    '-': 0x2d, '=': 0x2e, '[': 0x2f, ']': 0x30, '\\': 0x31, ';': 0x33,
    "'": 0x34, '`': 0x35, ',': 0x36, '.': 0x37, '/': 0x38,
    'F1': 0x3a, 'F2': 0x3b, 'F3': 0x3c, 'F4': 0x3d,
    'F5': 0x3e, 'F6': 0x3f, 'F7': 0x40, 'F8': 0x41,
    'F9': 0x42, 'F10': 0x43, 'F11': 0x44, 'F12': 0x45,
    'Left': 0x50, 'Right': 0x4f, 'Up': 0x52, 'Down': 0x51,
    # Media/Volume keys
    'VolUp': 0x80, 'VolDown': 0x81, 'Mute': 0x7f,
    'PlayPause': 0xcd, 'Stop': 0xb7, 'NextTrack': 0xb5, 'PrevTrack': 0xb6,
}
for _fi in range(1, 13):
    HID_KEY_CODES[f"f{_fi}"] = HID_KEY_CODES[f"F{_fi}"]

# Modifier keys (bitmask)
MOD_LCTRL = 0x01
MOD_LSHIFT = 0x02
MOD_LALT = 0x04
MOD_LGUI = 0x08

_BTN_NAMES = (
    "A", "B", "X", "Y", "LB", "RB", "Back", "Start", "Guide", "L3", "R3",
)

POLL_INTERVAL_MS = 100        # status/visualizer refresh (local D-Bus call)
START_TIMEOUT_S = 35          # surface diagnostics instead of hanging


def _kb_ready(status) -> bool:
    """Keyboard/mouse reports are accepted by the active engine."""
    return bool(status) and bool(status.get(KEY_KB_CAPABLE)) and bool(status.get(KEY_READY))


class ControllerVisualizer(Gtk.Box):
    """Live view of forwarded gamepad state + Deck-only Home/QAM shortcuts."""

    def __init__(self, send_key_cb, get_status):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self.send_key_cb = send_key_cb
        self.get_status = get_status
        self._btn_labels = []

        title = Gtk.Label()
        title.set_markup("<big><b>Physical input (evdev → HID)</b></big>")
        self.append(title)

        self._input_label = Gtk.Label(label="Input: waiting for service…")
        self._input_label.set_wrap(True)
        self._input_label.set_halign(Gtk.Align.START)
        self.append(self._input_label)

        btn_grid = Gtk.Grid(column_spacing=6, row_spacing=4)
        btn_grid.set_halign(Gtk.Align.CENTER)
        for i, name in enumerate(_BTN_NAMES):
            lbl = Gtk.Label(label=f"{name}: —")
            lbl.set_xalign(0)
            col = i % 4
            row = i // 4
            btn_grid.attach(lbl, col, row, 1, 1)
            self._btn_labels.append(lbl)
        self.append(btn_grid)

        self._axes_label = Gtk.Label(label="Sticks: —")
        self._axes_label.set_halign(Gtk.Align.START)
        self.append(self._axes_label)

        self._trig_label = Gtk.Label(label="Triggers: —")
        self._trig_label.set_halign(Gtk.Align.START)
        self.append(self._trig_label)

        self._hat_label = Gtk.Label(label="D-pad: —")
        self._hat_label.set_halign(Gtk.Align.START)
        self.append(self._hat_label)

        hint = Gtk.Label()
        hint.set_markup(
            "<small>Press sticks/buttons on the Deck. Values should "
            "change here before they reach the Mac. If nothing moves, "
            "use Desktop Mode or quit Steam and run <tt>evtest</tt>.</small>"
        )
        hint.set_wrap(True)
        hint.set_margin_top(8)
        self.append(hint)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.CENTER)
        button_box.set_margin_top(12)

        self.home_button = Gtk.Button(label="⌂ Home")
        self.home_button.set_size_request(150, 60)
        self.home_button.connect("clicked", self._send_home)
        button_box.append(self.home_button)

        self.qam_button = Gtk.Button(label="⋯ QAM")
        self.qam_button.set_size_request(150, 60)
        self.qam_button.connect("clicked", self._send_qam)
        button_box.append(self.qam_button)

        self.append(button_box)

    def set_input_source(self, text: str) -> None:
        self._input_label.set_label(f"Input: {text}")

    def _send_home(self, button=None):
        """Send Ctrl+1 for Home button."""
        if _kb_ready(self.get_status()):
            self.send_key_cb(0x1e, MOD_LCTRL, 200)
            logger.info("Home: Sent Ctrl+1 press")

    def _send_qam(self, button=None):
        """Send Ctrl+2 for QAM button."""
        if _kb_ready(self.get_status()):
            self.send_key_cb(0x1f, MOD_LCTRL, 200)
            logger.info("QAM: Sent Ctrl+2 press")

    def update_state(self, buttons, axes, triggers, hat):
        for i, lbl in enumerate(self._btn_labels):
            pressed = bool(buttons & (1 << i))
            mark = "●" if pressed else "○"
            lbl.set_markup(
                f"<b>{_BTN_NAMES[i]}</b>: <span "
                f"foreground='{'#2ecc71' if pressed else '#888888'}'>{mark}</span>"
            )
        self._axes_label.set_label(
            f"Sticks: LX={axes[0]:6d} LY={axes[1]:6d} "
            f"RX={axes[2]:6d} RY={axes[3]:6d}"
        )
        self._trig_label.set_label(f"Triggers: LT={triggers[0]:3d} RT={triggers[1]:3d}")
        hat_names = {
            0: "Up", 1: "Up-Right", 2: "Right", 3: "Down-Right",
            4: "Down", 5: "Down-Left", 6: "Left", 7: "Up-Left",
            8: "Center",
        }
        self._hat_label.set_label(f"D-pad: {hat_names.get(hat, f'0x{hat:02x}')}")


class VirtualKeyboard(Gtk.Box):
    """Virtual keyboard widget that sends HID keyboard reports."""

    def __init__(self, send_key_cb, get_status):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        self.send_key_cb = send_key_cb
        self.get_status = get_status

        # Common keys
        keys_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        # Row 0: F keys
        row0 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row0.set_homogeneous(True)
        for i in range(1, 13):
            btn = self._create_key_button(f'F{i}')
            row0.append(btn)
        keys_box.append(row0)

        # Row 1: Numbers
        row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row1.set_homogeneous(True)
        for key in ['1', '2', '3', '4', '5', '6', '7', '8', '9', '0']:
            btn = self._create_key_button(key)
            row1.append(btn)
        keys_box.append(row1)

        # Row 2: QWERTY
        row2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row2.set_homogeneous(True)
        for key in ['q', 'w', 'e', 'r', 't', 'y', 'u', 'i', 'o', 'p']:
            btn = self._create_key_button(key)
            row2.append(btn)
        keys_box.append(row2)

        # Row 3: ASDFGH
        row3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row3.set_homogeneous(True)
        for key in ['a', 's', 'd', 'f', 'g', 'h', 'j', 'k', 'l']:
            btn = self._create_key_button(key)
            row3.append(btn)
        keys_box.append(row3)

        # Row 4: ZXCVBN
        row4 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row4.set_homogeneous(True)
        for key in ['z', 'x', 'c', 'v', 'b', 'n', 'm']:
            btn = self._create_key_button(key)
            row4.append(btn)
        keys_box.append(row4)

        # Row 5: Special keys
        row5 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row5.set_homogeneous(True)
        for key in ['Space', 'Enter', 'Backspace', 'Tab', 'Escape']:
            btn = self._create_key_button(key)
            row5.append(btn)
        keys_box.append(row5)

        # Row 6: Arrow keys
        row6 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row6.set_homogeneous(True)
        for key in ['Left', 'Up', 'Down', 'Right']:
            btn = self._create_key_button(key)
            row6.append(btn)
        keys_box.append(row6)

        self.append(keys_box)

        # Shortcut keys section
        shortcuts_label = Gtk.Label(label="Shortcuts")
        shortcuts_label.set_margin_top(10)
        shortcuts_label.set_markup("<b>Shortcuts</b>")
        self.append(shortcuts_label)

        # First row: Copy, Paste, Cut, Select All, Super
        shortcuts_row1 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        shortcuts_row1.set_halign(Gtk.Align.CENTER)

        shortcuts1 = [
            ('Copy', 'c', MOD_LCTRL),
            ('Paste', 'v', MOD_LCTRL),
            ('Cut', 'x', MOD_LCTRL),
            ('Sel All', 'a', MOD_LCTRL),
            ('Super', None, MOD_LGUI),
        ]

        for label, key, modifier in shortcuts1:
            btn = Gtk.Button(label=label)
            if key:
                btn.connect("clicked", lambda b, k=key, m=modifier: self._send_shortcut(k, m))
            else:
                # Super key - just press modifier
                btn.connect("clicked", lambda b, m=modifier: self._send_modifier_only(m))
            shortcuts_row1.append(btn)

        self.append(shortcuts_row1)

    def _create_key_button(self, key):
        """Create a button for a keyboard key."""
        label = key.upper() if len(key) == 1 else key
        btn = Gtk.Button(label=label)
        btn.connect("clicked", lambda b: self._send_key(key))
        return btn

    def _send_key(self, key):
        """Send a single key press."""
        if not _kb_ready(self.get_status()):
            return
        key_code = HID_KEY_CODES.get(key) or HID_KEY_CODES.get(key.lower())
        if key_code:
            self.send_key_cb(key_code, 0, 140)
        else:
            logger.warning(f"No HID code for key {key!r}")

    def _send_shortcut(self, key, modifier):
        """Send a keyboard shortcut (key + modifier)."""
        if not _kb_ready(self.get_status()):
            return
        key_code = HID_KEY_CODES.get(key) or HID_KEY_CODES.get(key.lower())
        if key_code:
            self.send_key_cb(key_code, modifier, 200)

    def _send_modifier_only(self, modifier):
        """Send just a modifier key press (like Super key)."""
        if not _kb_ready(self.get_status()):
            return
        self.send_key_cb(0, modifier, 120)


class VirtualMediaControls(Gtk.Box):
    """Virtual media controls widget that sends HID keyboard media keys."""

    def __init__(self, send_key_cb, get_status):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_top(50)
        self.set_margin_bottom(50)
        self.set_margin_start(50)
        self.set_margin_end(50)

        self.send_key_cb = send_key_cb
        self.get_status = get_status

        # Title
        title_label = Gtk.Label()
        title_label.set_markup("<big><b>Media Controls</b></big>")
        self.append(title_label)

        # Volume controls
        volume_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        volume_box.set_halign(Gtk.Align.CENTER)
        volume_box.set_margin_top(20)

        vol_down_btn = Gtk.Button(label="🔉 Vol Down")
        vol_down_btn.set_size_request(150, 60)
        vol_down_btn.connect("clicked", lambda b: self._send_media_key('VolDown'))
        volume_box.append(vol_down_btn)

        mute_btn = Gtk.Button(label="🔇 Mute")
        mute_btn.set_size_request(150, 60)
        mute_btn.connect("clicked", lambda b: self._send_media_key('Mute'))
        volume_box.append(mute_btn)

        vol_up_btn = Gtk.Button(label="🔊 Vol Up")
        vol_up_btn.set_size_request(150, 60)
        vol_up_btn.connect("clicked", lambda b: self._send_media_key('VolUp'))
        volume_box.append(vol_up_btn)

        self.append(volume_box)

        # Playback controls
        playback_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        playback_box.set_halign(Gtk.Align.CENTER)
        playback_box.set_margin_top(20)

        prev_btn = Gtk.Button(label="⏮ Previous")
        prev_btn.set_size_request(150, 60)
        prev_btn.connect("clicked", lambda b: self._send_media_key('PrevTrack'))
        playback_box.append(prev_btn)

        play_pause_btn = Gtk.Button(label="⏯ Play/Pause")
        play_pause_btn.set_size_request(150, 60)
        play_pause_btn.connect("clicked", lambda b: self._send_media_key('PlayPause'))
        playback_box.append(play_pause_btn)

        stop_btn = Gtk.Button(label="⏹ Stop")
        stop_btn.set_size_request(150, 60)
        stop_btn.connect("clicked", lambda b: self._send_media_key('Stop'))
        playback_box.append(stop_btn)

        next_btn = Gtk.Button(label="⏭ Next")
        next_btn.set_size_request(150, 60)
        next_btn.connect("clicked", lambda b: self._send_media_key('NextTrack'))
        playback_box.append(next_btn)

        self.append(playback_box)

    def _send_media_key(self, key):
        """Send a media key press."""
        if not _kb_ready(self.get_status()):
            return
        key_code = HID_KEY_CODES.get(key)
        if key_code:
            self.send_key_cb(key_code, 0, 180)


class VirtualTrackpad(Gtk.Box):
    """Virtual trackpad widget that sends HID mouse reports."""

    def __init__(self, send_mouse_cb, get_status):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        self.send_mouse_cb = send_mouse_cb
        self.get_status = get_status

        # Info label
        info = Gtk.Label(label="Drag to move cursor • Tap to click • Double-tap & hold to drag • Scroll on right side")
        self.append(info)

        # Main horizontal box for trackpad + scroll wheel
        trackpad_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Trackpad area (left side)
        self.trackpad = Gtk.DrawingArea()
        self.trackpad.set_size_request(500, 300)
        self.trackpad.set_hexpand(True)
        self.trackpad.set_vexpand(True)
        self.trackpad.set_draw_func(self._draw_trackpad, None)

        # Add drag controller for mouse movement
        drag_controller = Gtk.GestureDrag()
        drag_controller.connect("drag-update", self._on_drag_update)
        drag_controller.connect("drag-end", self._on_drag_end)
        self.trackpad.add_controller(drag_controller)

        # Add click controller for tap-to-click and double-tap detection
        click_controller = Gtk.GestureClick()
        click_controller.connect("released", self._on_tap_click)
        click_controller.connect("pressed", self._on_tap_press)
        self.trackpad.add_controller(click_controller)

        # Track last position for delta calculation
        self._last_x = 0
        self._last_y = 0
        self._is_dragging = False
        self._drag_button_held = False  # Track if we're in drag mode (button held)
        self._last_tap_time = 0
        self._tap_count = 0

        trackpad_frame = Gtk.Frame()
        trackpad_frame.set_child(self.trackpad)
        trackpad_container.append(trackpad_frame)

        # Scroll wheel area (right side)
        self.scroll_area = Gtk.DrawingArea()
        self.scroll_area.set_size_request(80, 300)
        self.scroll_area.set_vexpand(True)
        self.scroll_area.set_draw_func(self._draw_scroll_area, None)

        # Add drag controller for scroll wheel
        scroll_drag_controller = Gtk.GestureDrag()
        scroll_drag_controller.connect("drag-update", self._on_scroll_drag)
        scroll_drag_controller.connect("drag-end", self._on_scroll_end)
        self.scroll_area.add_controller(scroll_drag_controller)

        self._scroll_last_y = 0
        self._is_scrolling = False

        scroll_frame = Gtk.Frame()
        scroll_frame.set_child(self.scroll_area)
        trackpad_container.append(scroll_frame)

        self.append(trackpad_container)

        # Mouse buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.CENTER)

        left_btn = Gtk.Button(label="Left Click")
        left_btn.connect("clicked", lambda b: self._send_click(0x01))
        button_box.append(left_btn)

        right_btn = Gtk.Button(label="Right Click")
        right_btn.connect("clicked", lambda b: self._send_click(0x02))
        button_box.append(right_btn)

        middle_btn = Gtk.Button(label="Middle Click")
        middle_btn.connect("clicked", lambda b: self._send_click(0x04))
        button_box.append(middle_btn)

        self.append(button_box)

        # Sensitivity slider
        sens_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sens_label = Gtk.Label(label="Sensitivity:")
        sens_box.append(sens_label)

        self.sensitivity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.1, 5.0, 0.1)
        self.sensitivity.set_value(1.0)
        self.sensitivity.set_hexpand(True)
        sens_box.append(self.sensitivity)

        self.append(sens_box)

    def _draw_trackpad(self, area, cr, width, height, user_data):
        """Draw the trackpad area."""
        # Background
        cr.set_source_rgb(0.2, 0.2, 0.25)
        cr.paint()

        # Border
        cr.set_source_rgb(0.4, 0.4, 0.5)
        cr.set_line_width(2)
        cr.rectangle(5, 5, width - 10, height - 10)
        cr.stroke()

        # Instructions
        cr.set_source_rgb(0.6, 0.6, 0.7)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(14)
        text = "Drag here to move mouse"
        extents = cr.text_extents(text)
        cr.move_to(width/2 - extents.width/2, height/2)
        cr.show_text(text)

    def _on_drag_update(self, gesture, offset_x, offset_y):
        """Handle drag motion."""
        if not self._is_dragging:
            self._is_dragging = True
            start_point = gesture.get_start_point()
            self._last_x = start_point.x
            self._last_y = start_point.y
            return

        # Get current position
        start_point = gesture.get_start_point()
        current_x = start_point.x + offset_x
        current_y = start_point.y + offset_y

        # Calculate delta
        dx = int((current_x - self._last_x) * self.sensitivity.get_value())
        dy = int((current_y - self._last_y) * self.sensitivity.get_value())

        # Update last position
        self._last_x = current_x
        self._last_y = current_y

        # Send mouse movement
        if dx != 0 or dy != 0:
            if _kb_ready(self.get_status()):
                # If in drag mode, keep button pressed while moving
                button_state = 0x01 if self._drag_button_held else 0
                self.send_mouse_cb(dx, dy, button_state, 0)

    def _on_drag_end(self, gesture, offset_x, offset_y):
        """Handle drag end."""
        self._is_dragging = False
        # If we were in drag mode, release the button
        if self._drag_button_held:
            self._drag_button_held = False
            if _kb_ready(self.get_status()):
                self.send_mouse_cb(0, 0, 0, 0)

    def _on_tap_press(self, gesture, n_press, x, y):
        """Handle tap press - detect double-tap and hold."""
        current_time = time.time()

        # Check if this is a double-tap (within 500ms)
        if n_press == 2 or (current_time - self._last_tap_time < 0.5 and self._tap_count == 1):
            # This is the second tap - enter drag mode
            self._drag_button_held = True
            self._tap_count = 0
            # Press and hold the left button
            if _kb_ready(self.get_status()):
                self.send_mouse_cb(0, 0, 0x01, 0)
        else:
            self._tap_count = 1

        self._last_tap_time = current_time

    def _on_tap_click(self, gesture, n_press, x, y):
        """Handle tap to click (left click)."""
        # Only trigger single click if we didn't drag and not in drag mode
        if not self._is_dragging and not self._drag_button_held and n_press == 1:
            self._send_click(0x01)  # Left click

    def _draw_scroll_area(self, area, cr, width, height, user_data):
        """Draw the scroll wheel area."""
        # Background
        cr.set_source_rgb(0.15, 0.15, 0.20)
        cr.paint()

        # Border
        cr.set_source_rgb(0.4, 0.4, 0.5)
        cr.set_line_width(2)
        cr.rectangle(2, 5, width - 4, height - 10)
        cr.stroke()

        # Up arrow
        cr.set_source_rgb(0.6, 0.6, 0.7)
        cr.move_to(width/2, 30)
        cr.line_to(width/2 - 10, 45)
        cr.line_to(width/2 + 10, 45)
        cr.close_path()
        cr.fill()

        # Down arrow
        cr.move_to(width/2, height - 30)
        cr.line_to(width/2 - 10, height - 45)
        cr.line_to(width/2 + 10, height - 45)
        cr.close_path()
        cr.fill()

        # Text
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(11)
        text = "Scroll"
        extents = cr.text_extents(text)
        cr.move_to(width/2 - extents.width/2, height/2 + extents.height/2)
        cr.show_text(text)

    def _on_scroll_drag(self, gesture, offset_x, offset_y):
        """Handle scroll wheel drag."""
        if not self._is_scrolling:
            self._is_scrolling = True
            start_point = gesture.get_start_point()
            self._scroll_last_y = start_point.y
            return

        # Get current position
        start_point = gesture.get_start_point()
        current_y = start_point.y + offset_y

        # Calculate scroll delta
        dy = current_y - self._scroll_last_y

        # Send scroll if moved enough (every 10 pixels)
        if abs(dy) >= 10:
            scroll_amount = int(dy / 10)
            if _kb_ready(self.get_status()):
                # Negative scroll for down, positive for up
                self.send_mouse_cb(0, 0, 0, -scroll_amount)
            self._scroll_last_y = current_y

    def _on_scroll_end(self, gesture, offset_x, offset_y):
        """Handle scroll drag end."""
        self._is_scrolling = False

    def _send_click(self, button_mask):
        """Send a mouse button click."""
        if not _kb_ready(self.get_status()):
            return

        # Press
        self.send_mouse_cb(0, 0, button_mask, 0)
        # Release after delay
        GLib.timeout_add(50, lambda: self.send_mouse_cb(0, 0, 0, 0) if _kb_ready(self.get_status()) else None)


class HoGPeripheralGUI(Gtk.ApplicationWindow):
    """Main GUI window — thin client of the emulator daemon."""

    def __init__(self, app):
        super().__init__(application=app, title="BT Controller Emulator")
        # Steam Deck optimized size (1280x800 native resolution, use ~80% for window)
        self.set_default_size(1024, 650)
        # Allow window resizing
        self.set_resizable(True)

        # Daemon client + cached status
        self.client = BtEmuClient()
        self._status = None
        self._poll_id = None
        self._start_deadline = 0.0
        self._local_error = ""

        # Build UI
        self._build_ui()

        # Status polling (also drives the visualizer)
        self._poll_id = GLib.timeout_add(POLL_INTERVAL_MS, self._poll_status)

    def _build_ui(self):
        """Build the user interface."""
        # Main box
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.set_margin_top(6)
        main_box.set_margin_bottom(6)
        main_box.set_margin_start(6)
        main_box.set_margin_end(6)

        # Header bar
        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label="BT Controller Emulator"))
        self.set_titlebar(header)

        # Mode selection
        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        mode_box.set_halign(Gtk.Align.CENTER)

        mode_label = Gtk.Label(label="Mode:")
        mode_box.append(mode_label)

        self.bluetooth_radio = Gtk.CheckButton(label="Bluetooth")
        self.bluetooth_radio.set_active(True)
        self.bluetooth_radio.connect("toggled", self._on_bluetooth_toggled)
        mode_box.append(self.bluetooth_radio)

        self.wired_radio = Gtk.CheckButton(label="Wired USB")
        self.wired_radio.set_group(self.bluetooth_radio)
        self.wired_radio.connect("toggled", self._on_wired_toggled)
        mode_box.append(self.wired_radio)

        main_box.append(mode_box)

        # Device profile (gamepad identity vs keyboard-only)
        profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        profile_box.set_halign(Gtk.Align.CENTER)
        profile_box.append(Gtk.Label(label="Profile:"))
        self.gamepad_radio = Gtk.CheckButton(label="Gamepad (macOS)")
        self.gamepad_radio.set_active(True)
        self.gamepad_radio.connect("toggled", self._on_gamepad_toggled)
        profile_box.append(self.gamepad_radio)
        self.keyboard_radio = Gtk.CheckButton(label="Keyboard")
        self.keyboard_radio.set_group(self.gamepad_radio)
        self.keyboard_radio.connect("toggled", self._on_keyboard_toggled)
        profile_box.append(self.keyboard_radio)
        main_box.append(profile_box)

        # Status box
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)

        self.status_label = Gtk.Label(label="Status: Stopped")
        self.status_label.set_markup("<big><b>Status: Stopped</b></big>")
        status_box.append(self.status_label)

        self.connection_label = Gtk.Label(label="Not connected")
        status_box.append(self.connection_label)

        self.device_info_label = Gtk.Label(label="")
        self.device_info_label.set_wrap(True)
        status_box.append(self.device_info_label)

        main_box.append(status_box)

        # Tabbed interface
        notebook = Gtk.Notebook()
        notebook.set_vexpand(True)

        # Controller tab
        self.visualizer = ControllerVisualizer(self._send_key_via_daemon, self._get_cached_status)
        notebook.append_page(self.visualizer, Gtk.Label(label="Controller"))

        # Keyboard tab
        self.keyboard = VirtualKeyboard(self._send_key_via_daemon, self._get_cached_status)
        notebook.append_page(self.keyboard, Gtk.Label(label="Keyboard"))

        # Media controls tab
        self.media = VirtualMediaControls(self._send_key_via_daemon, self._get_cached_status)
        notebook.append_page(self.media, Gtk.Label(label="Media"))

        # Trackpad tab
        self.trackpad = VirtualTrackpad(self._send_mouse_via_daemon, self._get_cached_status)
        notebook.append_page(self.trackpad, Gtk.Label(label="Trackpad"))

        main_box.append(notebook)

        # Control buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        button_box.set_halign(Gtk.Align.CENTER)

        self.start_button = Gtk.Button(label="Start Service")
        self.start_button.connect("clicked", self._on_start_clicked)
        button_box.append(self.start_button)

        self.stop_button = Gtk.Button(label="Stop Service")
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", self._on_stop_clicked)
        button_box.append(self.stop_button)

        main_box.append(button_box)

        self.set_child(main_box)

    # -------------------------------------------------------- mode/profile

    def _on_bluetooth_toggled(self, button):
        if button.get_active() and self._radios_enabled():
            logger.info("Mode: Bluetooth")

    def _on_wired_toggled(self, button):
        if button.get_active() and self._radios_enabled():
            logger.info("Mode: Wired USB")

    def _on_gamepad_toggled(self, button):
        if button.get_active() and self._radios_enabled():
            logger.info("Profile: Gamepad")

    def _on_keyboard_toggled(self, button):
        if button.get_active() and self._radios_enabled():
            logger.info("Profile: Keyboard")

    def _selected_mode(self) -> str:
        return MODE_BLUETOOTH if self.bluetooth_radio.get_active() else MODE_USB

    def _selected_profile(self) -> str:
        return PROFILE_GAMEPAD if self.gamepad_radio.get_active() else PROFILE_KEYBOARD

    # ------------------------------------------------------------ callbacks

    def _get_cached_status(self):
        return self._status

    def _send_key_via_daemon(self, code, modifiers, hold_ms):
        try:
            self.client.send_key(code, modifiers, hold_ms)
        except BtEmuError as e:
            logger.warning("send_key: %s", e)

    def _send_mouse_via_daemon(self, dx, dy, buttons, wheel):
        try:
            self.client.send_mouse(dx, dy, buttons, wheel)
        except BtEmuError as e:
            logger.warning("send_mouse: %s", e)

    def _on_start_clicked(self, button):
        """Ask the daemon to start a session; the poll loop renders progress."""
        self.start_button.set_sensitive(False)
        self._set_controls_locked(True)
        self._local_error = ""
        self._start_deadline = time.monotonic() + START_TIMEOUT_S
        self.status_label.set_markup("<big><b>Status: Starting...</b></big>")
        self.connection_label.set_label("Waiting for connection...")
        self.device_info_label.set_label("")
        try:
            self.client.start(self._selected_mode(), self._selected_profile())
        except BtEmuError as e:
            self._local_error = str(e)
            self._set_controls_locked(False)
            self.status_label.set_markup(
                f"<big><b>Error: {GLib.markup_escape_text(self._local_error)}</b></big>"
            )

    def _on_stop_clicked(self, button):
        """Ask the daemon to stop the session."""
        self.stop_button.set_sensitive(False)
        try:
            self.client.stop()
        except BtEmuError as e:
            logger.warning("stop: %s", e)
            self.stop_button.set_sensitive(True)

    # ------------------------------------------------------------- polling

    def _poll_status(self) -> bool:
        try:
            status = self.client.get_status()
        except BtEmuError as e:
            status = None
            self._local_error = str(e)
        self._render_status(status)
        return True

    def _render_status(self, status) -> None:
        self._status = status

        if status is None:
            # Daemon is down.
            self.status_label.set_markup(
                "<big><b>Error: emulator daemon not reachable</b></big>"
            )
            self.connection_label.set_label(
                "Run: sudo systemctl start bt-controller-emulator"
            )
            self.device_info_label.set_label("")
            self._set_controls_locked(False)
            self.start_button.set_sensitive(True)
            self.stop_button.set_sensitive(False)
            return

        st = status.get(KEY_STATUS)
        detail = status.get(KEY_DETAIL, "")
        error = status.get(KEY_ERROR, "")

        if st == STATUS_ERROR:
            self._set_controls_locked(False)
            self.status_label.set_markup(
                f"<big><b>Error: {GLib.markup_escape_text(detail or error)}</b></big>"
            )
        else:
            self.status_label.set_markup(
                f"<big><b>Status: {GLib.markup_escape_text(detail)}</b></big>"
            )

        self.connection_label.set_label(str(status.get(KEY_CONN, "Not connected")))

        # Device / bonding info or mode hints
        device_name = status.get(KEY_DEVICE_NAME, "")
        if device_name:
            paired_line = "yes" if status.get(KEY_PAIRED) else "no"
            trusted_line = "yes" if status.get(KEY_TRUSTED) else "no"
            self.device_info_label.set_markup(
                f"<b>Device:</b> {GLib.markup_escape_text(device_name)}\n"
                f"<b>Address:</b> {GLib.markup_escape_text(str(status.get(KEY_DEVICE_ADDR, '')))}\n"
                f"<b>Paired:</b> {paired_line}  <b>Trusted:</b> {trusted_line}"
            )
        elif st == STATUS_ACTIVE:
            mode = status.get(KEY_MODE)
            profile = status.get(KEY_PROFILE)
            if mode == MODE_BLUETOOTH and profile == PROFILE_GAMEPAD:
                self.device_info_label.set_label(
                    "macOS: System Settings → Bluetooth → Pro Controller → Connect, "
                    "accept the pairing request. Game Controllers will then list a "
                    "Pro Controller."
                )
            elif mode == MODE_USB and profile == PROFILE_GAMEPAD:
                self.device_info_label.set_label(
                    "USB gamepad presents as a DUALSHOCK 4 (054C:05C4). On the Mac "
                    "check System Settings → Game Controllers — a Wireless Controller "
                    "should be listed. HID: /dev/hidg0 gamepad, /dev/hidg1 keyboard, "
                    "/dev/hidg2 mouse on Deck."
                )
            elif mode == MODE_USB:
                self.device_info_label.set_label(
                    "Mac: Game Controllers tracks Bluetooth only — SteamDeckPad may "
                    "show Disconnected here. USB gamepad is separate: System Settings "
                    "→ General → About → System Report → USB. HID: /dev/hidg0 gamepad, "
                    "/dev/hidg1 keyboard, /dev/hidg2 mouse on Deck."
                )
            else:
                self.device_info_label.set_label("")
        else:
            self.device_info_label.set_label("")

        # Start watchdog: surface diagnostics instead of hanging.
        if st == STATUS_STARTING and self._start_deadline and time.monotonic() > self._start_deadline:
            self.status_label.set_markup(
                "<big><b>Status: Starting timed out — check "
                "'journalctl -u bt-controller-emulator -f'</b></big>"
            )
            self.connection_label.set_label(
                "Check Konsole/journal for the stalled step (bt-controller-emulator: ...)"
            )
            self._set_controls_locked(False)
            self._start_deadline = 0.0
        elif st == STATUS_STOPPED:
            self._start_deadline = 0.0
            self._set_controls_locked(False)
        elif st == STATUS_ACTIVE or st == STATUS_DEGRADED:
            self._start_deadline = 0.0

        # Buttons
        busy = st in (STATUS_STARTING, STATUS_STOPPING)
        running = st in (STATUS_ACTIVE, STATUS_DEGRADED)
        self.start_button.set_sensitive(st in (STATUS_STOPPED, STATUS_ERROR))
        self.stop_button.set_sensitive(running or busy)

        # Visualizer
        if status.get(KEY_BUTTONS) is not None:
            self.visualizer.update_state(
                status.get(KEY_BUTTONS, 0),
                status.get(KEY_AXES, [0, 0, 0, 0]),
                status.get(KEY_TRIGGERS, [0, 0]),
                status.get(KEY_HAT, 8),
            )
        input_label = status.get(KEY_INPUT, "")
        self.visualizer.set_input_source(input_label or "waiting for service…")

    def _radios_enabled(self) -> bool:
        return self.bluetooth_radio.get_sensitive()

    def _set_controls_locked(self, locked: bool) -> None:
        for radio in (self.bluetooth_radio, self.wired_radio,
                      self.gamepad_radio, self.keyboard_radio):
            radio.set_sensitive(not locked)


class HoGApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.steamdeck.hogp.gui")

    def do_activate(self):
        win = HoGPeripheralGUI(self)
        win.present()


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    HoGApp().run(None)


if __name__ == "__main__":
    main()
