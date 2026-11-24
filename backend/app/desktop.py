"""One program for the desktop app: `relay-server <service>`, so PyInstaller bundles a single executable.

    relay-server api --port 8001        the API, also serving the web UI (FRONTEND_DIR)
    relay-server gateway --port 8002    the device gateway (adb, the bridge, the live view)
    relay-server worker                 the run executor
    relay-server provision ...          first-run Android setup (app/provision)
    relay-server phone|doctor|setup ... the `relay` command for Claude Code and Cowork (app/cli.py); the app
                                        installs a `relay` launcher that runs this program

The Electron app sets everything else through the environment (DATA_DIR, API keys, ADB_PATH, ...).
Services listen on 127.0.0.1 only.
"""

import argparse
import asyncio
import multiprocessing
import os
import sys
import threading
import time


def exit_with_parent():
    """Exit when the Electron app that started this service is gone, even if it was killed without
    stopping its services (crash, Task Manager). RELAY_PARENT_PID is set by the app."""
    try:
        pid = int(os.environ.get("RELAY_PARENT_PID", ""))
    except ValueError:
        return

    def wait_windows():
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            os._exit(0)  # already gone
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        os._exit(0)

    def wait_posix():
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                os._exit(0)
            except PermissionError:
                pass
            time.sleep(2)

    target = wait_windows if sys.platform == "win32" else wait_posix
    threading.Thread(target=target, name="exit-with-parent", daemon=True).start()


def serve(app, port):
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info", access_log=False)


CLI_COMMANDS = ("phone", "doctor", "setup", "mcp", "run", "reply", "cancel", "-h", "--help")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in CLI_COMMANDS:
        from .cli import main as cli

        return cli(argv)
    parser = argparse.ArgumentParser(prog="relay-server")
    sub = parser.add_subparsers(dest="service", required=True)
    for name in ("api", "gateway"):
        sub.add_parser(name).add_argument("--port", type=int, required=True)
    sub.add_parser("worker")
    sub.add_parser("provision", add_help=False)
    args, rest = parser.parse_known_args(argv)
    exit_with_parent()
    if args.service != "provision":  # setup starts and stops emulators of its own: left as it was
        from .processes import die_with_this_process

        die_with_this_process()

    if args.service == "api":
        from .api import app

        app.state.port = args.port  # for the discovery file the `relay` command reads (api/phone.py publish)
        serve(app, args.port)
    elif args.service == "gateway":
        from .gateway import app

        serve(app, args.port)
    elif args.service == "worker":
        from .worker import main as worker

        asyncio.run(worker())
    else:
        from .provision import main as provision

        return provision(rest)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
