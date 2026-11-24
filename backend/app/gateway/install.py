"""install_app: an APK straight from APKPure (apkeep), for when Play Store cannot be used. Failures say why in
words an agent can act on, and always name the fallback (Play Store, then the website)."""

import contextlib
import re
import tempfile
import zipfile
from pathlib import Path

from fastapi import HTTPException

from ..android import validate_package
from ..config import settings
from . import core

# Sideloads are otherwise held for Play Protect review or a consent dialog.
VERIFIER_SETTINGS = {
    "verifier_verify_adb_installs": "0",
    "package_verifier_enable": "0",
    "package_verifier_user_consent": "-1",
}


PLAY_STORE_NEXT_STEP = (
    "Install from Play Store instead: open market://details?id={package} with adb, then act "
    "with goal 'Install the app and open it'. If Play Store has no suitable app, use the website in the "
    "browser."
)

# What Android's install failure codes mean, in words an agent can act on.
INSTALL_FAILURES = {
    "INSTALL_FAILED_NO_MATCHING_ABIS": "the app has no build for this device's CPU architecture",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE": "the device is out of storage",
    "INSTALL_FAILED_VERSION_DOWNGRADE": "a newer version is already installed",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE": "an installed copy is signed differently and blocks this one",
    "INSTALL_FAILED_OLDER_SDK": "the app needs a newer Android version than this device runs",
    "INSTALL_FAILED_MISSING_SHARED_LIBRARY": "the app needs a system library this device lacks",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES": "the downloaded APK is unsigned or damaged",
}


def install_result(package, status, message, detail="", reason=None):
    """Plain-language result for the agent; failure always says why and names the next fallback.

    `reason` is a stable code: not_available, tool_missing, timeout, no_download, corrupt,
    rejected. Retrying install_app never fixes any of them.
    """
    result = {"ok": status != "failed", "kind": "install_app", "package": package,
              "status": status, "message": message}
    if reason:
        result["reason"] = reason
    if detail:
        result["detail"] = detail[-1000:]
    if status == "failed":
        result["next_step"] = (
            "Do not call install_app again for this package. "
            + PLAY_STORE_NEXT_STEP.format(package=package)
        )
    return result


def apkeep_has_versions(output):
    """True/False when apkeep's `-l` output says whether the source carries the app; None if unclear."""
    match = re.search(r"Versions available for [^\r\n]+:\s*\|([^\r\n]*)", output)
    if not match:
        return None
    return bool(match[1].strip())


async def is_installed(slot, package):
    output = await core.shell(slot, "pm", "list", "packages", package)
    return f"package:{package}".encode() in output.split()


ANDROID_ABIS = ("arm64-v8a", "armeabi-v7a", "armeabi", "x86_64", "x86", "riscv64", "mips64", "mips")


def split_apks(xapk, directory, abis):
    """Extract an XAPK's APKs, keeping only the native-code split for the best device ABI."""
    with zipfile.ZipFile(xapk) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".apk")]
        abi_splits = {
            name: abi for name in names for abi in ANDROID_ABIS
            if Path(name).name.lower() == f"config.{abi.replace('-', '_')}.apk"
        }
        chosen = next(
            (n for abi in abis for n, split_abi in abi_splits.items() if split_abi == abi), None
        )
        paths = []
        for index, name in enumerate(names):
            if name in abi_splits and name != chosen:
                continue
            # Never trust archive paths: write each split under a generated name.
            target = Path(directory) / "splits" / f"{index}-{Path(name).name}"
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(archive.read(name))
            paths.append(str(target))
    return paths


async def install_app(slot, owner, package):
    try:
        validate_package(package)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    async with core.locks[slot]:
        core.check_owner(slot, owner)
        if await is_installed(slot, package):
            return install_result(package, "already_installed", f"{package} is already installed. Launch it by package name.")
        abis = (await core.shell(slot, "getprop", "ro.product.cpu.abilist")).decode().strip().split(",")
    # Ask the source first: an app it does not carry fails in a second, not after a download.
    with contextlib.suppress(Exception):
        listed, listed_err, _ = await core.process_result(
            settings.apkeep_path, "-a", package, "-d", "apk-pure", "-l", timeout=30
        )
        if apkeep_has_versions((listed + listed_err).decode(errors="replace")) is False:
            return install_result(
                package, "failed",
                f"{package} cannot be installed without Play Store: the direct download source "
                "(APKPure) has no builds of it, usually because the app is not distributed "
                "there or is restricted by region. The device and download tool are working.",
                reason="not_available",
            )
    with tempfile.TemporaryDirectory() as directory:
        try:
            stdout, stderr, _ = await core.process_result(
                # Without arch, APKPure may return a bundle with only a 32-bit ARM split.
                settings.apkeep_path, "-a", package, "-d", "apk-pure", "-o", f"arch={abis[0]}", directory,
                timeout=settings.install_timeout_seconds,
            )
        except FileNotFoundError:
            return install_result(
                package, "failed",
                "Direct installation is not set up on the device host: the APK downloader "
                "(apkeep) was not found. This is a setup problem, not a problem with the app.",
                reason="tool_missing",
            )
        except TimeoutError:
            return install_result(
                package, "failed",
                f"Downloading {package} timed out after {settings.install_timeout_seconds:.0f}s.",
                reason="timeout",
            )
        download = next(
            (f for f in sorted(Path(directory).iterdir()) if f.suffix in (".apk", ".xapk")), None
        )
        if not download:
            return install_result(
                package, "failed",
                f"The download tool ran but produced no APK for {package}. The source may be "
                "unreachable or blocking the request; detail has the tool's output.",
                (stdout + stderr).decode(errors="replace").strip(), reason="no_download",
            )
        async with core.locks[slot]:
            core.check_owner(slot, owner)
            try:
                apks = split_apks(download, directory, abis) if download.suffix == ".xapk" else [str(download)]
            except zipfile.BadZipFile:
                return install_result(
                    package, "failed", f"The downloaded file for {package} was corrupt.",
                    reason="corrupt",
                )
            for key, value in VERIFIER_SETTINGS.items():
                await core.command_result("-s", core.slots[slot], "shell", f"settings put global {key} {value}")
            # Record Play Store as the installer so the app behaves as a Play install.
            _, _, missing_play = await core.command_result("-s", core.slots[slot], "shell", "pm path com.android.vending")
            installer = [] if missing_play else ["-i", "com.android.vending"]
            verb = "install" if len(apks) == 1 else "install-multiple"
            stdout, stderr, code = await core.command_result(
                "-s", core.slots[slot], verb, "-r", "-g", *installer, *apks,
                timeout=settings.install_timeout_seconds,
            )
            if not code and await is_installed(slot, package):
                return install_result(package, "installed", f"{package} is installed. Launch it by package name.")
            output = (stdout + stderr).decode(errors="replace").strip()
            why = next((text for code, text in INSTALL_FAILURES.items() if code in output), None)
            return install_result(
                package, "failed",
                f"Android rejected the installation of {package}"
                + (f": {why}." if why else "; detail has Android's error."),
                output, reason="rejected",
            )
