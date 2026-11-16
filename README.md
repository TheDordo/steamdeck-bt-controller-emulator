# Steamin Ma HoG

Emulates a Bluetooth LE or **Wired USB** gamepad peripheral that forwards controller input from a Steam Deck to other devices. Not only that, but you also get full keyboard, trackpad, and media controls tabs.

![controller view](assets/controller-tab.png) ![keyboard view](assets/keyboard-tab.png) ![media view](assets/media-tab.png) ![trackpad view](assets/trackpad-tab.png)

HID-over-GATT via BlueZ D-Bus, plus USB gadget (configfs) for wired connections. Built for **SteamOS 3.x** on Steam Deck; other Linux distros may work if dependencies are installed.

> **Maintained fork (SteamOS 3.7+ fixes):** [TheDordo/steamdeck-bt-controller-emulator](https://github.com/TheDordo/steamdeck-bt-controller-emulator) — includes BlueZ 5.76+ registration fixes, Steam Deck evdev detection, USB setup improvements, and [`scripts/diagnose.sh`](scripts/diagnose.sh).  
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

### Bluetooth (wireless)

1. Select **Bluetooth**
2. **Start Service** — wait for **Active - Discoverable** (not stuck on *Starting...*)
3. On the host (PC, TV, etc.), open Bluetooth settings
4. Pair **SteamDeckPad** (or similar), confirm PIN on both sides
5. Host should see a gamepad, keyboard, and mouse

CLI (no GUI): `python3 -m src.hogp` from the install directory (see `--help`).

### Wired USB

1. **BIOS:** Shut down → hold **Volume +** + **Power** → Setup Utility → **Advanced** → **USB Configuration** → **USB Dual Role Device** → **DRD** → save and exit. Cold boot.
2. Connect Deck to host with a **data** USB-C cable (Deck port toward host).
3. In the app: **Wired USB** → **Start Service** (first run configures the gadget; no password prompt if install completed).
4. Host should enumerate USB HID (gamepad, keyboard, mouse).

Manual gadget setup: `sudo ~/steamdeck-bt-controller-emulator/scripts/setup-usb-gadget.sh`  
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

### `Advertisement failed` / `Failed to parse advertisement`

Usually a bad combination of `LEAdvertisement1` properties (e.g. `Appearance` set twice). Update to the latest fork build.

### `GATT registration failed` / `Failed to create entry in database`

BlueZ **already owns** Generic Access (0x1800) and often Device Information (0x180a). This fork registers **only the HID service (0x1812)**. Update to the latest fork build if you still see this after an older install.

### Status stuck on “Starting...” (Bluetooth)

- Re-run the [install command](#installation), then **log out / in**
- Run from **Desktop Mode**
- Run `diagnose.sh`; check BlueZ / D-Bus errors in Konsole when starting the service

### Visible in discovery but never connects / pairs

- Re-run **install.sh** and log out/in (registers an auto-accept **pairing agent**).
- On the **host**, start the pairing flow from Bluetooth settings (don’t rely only on “scan”).
- If Android pairs then disconnects: on the Deck (Developer mode), try in `/etc/bluetooth/main.conf` under `[General]`: `ReverseServiceDiscovery = false`, then `sudo systemctl restart bluetooth`. This stops BlueZ from probing the phone as a GATT client during connect.

### macOS shows connected but not “Paired” / no game controller

GATT notify can start **before** macOS finishes SMP bonding. Latest builds **call `Device1.Pair()` and set `Trusted`** when HID notify starts, and the UI shows **Paired / Trusted** (not only “sending data”).

1. Re-run [install.sh](#installation) and **log out / in** (D-Bus policy + pairing agent).
2. On the Mac: **System Settings → Bluetooth** — remove old **SteamDeckPad** entries, then connect again from the list (don’t use only a quick-connect chip).
3. If the Mac stalls on pairing: accept any **“Bluetooth Pairing Request”** dialog (Just Works / confirm).
4. Deck UI should show **Paired — sending HID data** with **Paired: yes** and **Trusted: yes**. If **Paired** stays `no`, run `diagnose.sh` and check `journalctl -u bluetooth` while connecting.
5. For controller input in games, open **System Settings → Game Controllers** (or Steam) and confirm **SteamDeckPad** appears; map buttons once if needed.

### Pairs but no input (Bluetooth)

- Forget device on both sides and pair again
- Reboot the Deck after first install
- Confirm `evtest` shows a gamepad node (see [Diagnostics](#diagnostics))
- On the host (e.g. Steam), map the new controller once

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

Key modules: `src/hogp/bluez.py`, `gatt_app.py`, `input_handler.py`, `usb_gadget.py`, `gui.py`

---

## References

[EmuBTHID](https://github.com/Alkaid-Benetnash/EmuBTHID), [BTGamepad](https://github.com/007durgesh219/BTGamepad), [diyps3controller](https://github.com/rafikel/diyps3controller), [GIMX](https://github.com/matlo/GIMX), [DeckJoy](https://github.com/Lucaber/deckjoy) (USB gadget reference)