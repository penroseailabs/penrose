"""Runtime permissions granted as apps arrive, so Android never shows its permission dialog for them.

Every app installed on the phone (from the Play Store, install_app or an update) gets the permission groups in
`settings.auto_grant` (notifications and location while in use, by default) as its folder lands in /data/app: a watch
that runs on the phone itself (inotifyd), so each install is pushed here, nothing is checked on a timer. When the phone
comes up, the apps already on it get them too, so an app installed while the gateway was down is not missed.

An app gets only the permissions its manifest asks for (`pm grant` refuses the rest), and it is kept off Android's
auto-revoke for apps unused for months, which would take them back. Anything outside the set (camera, contacts, ...)
is still the app's to ask for, and the agent's `grant` op is there when a task needs one.

The same pass keeps the apps able to hear their notifications. Android delivers no push to an app in its
"stopped" state (force-stopped and not opened since: Play services logs "Failed to broadcast to stopped app"), so
that state is cleared; and the browser is granted notifications too, for the websites that send them."""

import asyncio
import contextlib
import logging
import re

from ..bridge.ops import PERMISSIONS
from ..config import settings
from . import core, watch

log = logging.getLogger("gateway.grants")
tasks: dict[str, asyncio.Task] = {}
INSTALL_DIR = "/data/app"
BROWSER = "com.android.chrome"  # part of the phone's image, so not among the installed apps
PACKAGE_DIR = re.compile(r"^([A-Za-z][\w.]*)-[\w=-]+$")  # com.example.app-Ab3dE...== inside an install's folder
REGISTER_TRIES = 20  # the folder lands before the package manager has the app: ask again every half second


def wanted():
    """The full permission names of the groups in settings.auto_grant ("" grants nothing)."""
    names = []
    for group in settings.auto_grant.split(","):
        names += ["android.permission." + p for p in PERMISSIONS.get(group.strip().lower(), [])]
    return list(dict.fromkeys(names))


def grant_script(packages):
    """One shell run for all of them: each permission an app did not ask for fails quietly."""
    return ("for pkg in " + " ".join(packages) + "; do for p in " + " ".join(wanted()) + "; do "
            "pm grant $pkg $p 2>/dev/null; done; appops set $pkg AUTO_REVOKE_PERMISSIONS_IF_UNUSED ignore 2>/dev/null; "
            "cmd package unstop $pkg >/dev/null 2>&1; done; true")


def watch_command():
    """The phone's side of the watch. It ends any earlier one first: ending adb on this computer ends its shell on
    the phone but not the root inotifyd under Magisk's su, so each restart of the gateway left one behind."""
    return f"su -c 'pkill -f \"^inotifyd - {INSTALL_DIR} \"; exec inotifyd - {INSTALL_DIR}:ny'"


def new_install(line):
    """The folder an install moved into /data/app (~~...==), from one inotifyd line, or None."""
    parts = line.strip().split("\t")
    if len(parts) == 3 and parts[0] in ("n", "y") and parts[2].startswith("~~"):
        return parts[2]
    return None


async def grant(slot, packages):
    if packages and wanted():
        await core.adb(slot, "shell", grant_script(packages), timeout=20 + 2 * len(packages))


async def granted_on_arrival(slot, folder):
    """Grant to the app in a new install folder once the package manager knows it."""
    listing = (await core.adb(slot, "shell", f"su -c 'ls {INSTALL_DIR}/{folder}'", timeout=10)).decode(errors="replace")
    packages = [m.group(1) for name in listing.split() if (m := PACKAGE_DIR.match(name))]
    for package in packages:
        for _ in range(REGISTER_TRIES):
            if b"package:" in await core.adb(slot, "shell", f"pm path {package}", timeout=10):
                break
            await asyncio.sleep(0.5)
        await grant(slot, [package])
        log.info("%s: granted %s to newly installed %s", slot, settings.auto_grant, package)


async def follow(slot):
    """Grant to the apps already there, then to each one installed, for as long as the phone stays up."""
    third_party = (await core.adb(slot, "shell", "pm list packages -3", timeout=15)).decode(errors="replace")
    await grant(slot, [*re.findall(r"package:(\S+)", third_party), BROWSER])
    proc = await asyncio.create_subprocess_exec(settings.adb_path, "-s", core.slots[slot], "shell", watch_command(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async for line in proc.stdout:  # ends when the phone goes down or restarts
            if folder := new_install(line.decode(errors="replace")):
                try:
                    await granted_on_arrival(slot, folder)
                except Exception as exc:  # one install's grant failing must not end the watch
                    log.warning("%s: could not grant to the app in %s: %s", slot, folder, exc)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def became_ready(slot):
    """Start (or restart) the slot's watch; it runs beside the rest of the phone's start, never holding it up."""
    if not wanted():
        return
    if task := tasks.pop(slot, None):
        task.cancel()

    async def run():
        try:
            await follow(slot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("%s: permission grants on install are off until the phone restarts: %s", slot, exc)

    tasks[slot] = asyncio.create_task(run())


async def close():
    for task in tasks.values():
        task.cancel()
    for task in tasks.values():
        with contextlib.suppress(BaseException):
            await task
    tasks.clear()


watch.on_ready(became_ready)
