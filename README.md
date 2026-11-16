# Steamin Ma HoG

Emulates a Bluetooth or **Wired USB** gamepad / keyboard / trackpad peripheral that forwards controller input from a Steam Deck to other devices.

**Gamepad mode is macOS-native:** the Deck can now present itself as two of the controllers Apple's GameController framework natively supports, so games on a Mac (macOS 13+ through macOS 27 Golden Gate) see a real game controller instead of ignoring a generic HID pad:

| Transport | Gamepad identity on the Mac | Keyboard/mouse |
|-----------|-----------------------------|----------------|
| Bluetooth (BR/EDR) | **Nintendo Switch Pro Controller** ("Pro Controller") | not in this mode |
| Wired USB | **DUALSHOCK 4** (VID 0x054C / PID 0x05C4) | separate HID interfaces, always present |

![controller view](assets/controller-tab.png) ![keyboard view](assets/keyboard-tab.png) ![media view](assets/media-tab.png) ![trackpad view](assets/trackpad-tab.png)

HID-over-GATT via BlueZ D-Bus, classic Bluetooth HID (HIDP/L2CAP) for the Pro Controller gamepad, plus USB gadget (configfs) for wired connections. Built for **SteamOS 3.x** on Steam Deck; other Linux distros may work if dependencies are installed.

> **Maintained fork (SteamOS 3.7+ fixes):** [TheDordo/steamdeck-bt-controller-emulator](https://github.com/TheDordo/steamdeck-bt-controller-emulator) — includes BlueZ 5.76+ registration fixes, Steam Deck evdev detection, USB setup improvements, macOS-native gamepad identities (Pro Controller over Bluetooth, DualShock 4 over USB), and [`scripts/diagnose.sh`](scripts/diagnose.sh).
> Upstream: [xXJSONDeruloXx/steamdeck-bt-controller-emulator](https://github.com/xXJSONDeruloXx/steamdeck-bt-controller-emulator).

---

## Before you install

| Requirement | Bluetooth | Wired USB |
|-------------|-----------|-----------|
| **Mode** | Desktop Mode recommended (Konsole + app menu) | Desktop Mode |
| **User password** | Optional (for `btmgmt`) | **Required** (`passwd`) for setup |
| **BIOS** | — | **USB Dual Role Device → DRD** |
| **Cable** | — | USB-C **data** cable (not charge-only) |
| **After install** | **Log out and back in** | Same + cold boot after BIOS change |

On **SteamOS Preview / Main / Beta** (BlueZ 5.76+), use this fork and re-run the installer after updates. Bleeding-edge OS builds can still break built-in controller evdev; run diagnostics if both modes fail.

---

## Installation

Review the script before running: [install.sh](install.sh)

**One-line install / update** (Desktop Konsole):

```bash
curl -fsSL https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/install.sh | bash
```

The installer will:

- Clone or update `~/steamdeck-bt-controller-emulator`
- Add you to `input` and `bluetooth` groups
- Install D-Bus policy, sudoers (BlueZ + passwordless USB setup), and udev rules for `/dev/hidg*`
- Create Desktop and application menu shortcuts

**You must log out and log back in** (or reboot) after the first install so groups and D-Bus policy apply.

Add the desktop shortcut to Steam as a non-Steam game if you want it from Gaming Mode — still prefer **Desktop Mode** when starting Bluetooth/USB service.

### Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/uninstall.sh | bash
```

---

## Usage

Launch **BT Controller Emulator** from the app menu, Desktop shortcut, or:

```bash
cd ~/steamdeck-bt-controller-emulator
./scripts/run-gui.sh
```

Pick **Bluetooth** or **Wired USB**, then pick a **Profile**:

- **Gamepad (macOS)** — the Deck appears to the host as a natively supported game controller (Pro Controller over Bluetooth, DUALSHOCK 4 over USB). Games that use Apple's GameController framework, Steam, emulators, etc. see it as a real controller.
- **Keyboard** — the classic behaviour: BLE HOGP keyboard/gamepad/mouse (Bluetooth) or generic HID gamepad + keyboard + mouse (USB).

Switching between Gamepad and Keyboard profile requires **Stop Service → switch → Start** (and re-pair/re-plug on the host) — the identity change happens at connect time. This matches "disconnect and reconnect to switch modes".

### Gamepad over Bluetooth (macOS / Android / Linux)

1. Select **Bluetooth** + **Gamepad (macOS)** profile
2. **Start Service** — wait for **Active - Pro Controller**
3. On the Mac: **System Settings → Bluetooth** → find **Pro Controller** → **Connect**
4. Accept the **Bluetooth Pairing Request** (Just Works, no PIN)
5. The Deck UI shows **✓ Paired — streaming as Pro Controller**
6. Check **System Settings → Game Controllers** — a **Pro Controller** should be listed; test sticks/buttons in any GameController-based game, Steam, or the Controller tab visualizer first (visualizer proves evdev forwarding before the Mac is involved)
7. The Deck's real battery level is transmitted with the Pro Controller input reports (byte 2, refreshed every ~15 s) — macOS shows it next to the controller in System Settings

Keyboard over Bluetooth stays available via the **Keyboard** profile (pairs as **SteamDeckPad**, LE HOGP). You can keep both pairings on the Mac and switch by stopping/starting the service with the desired profile. In this mode the Deck battery is exposed on the standard HOGP Battery Service (0x180F), which macOS reads automatically.

CLI (no GUI): `python3 -m src.hogp --mode pro` (Pro Controller) or the default HOGP mode (see `--help`).

### Gamepad over Wired USB

1. **BIOS:** Shut down → hold **Volume +** + **Power** → Setup Utility → **Advanced** → **USB Configuration** → **USB Dual Role Device** → **DRD** → save and exit. Cold boot.
2. Connect Deck to host with a **data** USB-C cable (Deck port toward host).
3. In the app: **Wired USB** + **Gamepad (macOS)** profile → **Start Service** (first run configures the gadget; no password prompt if install completed).
4. On the Mac: **System Settings → Game Controllers** — a **Wireless Controller** (DUALSHOCK 4) should appear; games see it as a native PlayStation pad. **System Report → USB** shows the composite device (054C:05C4) with gamepad, keyboard and mouse interfaces.

The **Keyboard** profile keeps the previous generic gamepad identity for hosts that don't support DS4 descriptors.

Manual gadget setup: `sudo ~/steamdeck-bt-controller-emulator/scripts/setup-usb-gadget.sh [--profile ds4|generic]`
Teardown: `sudo ~/steamdeck-bt-controller-emulator/scripts/cleanup-usb-gadget.sh`

---

## Diagnostics

If anything fails, run this and share the output when asking for help:

```bash
~/steamdeck-bt-controller-emulator/scripts/diagnose.sh
```

Other helpers:

| Script | Purpose |
|--------|---------|
| [`scripts/diagnose.sh`](scripts/diagnose.sh) | Groups, BlueZ, evdev gamepads, USB gadget |
| [`scripts/check-usb-gadget.sh`](scripts/check-usb-gadget.sh) | USB gadget / `hidg` only |
| [`scripts/view-logs.sh`](scripts/view-logs.sh) | Tail journal while reproducing |

**Quick evdev check:** `evtest` — you should see something like **Valve Software Steam Deck Controller** or **Microsoft X-Box 360 pad** (Steam virtual pad). If the list has no gamepad, the app cannot forward sticks/buttons until SteamOS exposes input (try quitting Steam, Desktop Mode, or a stable OS channel).

---

## Troubleshooting

### Gamepad pairs as "Pro Controller" but Mac shows nothing in Game Controllers

1. Confirm the Deck UI shows **✓ Paired — streaming as Pro Controller** (both L2CAP channels connected).
2. Check `journalctl -u bluetooth` + the app log: you should see subcommand traffic (`Pro Controller subcommand 0x02 …`) — that proves macOS is running the Pro init protocol.
3. On the Mac, remove the **Pro Controller** entry, toggle Bluetooth off/on, and pair again.
4. If pairing spins forever: the Deck must be **pairable + discoverable** with Class of Device `0x002508`; the app sets this at start. Re-run install.sh if D-Bus policy blocks it.
5. macOS 13+ is required for Pro Controller support (macOS 26 Tahoe / 27 Golden Gate both include it).

### Pairs but no input (Bluetooth gamepad)

- The Mac must be the one initiating the L2CAP HID connection — if another host (Android/Windows) previously paired, forget the Deck there or it may hold the connection.
- On the Deck, quit Steam (Desktop Mode) so the built-in controller is exposed to evdev (`evtest` must show a gamepad).
- Watch the Controller tab visualizer while pressing sticks/buttons — if it doesn't move, the problem is evdev, not Bluetooth.

### USB gamepad not recognised as a DUALSHOCK 4

- **System Report → USB** must show the device as `054C:05C4`. If it shows `1209:0001`, the gadget was configured with the generic profile — run `sudo scripts/setup-usb-gadget.sh --profile ds4` manually or switch the app to the Gamepad profile and Start again.
- Some Mac games only refresh the controller list at launch — quit and reopen the game.
- The keyboard/mouse interfaces on the same composite device are normal and expected.

### `Advertisement failed` / `Failed to parse advertisement` (Keyboard profile)

Usually a bad combination of `LEAdvertisement1` properties (e.g. `Appearance` set twice). Update to the latest fork build.

### `GATT registration failed` / `Failed to create entry in database` (Keyboard profile)

BlueZ **already owns** Generic Access (0x1800) and often Device Information (0x180a). This fork registers **only the HID service (0x1812)**. Update to the latest fork build if you still see this after an older install.

### Status stuck on “Starting...” (Bluetooth)

- Re-run the [install command](#installation), then **log out / in**
- Run from **Desktop Mode**
- Run `diagnose.sh`; check BlueZ / D-Bus errors in Konsole when starting the service

### Visible in discovery but never connects / pairs

- Re-run **install.sh** and log out/in (registers an auto-accept **pairing agent**).
- On the **host**, start the pairing flow from Bluetooth settings (don't rely only on “scan”).
- If Android pairs then disconnects: on the Deck (Developer mode), try in `/etc/bluetooth/main.conf` under `[General]`: `ReverseServiceDiscovery = false`, then `sudo systemctl restart bluetooth`. This stops BlueZ from probing the phone as a GATT client during connect.

### macOS shows connected but not “Paired” (Keyboard / SteamDeckPad)

GATT notify can start **before** macOS finishes SMP bonding. Latest builds **call `Device1.Pair()` and set `Trusted`** when HID notify starts, and the UI shows **Paired / Trusted** (not only “sending data”).

1. Re-run [install.sh](#installation) and **log out / in** (D-Bus policy + pairing agent).
2. On the Mac: **System Settings → Bluetooth** — remove old **SteamDeckPad** entries, then connect again from the list (don't use only a quick-connect chip).
3. If the Mac stalls on pairing: accept any **“Bluetooth Pairing Request”** dialog (Just Works / confirm).
4. Deck UI should show **Paired — sending HID data** with **Paired: yes** and **Trusted: yes**. If **Paired** stays `no`, run `diagnose.sh` and check `journalctl -u bluetooth` while connecting.
5. If macOS shows the Deck as a **laptop** or adds a **Bluetooth audio output**: remove **every** “Steam Deck” / **SteamDeckPad** entry on the Mac, update the app, start the service (it switches the adapter to **LE-only** while running), then pair **only SteamDeckPad**. Stop the service before using Deck Bluetooth headphones again.

### Wired USB fails immediately

- BIOS **DRD** enabled, **cold boot**
- `passwd` set for user `deck`
- Re-run installer (passwordless `sudo` for setup scripts)
- `check-usb-gadget.sh` — need UDC, gadget bound, `/dev/hidg0`–`hidg2` writable

### Both modes fail on Preview / Main

Some prerelease builds do not expose built-in controls to evdev while Steam or the kernel holds them. Use `diagnose.sh` and `evtest`. Mitigations: Desktop Mode, exit Steam before starting the service, **Stable** channel, or USB mode with DRD. Kernel customization on stock SteamOS is not a practical fix for most users.

### General pairing / mapping tips

- Confirm matching PIN when prompted (often acknowledge on PC first, then Deck)
- Triggers/bumpers may need one-time mapping in the host’s controller settings
- **Guide / QAM** on the physical Deck are not forwarded; use the in-app **Home** / **QAM** buttons (keyboard shortcuts)

---

## Development

From repo root on a dev machine:

- `just deploy` — sync to Deck over SSH (edit IP in justfile)
- `just run` — run remotely
- `just logs` — tail logs

Key modules: `src/hogp/bluez.py`, `gatt_app.py`, `input_handler.py`, `usb_gadget.py`, `pro_controller.py` (BR/EDR Pro Controller emulator), `ds4.py` (DualShock 4 USB identity), `gui.py`

---

## References

- Protocol research: [dekuNukem/Nintendo_Switch_Reverse_Engineering](https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering) (BT HID reports + subcommands), [mart1nro/joycontrol](https://github.com/mart1nro/joycontrol) (Pro Controller L2CAP server + SDP record), Linux [hid-nintendo.c](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-nintendo.c) (report structs, calibration), [esp-cpp/espp](https://github.com/esp-cpp/espp) `switch_pro` (subcommand reply engine)
- DualShock 4 USB: [chrippa/ds4drv](https://github.com/chrippa/ds4drv) (report layout), [orcadom/DS4](https://github.com/orcadom/DS4) (public-domain USB report descriptor)
- HOGP: [EmuBTHID](https://github.com/Alkaid-Benetnash/EmuBTHID), [BTGamepad](https://github.com/007durgesh219/BTGamepad), [diyps3controller](https://github.com/rafikel/diyps3controller), [GIMX](https://github.com/matlo/GIMX), [DeckJoy](https://github.com/Lucaber/deckjoy) (USB gadget reference)
