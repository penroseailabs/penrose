"""A lease that may use only some apps.

A task that a notification woke (app/watches.py) works on the phone with nobody watching, started by something
an app showed. So its lease names the apps it may use (`apps`, package IDs, kept with the lease), and the gateway,
the one place every action passes, holds it to them, whatever the agents were told or talked into:

- an action on the screen (a tap, typed text, a click through the bridge) goes ahead only while one of those
  apps, or Android's own (the home screen, a permission dialog, the keyboard), is in front;
- an app is opened, stopped or granted a permission only when it is one of them;
- a raw adb command, an install, and the device's settings are refused.

So such a task can end up looking at another app (a link opened it, a notification was tapped), but it cannot
do anything there except leave. Reads are never refused. A lease without `apps` is not limited.
"""

import json

from fastapi import HTTPException

from ..bridge.devices import READ_ONLY as READ_CALLS
from ..bridge.opscatalog import NOTIFIER
from ..bridge.opscatalog import READ_ONLY as READ_OPS
from . import core, watch

# Android's own, where a task passes through whatever it works in: the home screens, the system's dialogs
# (permissions, "Open with"), the status bar and shade, and the keyboard.
ALWAYS = {
    watch.HOME, "com.google.android.apps.nexuslauncher", "android", "com.android.systemui",
    "com.google.android.permissioncontroller", "com.android.permissioncontroller", "com.google.android.inputmethod.latin",
}
# Ops that name the app they act on: allowed for an app of the lease.
PACKAGE_OPS = {"launch", "stop", "grant", "revoke", "open", "prefs"}
# Ops that act on whatever is in front.
FRONT_OPS = {"set_value", "scroll_to"}
# Ops that change nothing an app holds: the shade, the keys, and what every task sets before it works.
FREE_OPS = {*READ_OPS, "shade", "hide_keyboard", "unlock", "key", "notification_tap", "notification_dismiss",
            "animations", "rotation"}
# Actions (models.Action) that only leave where the task is.
FREE_ACTIONS = {"back", "home", "recents"}


def of(slot, owner):
    """The package IDs `owner`'s lease on the phone is limited to, or None when it is not limited."""
    rows = core.store.rows("SELECT apps FROM leases WHERE slot=? AND owner=?", (slot, owner))
    return set(json.loads(rows[0]["apps"])) if rows and rows[0]["apps"] else None


def keep(owner, apps):
    """Set (or, with none, lift) the limit of `owner`'s lease."""
    core.store.execute("UPDATE leases SET apps=? WHERE owner=?", (json.dumps(sorted(set(apps))) if apps else None, owner))


def refusal(allowed, what):
    return (f"Not done: {what}. A notification started this task, so it may only use {', '.join(sorted(allowed))}. "
            "Leave with home or back and do the rest there; what cannot be done in those apps is reported to the "
            "user, not worked around.")


async def front(slot, allowed):
    """Why nothing may be done on the screen now, or None: an app outside the lease is in front."""
    package = await core.phones[slot].foreground()
    if package is None:
        return "Not done: the app in front could not be told. Look at the screen again."
    if package in allowed or package in ALWAYS:
        return None
    return refusal(allowed, f"{package} is in front")


async def action(slot, owner, act):
    """Refuse (422, told to the agent) an action a limited lease may not take."""
    allowed = of(slot, owner)
    if allowed is None or act.kind in FREE_ACTIONS:
        return
    if act.kind in ("adb_command", "install_app", "google_signin"):
        raise HTTPException(422, refusal(allowed, "adb commands, installs and account sign-ins are not open to it"))
    if why := await front(slot, allowed):
        raise HTTPException(422, why)


async def bridge_call(slot, owner, cmd, process, args):
    """A refusal as the bridge would answer one ({"ok": False, "refused": True, "error"}), or None to go ahead."""
    allowed = of(slot, owner)
    if allowed is None or cmd in READ_CALLS:
        return None
    handle = str(args.get("h", ""))
    if process == NOTIFIER:
        if cmd in ("notification_click", "notification_dismiss"):
            parts = str(args.get("key", "")).split("|")
            package = parts[1] if len(parts) > 1 else ""
            why = None if package in allowed else refusal(allowed, f"the notification is from {package or 'another app'}")
        else:
            why = None if cmd == "notifications" else refusal(allowed, f"{cmd} is not open to it")
    elif handle.startswith("ua:") or not process:
        why = await front(slot, allowed)
    else:
        package = process.split(":")[0]
        why = None if package in allowed or package in ALWAYS else refusal(allowed, f"{package} is not one of them")
    return {"ok": False, "refused": True, "error": why, "_process": process} if why else None


async def op(slot, owner, op):
    """Why a limited lease may not run this op, or None."""
    allowed = of(slot, owner)
    if allowed is None:
        return None
    name = op.get("op")
    if name == "prefs" and str(op.get("action")).lower() not in ("set", "remove"):
        return None
    if name in FREE_OPS:
        return None
    if name in FRONT_OPS:
        return await front(slot, allowed)
    if name in PACKAGE_OPS:
        package = str(op.get("package") or "")
        if package in allowed:
            return None
        return refusal(allowed, f"{name} needs `package`, one of them" if not package else f"{package} is not one of them")
    return refusal(allowed, f"{name} changes the device, which is not open to it")
