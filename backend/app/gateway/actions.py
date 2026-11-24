"""Acting on a leased phone: the bridge inside its apps, the ops lane, and plain input (taps, keys, text, adb)."""

import contextlib
import re
import shlex
import typing

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..bridge.devices import DeviceError
from ..bridge.opscatalog import NOTIFIER
from ..models import Action
from . import core, devices, notices, scope, sleep
from .install import install_app

router = APIRouter(prefix="/devices/{slot}")


class ActionRequest(BaseModel):
    owner: str
    action: Action


class BridgeRequest(BaseModel):
    """One call on bridge.devices.XposedDevice: what the worker's ui.BridgeDevice needs."""

    owner: str
    method: typing.Literal["display", "bridges", "snapshot_display", "call"]
    cmd: str = ""
    process: str | None = None
    timeout: float = Field(default=10, gt=0, le=60)
    args: dict[str, typing.Any] = Field(default_factory=dict)
    # The call types one of the user's private values (args["text"]): every read after it is scrubbed of it.
    private: bool = False


class OpsRequest(BaseModel):
    """Ops for one device, in order; the first failure stops the rest. See bridge/opscatalog.py."""

    owner: str
    ops: list[dict[str, typing.Any]] = Field(min_length=1, max_length=8)


KEYS = {"back": 4, "home": 3, "recents": 187, "enter": 66, "delete": 67}


@router.post("/bridge")
async def bridge(slot: str, body: BridgeRequest):
    """Read and act through the bridge module inside the device's apps (app/bridge). Not
    serialized with the slot lock: a look asks every visible app at once, and each app's
    socket serializes its own requests."""
    core.require_slot(slot)
    core.check_owner(slot, body.owner)
    phone = core.phones[slot]
    try:
        if body.method == "call":
            if not body.cmd:
                raise HTTPException(422, "call needs cmd")
            if body.private and isinstance(body.args.get("text"), str):
                core.redactions[slot].add(body.args["text"])  # before the call: its own answer is scrubbed too
            # A lease limited to some apps (scope.py) acts only in them; the refusal reads like one of the bridge's.
            if refused := await scope.bridge_call(slot, body.owner, body.cmd, body.process, body.args):
                return refused
            result = await phone.call(body.cmd, process=body.process, timeout=body.timeout, **body.args)
        elif body.method == "snapshot_display":
            # The caller may pass the window list and bridge sockets it has just read (each is an adb round trip),
            # how to wait for the screen after an action and its event cursors (devices.snapshot_display).
            result = await phone.snapshot_display(**{k: v for k, v in body.args.items()
                                                     if k in ("visible", "bridges", "settle", "events_since")})
            # Each app's own snapshot is in "windows" already; sent twice it doubled the reply, the largest the
            # gateway sends (a few hundred KB for a Compose or web screen).
            result.pop("bridge_snapshots", None)
        else:
            result = await getattr(phone, body.method)()
    except DeviceError as exc:
        raise HTTPException(502, str(exc)) from None
    secrets = core.redactions[slot]
    return core.scrub(result, secrets) if secrets else result


@router.get("/notifications/events")
async def notification_events(slot: str, since: int = 0, wait: float = Query(default=0, ge=0, le=60)):
    """The notifications posted since event `since`, from the bridge module's listener: {seq, events}, each event
    with its app's name (`label`). Read-only, so no lease: the worker's notification intake (app/notifications.py)
    reads them while a task uses the phone, and while the phone sleeps (it is not woken for it). `since` past the
    last event returns none, with the current seq.

    With `wait` (seconds) the answer is held until the listener has something after `since` (notices.py hears it
    the moment a notification is posted), so the intake is told of one as it arrives. When nothing came, or the
    phone is not up, it answers {"seq": since or None, "events": []} without asking the phone."""
    core.require_slot(slot)
    if wait and not await notices.changed(slot, since, wait):
        return {"seq": since if notices.seq(slot) is not None else None, "events": []}
    phone = core.phones[slot]
    try:
        found = await phone.call("events", process=NOTIFIER, since=since)
        events = [e for e in found.get("events", []) if e.get("type") == "notification"]
        if any(not e.get("label") for e in events):  # an older listener: the names, from what is in the shade now
            with contextlib.suppress(DeviceError):
                shade = await phone.call("notifications", process=NOTIFIER, ongoing=True)
                labels = {n.get("app"): n.get("label") for n in shade.get("notifications", []) if n.get("label")}
                for e in events:
                    e["label"] = e.get("label") or labels.get(e.get("app")) or e.get("app")
    except DeviceError as exc:
        raise HTTPException(502, str(exc)) from None
    return core.scrub({"seq": found.get("seq", 0), "events": events}, core.redactions[slot])


@router.post("/ops")
async def run_ops(slot: str, body: OpsRequest):
    """Device commands beside the UI agent (settings, apps, notifications, emulator console, app preferences,
    waiting for the screen). Like the bridge, not serialized with the slot lock: they run while the UI agent works.
    Failures are results, not errors; the first one stops the rest."""
    core.require_slot(slot)
    core.check_owner(slot, body.owner)
    if scope.of(slot, body.owner) is None:
        results = await core.ops_lane[slot].run_many(body.ops)
    else:  # a lease limited to some apps: each op is checked as its turn comes (an earlier one may change what is in front)
        results = []
        for op in body.ops:
            if why := await scope.op(slot, body.owner, op):
                results.append({"op": op.get("op"), "ok": False, "refused": True, "summary": why})
            else:
                results.append(await core.ops_lane[slot].run(op))
            if not results[-1]["ok"]:
                break
    return core.scrub({"results": results}, core.redactions[slot])


@router.post("/actions")
async def perform(slot: str, body: ActionRequest):
    core.require_slot(slot)
    if body.action.kind == "install_app":
        core.check_owner(slot, body.owner)
        await scope.action(slot, body.owner, body.action)
        # Downloads take minutes; install_app holds the device lock only for its ADB steps.
        return await install_app(slot, body.owner, body.action.package)
    # The user's own tap, swipe or key on a sleeping phone wakes it and does nothing else: it would land on a
    # screen they have not seen yet. Anything else wakes it and goes ahead.
    asleep = sleep.asleep(slot)
    await sleep.wake(slot)
    if asleep and body.owner.startswith("manual:") and body.action.kind in ("tap", "swipe", *KEYS):
        core.check_owner(slot, body.owner)
        return {"ok": True, "kind": body.action.kind, "woke": True}
    if body.owner.startswith("manual:"):
        # The user's own taps may sign the phone in or out of Google: its accounts are read again shortly after.
        devices.recheck_accounts(slot)
    async with core.locks[slot]:
        core.check_owner(slot, body.owner)
        a = body.action
        await scope.action(slot, body.owner, a)  # a lease limited to some apps (scope.py)
        if a.kind == "adb_command":
            return await adb_command(slot, a.command)
        elif a.kind == "tap":
            await core.shell(slot, "input", "tap", a.x, a.y)
        elif a.kind == "swipe":
            await core.shell(slot, "input", "swipe", a.x, a.y, a.x2, a.y2, a.duration_ms)
        elif a.kind == "type_text":
            await type_text(slot, a.text, a.sensitive)
        elif a.kind == "google_signin":
            # The user signs in themselves, from the phone panel.
            await core.shell(
                slot, "am", "start", "-a", "android.settings.ADD_ACCOUNT_SETTINGS", "--esa", "account_types", "com.google"
            )
        elif a.kind in KEYS:
            await core.shell(slot, "input", "keyevent", KEYS[a.kind])
        return {"ok": True, "kind": a.kind}


CHAINED_ADB_SHELL = re.compile(r"(;|&&|\|\|)\s*adb\s+shell\s+")


def device_shell(expression):
    """What a host shell would hand the device for `adb shell <expression>`: the whole expression in one
    pair of quotes loses them, and `; adb shell` chains become plain `;` (the device has no adb)."""
    expression = CHAINED_ADB_SHELL.sub(lambda m: m[1] + " ", expression.strip())
    if len(expression) >= 2 and expression[0] == expression[-1] and expression[0] in "'\"":
        with contextlib.suppress(ValueError):
            if len(parts := shlex.split(expression)) == 1:
                return parts[0]
    return expression


async def adb_command(slot, command):
    """Run `adb ...` directly, never through a host shell, keeping an Android shell expression (its quotes
    and operators) intact."""
    match = re.fullmatch(r"\s*adb\s+(.+?)\s*", command, re.DOTALL)
    if not match:
        raise HTTPException(422, "command must start with adb and include arguments")
    adb_text = match[1]
    shell_match = re.fullmatch(r"shell\s+(.+)", adb_text, re.DOTALL)
    try:
        args = ["shell", device_shell(shell_match[1])] if shell_match else shlex.split(adb_text)
        if not args:
            raise ValueError("command must include ADB arguments")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    stdout, stderr, exit_code = await core.command_result("-s", core.slots[slot], *args)
    return {
        "ok": exit_code == 0, "kind": "adb_command", "exit_code": exit_code,
        "output": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    }


async def type_text(slot, text, sensitive):
    """Into the focused field: private codes and passwords, and the user's own typing on the phone panel."""
    if not text.isascii() or any(ord(c) < 32 for c in text):
        raise HTTPException(
            422,
            "ADB text entry supports printable ASCII. Enter Unicode manually with the Android keyboard.",
        )
    if sensitive and text:
        core.redactions[slot].add(text)
    # Separate literal percent signs so Android does not interpret a literal %s as a space.
    for chunk in re.split("(%)", text):
        if chunk:
            await core.shell(slot, "input", "text", chunk.replace(" ", "%s"))
