#!/usr/bin/env python3
"""Throwaway test phones, so a Claude session (or a person) can test on an emulator without touching the user's
penrose phone (emulator-5570) or the image setup builds (emulator-5580).

A test phone is Relay's golden image (rooted, Vector, relay-bridge, the Relay home app; built by setup) started
read-only on a port of its own: nothing it does is kept, every start is a clean phone, and several can run at once.
Each one costs memory (2 GB by default), so stop it when done.

    python3 scripts/test_phone.py start [--window] [--memory 2048]   # prints the serial, e.g. emulator-5600
    python3 scripts/test_phone.py list
    python3 scripts/test_phone.py stop emulator-5600                 # or: stop all

Then use it with `adb -s <serial> ...` (install your app with `adb -s <serial> install -r app.apk`). Standard library
only. Relay's data folder is found per OS, or set RELAY_DATA_DIR (or --data) for another profile."""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

GOLDEN = "relay-golden"
GOLDEN_SERIAL = "emulator-5580"
PORTS = range(5600, 5700, 2)  # the console port; adb uses the next one. Relay's own are 5570 and 5580.
WINDOWS = sys.platform == "win32"


def data_dir(override=None):
    if override or os.environ.get("RELAY_DATA_DIR"):
        return Path(override or os.environ["RELAY_DATA_DIR"]).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Relay"
    if WINDOWS:
        return Path(os.environ["APPDATA"]) / "Relay"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Relay"


def sdk_dir(data: Path):
    try:
        configured = json.loads((data / "config.json").read_text()).get("sdk")
    except (OSError, ValueError):
        configured = None
    return Path(configured) if configured else data / "android" / "sdk"


def tool(sdk: Path, folder, name):
    return sdk / folder / (name + (".exe" if WINDOWS else ""))


def adb(sdk, *args, timeout=30):
    result = subprocess.run([str(tool(sdk, "platform-tools", "adb")), *args], capture_output=True, text=True,
                            timeout=timeout)
    return result.stdout


def running(sdk):
    return [line.split()[0] for line in adb(sdk, "devices").splitlines()[1:] if line.startswith("emulator-")]


def port_free(port):
    for p in (port, port + 1):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                return False
    return True


def start(data: Path, window: bool, memory: int, grpc: int | None = None):
    sdk, golden_home = sdk_dir(data), data / "android" / "golden"
    if not (golden_home / f"{GOLDEN}.avd" / "provisioned").exists():
        sys.exit(f"No finished penrose image in {golden_home}: run penrose's phone setup first.")
    if GOLDEN_SERIAL in running(sdk):
        sys.exit("penrose's setup is building its image right now; start a test phone once it has finished.")
    serials = set(running(sdk))
    port = next((p for p in PORTS if f"emulator-{p}" not in serials and port_free(p)), None)
    if port is None:
        sys.exit("No free port for another test phone; stop one first (list shows them).")
    serial = f"emulator-{port}"
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "ANDROID_HOME": str(sdk), "ANDROID_SDK_ROOT": str(sdk), "ANDROID_AVD_HOME": str(golden_home)}
    args = [str(tool(sdk, "emulator", "emulator")), "-avd", GOLDEN, "-port", str(port), "-read-only", "-no-snapshot",
            "-no-audio", "-no-boot-anim", "-gpu", "host", "-feature", "-Vulkan", "-memory", str(memory),
            # After an emulator crash the next start asks whether to send the crash report, in a window nobody sees,
            # and waits for the answer before it boots (the reports are shared by every emulator on this computer).
            "-crash-report-mode", "never"]
    if not window:
        args.append("-no-window")
    if grpc:  # the emulator's gRPC service without a token, for tools that drive it (AndroidWorld)
        args += ["-grpc", str(grpc)]
    if WINDOWS:
        # As penrose starts its own phone (desktop/src/main.js): without it a test phone can drop to its slow clock
        # (hpet) within minutes and everything measured on it is 1.5 to 2 times slower. Last: the rest goes to qemu.
        args += ["-qemu", "-append", "tsc=nowatchdog"]
    log_path = logs / f"test-phone-{port}.log"
    launcher = Path(__file__).resolve().parent.parent / "backend" / "app" / "phone_desktop.py"
    if WINDOWS and not window and launcher.exists():
        # As penrose starts its own phone: on a desktop of its own (backend/app/phone_desktop.py has why). On the
        # desktop the user works on, the windows the OpenGL driver keeps for the phone are slow to make, the phone
        # stops drawing for seconds to minutes as their number grows, and so do the user's own programs. A phone
        # with a window to look at (--window) has to be on the user's desktop.
        log_path.write_text("")
        subprocess.run([sys.executable, str(launcher), "--detach", "--log", str(log_path), "--", *args], env=env,
                       stdin=subprocess.DEVNULL, check=True)
    else:
        with open(log_path, "w") as log:
            subprocess.Popen(args, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             start_new_session=not WINDOWS,
                             creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if WINDOWS else 0)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if adb(sdk, "-s", serial, "shell", "getprop sys.boot_completed").strip() == "1":
            print(serial)
            return
        time.sleep(2)
    sys.exit(f"{serial} did not finish starting within 5 minutes; see {logs / f'test-phone-{port}.log'}")


def test_phones(sdk):
    return [s for s in running(sdk) if int(s.split("-")[1]) in PORTS]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", help="penrose's data folder (default: the app's, or RELAY_DATA_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("start", help="start a clean test phone and print its serial")
    begin.add_argument("--window", action="store_true", help="show the phone's screen in a window")
    begin.add_argument("--memory", type=int, default=2048, help="the phone's memory in MB (default 2048)")
    begin.add_argument("--grpc", type=int, help="serve the emulator's gRPC on this port (no token)")
    sub.add_parser("list", help="the test phones running now")
    end = sub.add_parser("stop", help="stop a test phone (all its changes are discarded)")
    end.add_argument("serial", help="its serial, or all")
    args = parser.parse_args(argv)
    data = data_dir(args.data)
    sdk = sdk_dir(data)
    if args.command == "start":
        start(data, args.window, args.memory, args.grpc)
    elif args.command == "list":
        print("\n".join(test_phones(sdk)) or "no test phones running")
    else:
        targets = test_phones(sdk) if args.serial == "all" else [args.serial]
        for serial in targets:
            if serial not in test_phones(sdk):
                sys.exit(f"{serial} is not a test phone; only test phones (emulator-5600 and up) are stopped here.")
            adb(sdk, "-s", serial, "emu", "kill")
            print(f"stopped {serial}")


if __name__ == "__main__":
    main()
