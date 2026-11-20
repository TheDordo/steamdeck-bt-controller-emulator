"""
Command-line client for the emulator daemon.

    python3 -m src.hogp status
    python3 -m src.hogp start --mode bluetooth --profile gamepad
    python3 -m src.hogp start --mode bluetooth --profile xbox
    python3 -m src.hogp start --mode bluetooth --profile ds4
    python3 -m src.hogp start --mode usb --profile keyboard
    python3 -m src.hogp stop
    python3 -m src.hogp watch

Legacy flags are accepted: --mode pro maps to bluetooth + gamepad and
--mode hogp maps to bluetooth + keyboard.

The daemon itself runs as `python3 -m src.hogp.service` (systemd unit
bt-controller-emulator.service).
"""

import argparse
import sys

from .api import (
    MODE_BLUETOOTH, MODE_USB, PROFILE_GAMEPAD, PROFILE_KEYBOARD, PROFILE_XBOX, PROFILE_DS4,
    KEY_STATUS, KEY_MODE, KEY_PROFILE, KEY_DETAIL, KEY_ERROR, KEY_CONN,
    KEY_CONNECTED, KEY_PAIRED, KEY_TRUSTED, KEY_INPUT, KEY_BATTERY,
    KEY_DEVICE_NAME, KEY_DEVICE_ADDR, KEY_READY, KEY_KB_CAPABLE,
    KEY_BUTTONS, KEY_AXES, KEY_TRIGGERS, KEY_HAT,
)
from .client import BtEmuClient, BtEmuError


def _print_status(client: BtEmuClient) -> int:
    try:
        status = client.get_status()
    except BtEmuError as e:
        print(f"Error: {e}")
        return 1

    detail = status.get(KEY_DETAIL, "")
    print(f"Status:   {detail}")
    print(f"Mode:     {status.get(KEY_MODE, '')} ({status.get(KEY_PROFILE, '')})")
    print(f"Conn:     {status.get(KEY_CONN, '')}")
    device = status.get(KEY_DEVICE_NAME, "")
    if device:
        print(f"Device:   {device} ({status.get(KEY_DEVICE_ADDR, '')}) "
              f"paired={status.get(KEY_PAIRED, False)} trusted={status.get(KEY_TRUSTED, False)}")
    print(f"Input:    {status.get(KEY_INPUT, '') or 'none'}")
    if status.get("speaker"):
        print(f"Speaker:  {status['speaker']}")
    lightbar = status.get("lightbar", -1)
    if isinstance(lightbar, int) and lightbar >= 0:
        print(f"Lightbar: #{lightbar:06x}")
    print(f"Battery:  {status.get(KEY_BATTERY, 0)}%")
    if status.get(KEY_ERROR):
        print(f"Error:    {status.get(KEY_ERROR)}")
    if status.get(KEY_STATUS) in ("active", "degraded"):
        axes = status.get(KEY_AXES, [0, 0, 0, 0])
        triggers = status.get(KEY_TRIGGERS, [0, 0])
        print(f"State:    buttons=0x{status.get(KEY_BUTTONS, 0):04X} "
              f"axes={axes} triggers={triggers} hat={status.get(KEY_HAT, 8)}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="btemu",
        description="Control the Steam Deck BT Controller Emulator daemon.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="show daemon + session status")
    sub.add_parser("stop", help="stop the active session")

    start = sub.add_parser("start", help="start a session")
    start.add_argument("--mode", choices=("bluetooth", "usb", "pro", "hogp", "xbox", "ds4"),
                       default="bluetooth",
                       help="transport (pro = bluetooth gamepad, hogp = bluetooth keyboard, "
                            "xbox = bluetooth Xbox controller, ds4 = bluetooth DualShock 4)")
    start.add_argument("--profile", choices=("gamepad", "xbox", "ds4", "keyboard"),
                       help="HID identity (default: gamepad for pro, keyboard otherwise)")

    sub.add_parser("watch", help="follow status changes (Ctrl+C to exit)")
    sub.add_parser("daemon", help="run the privileged daemon in the foreground (dev)")

    args = parser.parse_args(argv)
    command = args.command or "status"

    if command == "daemon":
        from .service import main as service_main
        return service_main()

    client = BtEmuClient()
    if not client.daemon_running():
        print("The emulator daemon is not running.")
        print("Start it with:  sudo systemctl start bt-controller-emulator")
        print("(or re-run the installer: curl -fsSL https://raw.githubusercontent.com/"
              "TheDordo/steamdeck-bt-controller-emulator/main/install.sh | bash)")
        return 1

    try:
        if command == "status":
            return _print_status(client)

        if command == "stop":
            client.stop()
            print("Stopped.")
            return 0

        if command == "start":
            if args.mode in ("pro", "hogp", "xbox", "ds4"):
                mode = MODE_BLUETOOTH
                profile = {
                    "pro": PROFILE_GAMEPAD, "hogp": PROFILE_KEYBOARD, "xbox": PROFILE_XBOX,
                    "ds4": PROFILE_DS4,
                }[args.mode]
                if args.profile:
                    profile = args.profile
            else:
                mode = args.mode
                profile = args.profile or (
                    PROFILE_GAMEPAD if args.mode == MODE_BLUETOOTH else PROFILE_KEYBOARD
                )
            client.start(mode, profile)
            print("Started. Use 'btemu status' for details.")
            return 0

        if command == "watch":
            print("Watching status (Ctrl+C to exit)…")
            import signal
            from gi.repository import GLib

            loop = GLib.MainLoop()
            sub_id = client.subscribe_status(
                lambda s: print(f"[{s}] {client.get_status().get(KEY_DETAIL, '')}")
            )
            signal.signal(signal.SIGINT, lambda sig, frame: loop.quit())
            loop.run()
            client.unsubscribe_status(sub_id)
            return 0
    except BtEmuError as e:
        print(f"Error: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
