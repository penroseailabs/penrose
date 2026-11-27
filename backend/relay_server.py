"""PyInstaller's entry point for the desktop app's `relay-server` (see app/desktop.py).

The program holds Python and the libraries only. penrose's own code (`app`, with its prompts and skills beside
it) is plain files in a content folder, so an update can replace it without a new installer (desktop/src/content.js):
the one the app names in RELAY_CONTENT, else the one the app last used (for the `relay` command and the Claude
app, which start this program themselves), else the one the installer put beside this program.

    relay-server check-content <folder> [<zip>]   unpack the zip into the folder, then import every module in it:
                                                  an update that needs something this program lacks is refused

This file ships only in the installer, so it uses nothing from `app`.
"""

import importlib
import json
import multiprocessing
import os
import sys
import zipfile
from pathlib import Path

# Standard-library modules that exist on one kind of computer only: code for another computer may import them.
PLATFORM_ONLY = {"_winapi", "fcntl", "grp", "msvcrt", "pwd", "resource", "termios", "winreg", "winsound"}


def bundled_content():
    """resources/content, beside resources/server where this program is."""
    return Path(sys.executable).resolve().parent.parent / "content"


def read_json(path):
    try:
        value = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def data_dirs(environ):
    """Where the app may keep its data, most likely first (as app/cli.py data_dirs and desktop/src/main.js do)."""
    if file := environ.get("RELAY_CLI_FILE"):
        yield Path(file).parent.parent  # <data folder>/data/cli.json
    if folder := environ.get("RELAY_DATA_DIR"):
        yield Path(folder)
    home = Path.home()
    if sys.platform == "win32":
        base = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        raw = (base / "penrose" / "data-location.txt").read_bytes()
        chosen = raw.decode("utf-16-le" if b"\0" in raw else "utf-8", "ignore").lstrip("﻿").strip()
        if chosen and Path(chosen).is_absolute():
            yield Path(chosen)
    except OSError:
        pass
    yield base / "Relay"


def content_root(environ=os.environ, bundled=None):
    if folder := environ.get("RELAY_CONTENT"):
        return Path(folder)
    bundled = bundled or bundled_content()
    shell = read_json(bundled / "content.json").get("shell")
    for data in data_dirs(environ):
        active = read_json(data / "updates" / "active.json").get("dir")
        # Only content made for this program: after a new installer, the old update is left alone.
        if active and read_json(Path(active) / "content.json").get("shell") == shell:
            return Path(active)
    return bundled


def use(root):
    sys.path.insert(0, str(Path(root) / "backend"))


def check_content(folder, archive=None):
    """0 when every module of the content in `folder` imports with what this program holds."""
    folder = Path(folder)
    if archive:
        with zipfile.ZipFile(archive) as z:
            z.extractall(folder)  # extractall keeps every file inside the folder
    use(folder)
    package = folder / "backend" / "app"
    if not (package / "desktop.py").is_file():
        print(f"No app in {folder}", file=sys.stderr)
        return 1
    failed = 0
    for file in sorted(package.rglob("*.py")):
        parts = file.relative_to(package.parent).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        try:
            importlib.import_module(name)
        except (ImportError, SyntaxError) as exc:
            if getattr(exc, "name", None) in PLATFORM_ONLY:
                continue
            failed += 1
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception as exc:  # not about what this program holds: the app's own start check finds real faults
            print(f"{name}: {type(exc).__name__}: {exc} (not counted)", file=sys.stderr)
    return 1 if failed else 0


def run(argv):
    # Never write bytecode beside the code: inside a Mac app that breaks its signature.
    if cache := os.environ.get("RELAY_PYCACHE"):
        sys.pycache_prefix = cache
    else:
        sys.dont_write_bytecode = True
    if argv[:1] == ["check-content"]:
        sys.dont_write_bytecode = True
        return check_content(*argv[1:3])
    use(content_root())
    return importlib.import_module("app.desktop").main(argv)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(run(sys.argv[1:]))
