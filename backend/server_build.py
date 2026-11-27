"""Freeze relay-server with PyInstaller: Python and the libraries, without penrose's own code (relay_server.py).

Run by desktop/scripts/build-server.js, in backend/ with the build's Python. Because `app` is not analysed,
what it needs is named here: every module it imports today, every submodule of each library it uses and the whole
standard library, so that an update of `app` alone can import something it did not import before.
"""

import ast
import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent

# Standard-library packages with no use in a service (windows, tests, Python's own tools).
LEFT_OUT = {"antigravity", "ensurepip", "idlelib", "lib2to3", "test", "this", "tkinter", "turtle", "turtledemo"}


def imports(folder):
    """Every module named by an import statement under `folder`, wherever it is in the file."""
    found = set()
    for file in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_bytes(), str(file))):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                found.add(node.module)
    return found


def exists(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def arguments():
    used = {name for name in imports(BACKEND / "app") if name.split(".")[0] != "app"}
    stdlib = {name for name in sys.stdlib_module_names if name not in LEFT_OUT and exists(name)}
    libraries = {name.split(".")[0] for name in used} - sys.stdlib_module_names
    libraries = {name for name in libraries if exists(name)} | {"uvicorn"}
    args = ["relay_server.py", "--name", "relay-server", "--noconfirm", "--clean", "--onedir", "--console",
            "--distpath", "dist", "--workpath", str(Path("build") / "pyinstaller")]
    for name in sorted(stdlib | {name for name in used if exists(name)}):
        args += ["--hidden-import", name]
    # Packages of the standard library bring their submodules too (email.mime, xml.etree, ...).
    packages = {name for name in stdlib if (spec := importlib.util.find_spec(name)) and spec.submodule_search_locations}
    for name in sorted(packages | libraries):
        args += ["--collect-submodules", name]
    for name in sorted(LEFT_OUT | {"app", "pytest"}):
        args += ["--exclude-module", name]
    return args


if __name__ == "__main__":
    from PyInstaller.__main__ import run

    run(arguments())
