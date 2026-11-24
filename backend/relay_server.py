"""PyInstaller's entry point for the desktop app's `relay-server` (see app/desktop.py)."""

import multiprocessing
import sys

from app.desktop import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
