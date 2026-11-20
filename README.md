# Steam Deck BT Controller Emulator

Emulates a Bluetooth or **wired USB** gamepad / keyboard / trackpad peripheral that forwards controller input from a Steam Deck to other devices — built for the official **SteamOS 3.x ("holo") image** on a real Steam Deck.

**Gamepad mode is macOS-native:** the Deck presents itself as one of the controllers Apple's GameController framework natively supports, so games on a Mac (macOS 13+ through macOS 27 Golden Gate) see a real game controller instead of ignoring a generic HID pad:

| Transport | Gamepad identity on the Mac | Keyboard/mouse |
|-----------|-----------------------------|----------------|
| Bluetooth (LE) — **recommended** | **Xbox Wireless Controller** (Series X\|S, 045E:0B13) with rumble on the Deck | not in this mode |
| Bluetooth (BR/EDR) | **Nintendo Switch Pro Controller** ("Pro Controller") | not in this mode |
| Bluetooth (LE) | — | HID-over-GATT gamepad/keyboard/mouse ("SteamDeckPad") |
| Wired USB | **DUALSHOCK 4** (VID 0x054C / PID 0x05C4) | separate HID interfaces, always present |

![controller view](assets/controller-tab.png) ![keyboard view](assets/keyboard-tab.png) ![media view](assets/media-tab.png) ![trackpad view](assets/trackpad-tab.png)

## How it works — Bluetooth just works

The old architecture ran everything from the GUI and needed runtime `sudo` (btmgmt), sudoers rules, group memberships, a setcap'd Python copy and a separate root bridge process for the HID L2CAP ports. Every one of those was a "Start Service failed" waiting to happen.

This fork rewrites the repo around a **single root systemd daemon**:

```
┌───────────────────────────── Steam Deck (SteamOS holo) ─────────────────────────────┐
│                                                                                     │
│   bt-controller-emulator.service (root)                    GUI (GTK4, unprivileged) │
│   ├─ BlueZ: GATT app + advertisement + agent + SDP        └─ talks D-Bus only ──┐   │
│   ├─ btmgmt adapter config (no sudo)                                             │   │
│   ├─ HID L2CAP PSMs 0x11/0x13 bound in-process (no bridge)                       │   │
│   ├─ evdev input (no group memberships)                com.steamdeck.btemu ◄────┘   │
│   └─ USB gadget setup + /dev/hidg* (no sudoers)           (D-Bus system bus)        │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

- **Zero runtime sudo.** Sudo is needed once during `install.sh` and never again — no passwordless rules, no `bluetooth`/`input` group juggling, no logout required.
- **One instance by construction.** systemd owns the daemon; duplicate sessions and orphaned L2CAP bridges cannot exist.
- **Self-healing.** The daemon watches `org.bluez`: if bluetoothd restarts (OS updates, crashes), it degrades gracefully and re-registers the GATT app, advertisement, agent and SDP record automatically. A 5 s health watchdog re-registers lost advertisements and re-binds the Pro Controller listeners.
- **Clean start errors.** Every failure stage reports the exact D-Bus/BlueZ error through `GetStatus()` — the GUI shows it instead of a generic timeout.
- **Adapter snapshot/restore.** The daemon saves the adapter's name/class/settings before a session and restores them on Stop (or daemon shutdown), so Deck Bluetooth audio etc. keeps working normally afterwards.

The unprivileged GUI and CLI talk to the daemon over the system bus: `com.steamdeck.btemu` → `/com/steamdeck/btemu` (`com.steamdeck.btemu.Manager1`, see [`src/hogp/api.py`](src/hogp/api.py)).

> **Upstream:** [xXJSONDeruloXx/steamdeck-bt-controller-emulator](https://github.com/xXJSONDeruloXx/steamdeck-bt-controller-emulator). This fork targets the official SteamOS 3.x holo image with the daemon/client rewrite described above.

---

## Before you install

| Requirement | Bluetooth | Wired USB |
|-------------|-----------|-----------|
| **Mode** | Desktop Mode (Konsole + app menu) | Desktop Mode |
| **User password** | Required once (`passwd`) for install.sh sudo | Same + BIOS **USB Dual Role Device → DRD**, then cold boot |
| **Cable** | — | USB-C **data** cable (not charge-only) |
| **After install** | Nothing — the daemon is already running | Re-plug the cable when starting |

On **SteamOS Preview / Main / Beta**, re-run the installer after OS updates. Bleeding-edge builds can still break built-in controller evdev; run diagnostics if input doesn't move.

---

## Installation

Review the script before running: [install.sh](install.sh)

**One-line install / update** (Desktop Konsole):

```bash
curl -fsSL https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/install.sh | bash
```

The installer will:

- Clone or update `~/steamdeck-bt-controller-emulator`
- Install the D-Bus policy and the **systemd daemon** (`bt-controller-emulator.service`), then enable + start it
- Install the udev rule for `/dev/hidg*`
- Create Desktop and application menu shortcuts

**No logout needed** — the daemon starts immediately.

### Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/TheDordo/steamdeck-bt-controller-emulator/main/uninstall.sh | bash
```

Stops the daemon (restoring the Bluetooth adapter), removes the unit, D-Bus policy, udev rule and shortcuts, and clears the static BLE address.

---

## Usage

Launch **BT Controller Emulator** from the app menu, Desktop shortcut, or:

```bash
cd ~/steamdeck-bt-controller-emulator
./scripts/run-gui.sh
```

Pick **Bluetooth** or **Wired USB**, then pick a **Profile**:

- **Xbox (BT)** — Bluetooth LE Xbox Wireless Controller. Works with macOS Game Controllers and Steam on the Mac; host rumble plays on the Deck's haptics. Bluetooth only.
- **Pro / DS4** — Pro Controller over Bluetooth, DUALSHOCK 4 over USB.
- **Keyboard** — BLE HOGP gamepad/keyboard/mouse (Bluetooth) or generic HID gamepad + keyboard + mouse (USB).

Switching profiles requires **Stop Service → switch → Start** (and re-pair/re-plug on the host) — the identity changes at connect time.

CLI (no GUI):

```bash
python3 -m src.hogp start --mode bluetooth --profile xbox      # Xbox Wireless Controller (BLE)
python3 -m src.hogp start --mode bluetooth --profile gamepad   # Pro Controller
python3 -m src.hogp start --mode bluetooth --profile keyboard  # SteamDeckPad (HOGP)
python3 -m src.hogp start --mode usb --profile gamepad         # DUALSHOCK 4
python3 -m src.hogp status
python3 -m src.hogp stop
```

### Xbox controller over Bluetooth (recommended for macOS + Steam)

1. Select **Bluetooth** + **Xbox (BT)** → **Start Service** — wait for **Active - Discoverable**
2. On the Mac: **System Settings → Bluetooth** → **Xbox Wireless Controller** → **Connect**, accept the pairing request
3. **System Settings → Game Controllers** lists an Xbox controller; Steam on the Mac sees it as an Xbox Series controller
4. Rumble from games is played on the Deck's haptics (evdev force feedback — needs the Deck's built-in controller as the input device; check `journalctl -u bt-controller-emulator` for "no FF_RUMBLE support")

Each profile sets bluetoothd's Device ID in `/etc/bluetooth/main.conf` (Xbox `045E:0B13`, Pro `057E:2009`) and restarts bluetooth once when switching. macOS caches the vendor/product ID per Deck address, so **after switching profiles, Forget the device on the Mac and pair again** — `system_profiler SPBluetoothDataType` on the Mac must show Vendor ID 0x045E / Product ID 0x0B13 for the Xbox profile.

### Pro Controller over Bluetooth (macOS / Android / Linux)

1. Select **Bluetooth** + **Gamepad (macOS)** → **Start Service** — wait for **Active - Pro Controller**
2. On the Mac: **System Settings → Bluetooth** → find **Pro Controller** → **Connect**
3. Accept the **Bluetooth Pairing Request** (Just Works, no PIN)
4. The Deck UI shows **✓ Paired — streaming as Pro Controller**
5. Check **System Settings → Game Controllers** — a **Pro Controller** should be listed; test sticks/buttons in the Controller tab visualizer first (visualizer proves evdev forwarding before the Mac is involved)
6. The Deck's real battery level is transmitted with the Pro Controller input reports (byte 2, refreshed every ~15 s) — macOS shows it next to the controller in System Settings

### Keyboard over Bluetooth (SteamDeckPad, BLE HOGP)

1. Select **Bluetooth** + **Keyboard** → **Start Service** — wait for **Active - Discoverable**
2. On the host: pair **SteamDeckPad** (Just Works). The daemon completes bonding automatically (Pair + Trust) when the host subscribes to HID notifications — the UI shows **✓ Paired — sending HID data**
3. The on-screen Keyboard / Media / Trackpad tabs and the Deck controls all stream to the host

You can keep both pairings on the Mac and switch by stopping/starting the service with the desired profile. The Deck battery is exposed on the standard HOGP Battery Service (0x180F).

### Gamepad over Wired USB

1. **BIOS:** Shut down → hold **Volume +** + **Power** → Setup Utility → **Advanced** → **USB Configuration** → **USB Dual Role Device** → **DRD** → save and exit. Cold boot.
2. Connect Deck to host with a **data** USB-C cable.
3. In the app: **Wired USB** + **Gamepad (macOS)** → **Start Service**. The daemon configures the gadget itself (no password prompt).
4. On the Mac: **System Settings → Game Controllers** — a **Wireless Controller** (DUALSHOCK 4) should appear. **System Report → USB** shows the composite device (054C:05C4) with gamepad, keyboard and mouse interfaces.

The **Keyboard** profile keeps the generic gamepad identity for hosts that don't support DS4 descriptors.

Manual gadget setup: `sudo ~/steamdeck-bt-controller-emulator/scripts/setup-usb-gadget.sh [--profile ds4|generic]`
Teardown: `sudo ~/steamdeck-bt-controller-emulator/scripts/cleanup-usb-gadget.sh`

---

## Latency

Input is forwarded event-driven with a ~4 ms floor and no batching beyond a short stick/trigger coalesce:

- **Wired (DS4):** every state change writes a report to `/dev/hidg0` immediately; the host polls the USB interrupt endpoint at its native interval (sub-millisecond on high-speed links)
- **Bluetooth (Pro Controller):** buttons/sticks/hat changes trigger an immediate `0x30` report, with a 125 Hz baseline stream keeping the link alive between changes
- The daemon coalesces only stick/trigger axes (~4 ms) — buttons and the D-pad apply immediately

---

## Diagnostics

If anything fails, run this and share the output when asking for help:

```bash
~/steamdeck-bt-controller-emulator/scripts/diagnose.sh
```

Daemon log:

```bash
journalctl -u bt-controller-emulator -f        # live
~/steamdeck-bt-controller-emulator/scripts/view-logs.sh   # last 100 lines
```

Other helpers:

| Script | Purpose |
|--------|---------|
| [`scripts/diagnose.sh`](scripts/diagnose.sh) | Daemon, D-Bus API, BlueZ, evdev gamepads, USB gadget |
| [`scripts/check-usb-gadget.sh`](scripts/check-usb-gadget.sh) | USB gadget / `hidg` only |
| [`scripts/view-logs.sh`](scripts/view-logs.sh) | Tail the daemon journal |

**Quick evdev check:** `evtest` — you should see something like **Valve Software Steam Deck Controller** or **Microsoft X-Box 360 pad** (Steam virtual pad). If the list has no gamepad, the daemon cannot forward sticks/buttons until SteamOS exposes input (try quitting Steam, Desktop Mode, or a stable OS channel).

---

## Troubleshooting

### Start Service fails with a D-Bus error

The GUI shows the exact failure. The most common causes, in order:

1. **Daemon not running** — `sudo systemctl start bt-controller-emulator`, or re-run [install.sh](#installation).
2. **Pairing agent error (`AlreadyExists`)** — another process holds BlueZ's default agent. `sudo systemctl restart bluetooth` clears it; nothing else should register an agent now that the daemon owns it.
3. **SDP record error** — BlueZ rejected the profile registration. `journalctl -u bluetooth -n 50` + `journalctl -u bt-controller-emulator -n 100`.
4. **`Could not bind HID L2CAP sockets: PSM 0x11/0x13 busy`** — something else on the Deck holds the HID ports. The daemon already tries the usual fixes itself before giving up: it kills leftover `hid-l2cap-bridge` processes from an older install, unloads the kernel `hidp` module, and finally restarts `bluetooth.service` once. If the error persists, the message now shows the exact kernel errno and evidence of who holds the ports (full details in `journalctl -u bt-controller-emulator`). Two things to check first:

   - **Daemon not running as root** — well-known PSMs need CAP_NET_BIND_SERVICE, and a manually-started daemon fails every bind with `EACCES`. Verify: `ps -eo pid,user,args | grep hogp.service` — the user column must be `root`. Start it via `sudo systemctl start bt-controller-emulator`.
   - **A Bluetooth keyboard/mouse connected to the Deck itself** — `hidp` holds the ports for active connections and cannot be unloaded while one is up: `bluetoothctl devices Connected` → `bluetoothctl disconnect <MAC>` → Start again.

   Manual checks (no `ss -f bluetooth` needed — some SteamOS iproute2 builds lack it):

   ```bash
   cat /proc/net/l2cap | grep -E '0011|0013'             # kernel view of the HID ports
   ps -eo pid,user,args | grep hogp.service              # daemon must run as root
   bluetoothctl devices Connected                         # Deck-side BT keyboards/mice
   sudo systemctl restart bt-controller-emulator          # frees the daemon's own sockets
   sudo systemctl restart bluetooth                       # catch-all: frees bluetoothd's L2CAP sockets
   ```

### Gamepad pairs as "Pro Controller" but Mac shows nothing in Game Controllers

1. Confirm the Deck UI shows **✓ Paired — streaming as Pro Controller** (both L2CAP channels connected).
2. Check `journalctl -u bt-controller-emulator`: you should see subcommand traffic (`Pro Controller subcommand 0x02 …`, always logged) — that proves macOS is running the Pro init protocol.
3. On the Mac, remove the **Pro Controller** entry, toggle Bluetooth off/on, and pair again.
4. If pairing spins forever: the Deck must be **pairable + connectable** with Class of Device `0x002508`; the daemon sets this at start. Re-run install.sh if the D-Bus policy blocks it.
5. macOS 13+ is required for Pro Controller support (macOS 26 Tahoe / 27 Golden Gate both include it).

### Pairs but no input (Bluetooth gamepad)

- macOS identifies the Pro Controller by its SDP **Device ID (VID 057E / PID 2009)**, which bluetoothd publishes from `/etc/bluetooth/main.conf`. The daemon sets `DeviceID = usb:057E:2009:0001` there on the first Gamepad start (original backed up to `main.conf.btemu-bak`, restored by uninstall.sh) and restarts bluetooth once. **If the Mac paired before this, remove "Pro Controller" on the Mac and pair again** — macOS caches the old SDP records.
- `journalctl -u bt-controller-emulator` must show `Pro Controller subcommand 0x02 …` lines after connecting; if it shows only "channel connected", the Mac isn't running the Pro init (usually the cached Device ID above).
- The Mac must be the one initiating the L2CAP HID connection — if another host (Android/Windows) previously paired, forget the Deck there or it may hold the connection.
- On the Deck, quit Steam (Desktop Mode) so the built-in controller is exposed to evdev (`evtest` must show a gamepad).
- Watch the Controller tab visualizer while pressing sticks/buttons — if it doesn't move, the problem is evdev, not Bluetooth.

### macOS shows connected but not "Paired" (SteamDeckPad)

The daemon calls `Device1.Pair()` and sets `Trusted` when HID notifications start (and retries every 20 s per device).

1. On the Mac: **System Settings → Bluetooth** — remove old **SteamDeckPad** entries, then connect again from the list.
2. If the Mac stalls on pairing: accept any **"Bluetooth Pairing Request"** dialog (Just Works / confirm).
3. Deck UI should show **Paired — sending HID data** with **Paired: yes** and **Trusted: yes**. If **Paired** stays `no`, run `diagnose.sh` and check both journals while connecting.
4. If macOS shows the Deck as a **laptop** or adds a **Bluetooth audio output**: remove **every** "Steam Deck" / **SteamDeckPad** entry on the Mac, start the service (it switches the adapter to **LE-only** while running), then pair **only SteamDeckPad**. Stop the service before using Deck Bluetooth headphones again.

### Visible in discovery but never connects / pairs

- The daemon registers an auto-accept **pairing agent** automatically; make sure the service is active (`btemu status`).
- On the **host**, start the pairing flow from Bluetooth settings (don't rely only on "scan").
- If Android pairs then disconnects: on the Deck (Developer mode), try in `/etc/bluetooth/main.conf` under `[General]`: `ReverseServiceDiscovery = false`, then `sudo systemctl restart bluetooth`.

### Wired shows Active but the Mac doesn't see the device

- Re-plug the USB-C cable (the Mac re-enumerates on a fresh plug)
- On the Mac: **System Report → USB** — the composite device should appear as **054C:05C4** (Gamepad profile) or **1209:0001** (Keyboard profile)
- The daemon rebuilds the gadget whenever the configured profile doesn't match (tracked in `/tmp/steamdeck_hid.profile`); if it still says Active but the Mac sees nothing: `sudo ~/steamdeck-bt-controller-emulator/scripts/cleanup-usb-gadget.sh`, start again, then check `dmesg | tail -50` for `configfs-gadget` errors. A wedged dwc3 UDC only recovers on a Deck reboot

### Wired USB fails immediately

- BIOS **DRD** enabled, **cold boot**
- `passwd` set for user `deck` (needed for the installer's sudo)
- `check-usb-gadget.sh` — need UDC, gadget bound, `/dev/hidg0`–`hidg2` present

### Both modes fail on Preview / Main

Some prerelease builds do not expose built-in controls to evdev while Steam or the kernel holds them. Use `diagnose.sh` and `evtest`. Mitigations: Desktop Mode, exit Steam before starting the service, **Stable** channel, or USB mode with DRD.

### Pro Controller fails with PSM 0x11/0x13 busy on Preview / Main

Newer prerelease images ship a bluetoothd whose `input` plugin registers the HID L2CAP PSMs itself. Symptoms: Start fails with **PSM 0x11/0x13 busy** even with nothing paired, and `sudo python3 scripts/check-psm.py` shows bluetoothd channels on PSMs 17/19 in the debugfs table.

```bash
sudo bash ~/steamdeck-bt-controller-emulator/scripts/disable-bluez-input.sh
```

Revert with `--undo`. While the plugin is disabled, the Deck cannot use Bluetooth keyboards/mice connected to itself — nothing else is affected (the emulator's own pairing/audio paths don't need it).

### General pairing / mapping tips

- Confirm matching PIN when prompted (often acknowledge on PC first, then Deck)
- Triggers/bumpers may need one-time mapping in the host's controller settings
- **Guide / QAM** on the physical Deck are not forwarded; use the in-app **Home** / **QAM** buttons (keyboard shortcuts)

---

## Development

From repo root on a dev machine:

- `just deploy` — sync source to the Deck over SSH (edit IP in justfile)
- `just restart` — restart the daemon after a deploy
- `just logs` — follow the daemon journal
- `python3 -m src.hogp.service` — run the daemon in the foreground (as root) without systemd

Key modules:

| Module | Role |
|--------|------|
| [`src/hogp/service.py`](src/hogp/service.py) | Root daemon: session state machine, BlueZ lifecycle, watchdog/self-heal, D-Bus API |
| [`src/hogp/client.py`](src/hogp/client.py) | Unprivileged D-Bus client (GUI + CLI) |
| [`src/hogp/api.py`](src/hogp/api.py) | D-Bus contract (name, path, interface, status keys) |
| [`src/hogp/bluez.py`](src/hogp/bluez.py) | BlueZ helpers: async registration, adapter snapshot/restore, btmgmt (no sudo) |
| [`src/hogp/gatt_app.py`](src/hogp/gatt_app.py) | HID-over-GATT service (report maps, ATT notify throttle) |
| [`src/hogp/xbox.py`](src/hogp/xbox.py) | Xbox Wireless Controller (BLE HoG, rumble → Deck haptics) |
| [`src/hogp/pro_controller.py`](src/hogp/pro_controller.py) | Pro Controller emulator (L2CAP HIDP + subcommand engine, direct PSM bind) |
| [`src/hogp/input_handler.py`](src/hogp/input_handler.py) | evdev gamepad detection + forwarding |
| [`src/hogp/usb_gadget.py`](src/hogp/usb_gadget.py) / [`ds4.py`](src/hogp/ds4.py) | USB gadget HID + DualShock 4 identity |
| [`src/hogp/gui.py`](src/hogp/gui.py) | GTK4 client GUI |

---

## References

- Protocol research: [dekuNukem/Nintendo_Switch_Reverse_Engineering](https://github.com/dekuNukem/Nintendo_Switch_Reverse_Engineering) (BT HID reports + subcommands), [mart1nro/joycontrol](https://github.com/mart1nro/joycontrol) (Pro Controller L2CAP server + SDP record), Linux [hid-nintendo.c](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-nintendo.c) (report structs, calibration), [esp-cpp/espp](https://github.com/esp-cpp/espp) `switch_pro` (subcommand reply engine)
- DualShock 4 USB: [chrippa/ds4drv](https://github.com/chrippa/ds4drv) (report layout), [orcadom/DS4](https://github.com/orcadom/DS4) (public-domain USB report descriptor)
- HOGP: [EmuBTHID](https://github.com/Alkaid-Benetnash/EmuBTHID), [BTGamepad](https://github.com/007durgesh219/BTGamepad), [diyps3controller](https://github.com/rafikel/diyps3controller), [GIMX](https://github.com/matlo/GIMX), [DeckJoy](https://github.com/Lucaber/deckjoy) (USB gadget reference), [bt-keyboard-adapter](https://github.com/gwyntel/bt-keyboard-adapter) (BlueZ HOGP reference)
