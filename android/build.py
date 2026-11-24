"""Build what penrose ships to the phone into desktop/assets/android (listed in its manifest.json):

    python android/build.py                  # all of it
    python android/build.py bridge home      # only these: bridge, home, autoscope, profile

The desktop app brings every phone up to date with these files on its next start (backend/app/provision,
`reconcile`): what changed is installed and the phone restarts once. The APKs are signed with penrose's release
key (android/toolchain.py, android/README.md).
"""

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from toolchain import ASSETS, Tools  # noqa: E402


def autoscope(dest: Path):
    """The relay-autoscope Magisk module: its files zipped in sorted order with LF line endings and a fixed
    timestamp, so the same source always gives the same zip (the phone's record compares hashes)."""
    source = HERE / "autoscope"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(source).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o755 << 16
            z.writestr(info, path.read_bytes().replace(b"\r\n", b"\n"))


def main():
    parts = ["bridge", "home", "autoscope", "profile"]
    parser = argparse.ArgumentParser()
    parser.add_argument("parts", nargs="*", choices=parts, help="what to build (default: all)")
    parser.add_argument("--sdk")
    parser.add_argument("--jdk")
    args = parser.parse_args()
    wanted = args.parts or parts
    tools = Tools(args.sdk, args.jdk) if {"bridge", "home"} & set(wanted) else None
    ASSETS.mkdir(parents=True, exist_ok=True)
    if "bridge" in wanted:
        sys.path.insert(0, str(HERE / "bridge"))
        import build as bridge  # android/bridge/build.py
        shutil.copyfile(bridge.build(tools=tools), ASSETS / "relay-bridge.apk")
        sys.path.remove(str(HERE / "bridge"))
        sys.modules.pop("build")
    if "home" in wanted:
        sys.path.insert(0, str(HERE / "home"))
        import build as home  # android/home/build.py
        shutil.copyfile(home.build(tools=tools), ASSETS / "relay-home.apk")
    if "autoscope" in wanted:
        autoscope(ASSETS / "relay-autoscope.zip")
    if "profile" in wanted:
        text = (HERE / "device-profile" / "relay-device-profile.sh").read_bytes().replace(b"\r\n", b"\n")
        (ASSETS / "relay-device-profile.sh").write_bytes(text)
    for name in ("relay-bridge.apk", "relay-home.apk", "relay-autoscope.zip", "relay-device-profile.sh"):
        print(f"{ASSETS / name}  {(ASSETS / name).stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
