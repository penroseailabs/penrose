"""Which phones are up, who holds each (a durable exclusive lease), and where they are.

Whether a phone is up is the gateway's own record (watch.py), kept as the phone changes: listing the phones and
taking a lease cost no adb call. A lease that finds no free phone may wait for one (`wait`): it is answered the
moment the phone becomes ready or is released."""

import asyncio
import contextlib
import json
import typing

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..android import TIME_ZONE, google_accounts
from ..bridge.ops import PACKAGE, OpError
from ..config import settings
from . import core, scope, signin, sleep, watch

router = APIRouter()


class LeaseRequest(BaseModel):
    owner: str
    slot: str | None = None
    # Seconds to wait for a free, ready phone before answering 503 (none ready) or 409 (busy). 0: answer now.
    wait: float = Field(default=0, ge=0, le=60)
    # The package IDs this lease may use (scope.py): a task a notification woke. None: any app. Each request sets
    # it, so the same owner leasing again without it lifts the limit (the user wrote to the task).
    apps: list[str] | None = Field(default=None, max_length=12)


class LocationRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float = Field(default=10, gt=0, le=10000)
    # "computer": the window's regular fix. "phone": the user's own phone, sent with a message from the
    # companion app; it holds until a message comes from the computer again (release_location).
    source: typing.Literal["computer", "phone"] = "computer"


class TimeZoneRequest(BaseModel):
    zone: str = Field(pattern=TIME_ZONE, max_length=64)


class SetupRequest(BaseModel):
    active: bool


# The computer's location per slot, re-applied when the emulator reboots.
locations = {}  # the fix each phone has now
computer_fixes = {}  # the computer's latest fix, kept while the user's phone's fix holds
phone_held = set()  # slots on the user's phone's fix (a message came from the companion app)
gps_cleared = set()  # slots whose leftover mock providers were removed
# Google accounts on each phone, read when it becomes ready and after anything that may change them (sign-in,
# the user's own taps); pushed with the device list. Missing: not read yet.
accounts_seen = {}


def leases():
    return {r["slot"]: r["owner"] for r in core.store.rows("SELECT * FROM leases")}


def device_views():
    held = leases()
    return [
        {
            "id": slot,
            **watch.view(slot),
            "owner": held.get(slot),
            "fps": settings.stream_fps,
            "asleep": sleep.asleep(slot),
            "accounts": accounts_seen.get(slot),
            # Relay's guided Google sign-in on this phone (gateway/signin.py), while there is one.
            "signin": signin.jobs[slot].view() if slot in signin.jobs else None,
            **core.dimensions.get(slot, {}),
        }
        for slot in core.slots
    ]


@router.get("/devices")
async def device_list():
    return device_views()


@router.get("/devices/updates")
async def device_updates(request: Request):
    """The device list each time it changes (a phone's state, a lease, setup, its Google accounts), as server-sent
    events; the first one at once. The API holds this open and passes it on to the window."""

    async def generate():
        last = None
        while not await request.is_disconnected():
            news = watch.listen()
            views = json.dumps(device_views())
            if views != last:
                last = views
                yield f"data: {views}\n\n"
            if not await watch.wait(news, 15):
                yield ": keepalive\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


async def became_ready(slot):
    """A phone has just come up (or back): the countdown to sleep allows for the desktop app's update, the computer's
    location goes back in, and its Google accounts are read. Its sleep state is forgotten: the first wake after a
    boot checks everything, Chrome's browser too, as for a phone just started on demand."""
    sleep.forget(slot)
    sleep.idle(slot, settings.phone_sleep_seconds + sleep.STARTUP_GRACE_SECONDS)
    if slot in locations:
        with contextlib.suppress(Exception):
            await apply_location(slot, locations[slot])
    await read_accounts(slot)


watch.on_ready(became_ready)
signin.ended.append(lambda: [recheck_accounts(slot, delay=0.5) for slot in core.slots])


async def read_accounts(slot):
    """Read the phone's Google accounts again; pushed to the window when they changed."""
    with contextlib.suppress(Exception):
        output = (await core.adb(slot, "shell", "dumpsys account", timeout=8)).decode(errors="replace")
        found = google_accounts(output)
        if accounts_seen.get(slot) != found:
            accounts_seen[slot] = found
            watch.notify()


def recheck_accounts(slot, delay=2.0):
    """After something that may have signed the phone in or out (the user's own taps, a sign-in): read again soon."""
    async def later():
        await asyncio.sleep(delay)
        await read_accounts(slot)

    if watch.ready(slot):
        asyncio.create_task(later())


@router.put("/devices/{slot}/setup")
async def set_setup(slot: str, body: SetupRequest):
    """The desktop app is updating the phone (provision device-ready): leases wait until it says it is done."""
    core.require_slot(slot)
    watch.set_setup(slot, body.active)
    return watch.view(slot)


def _grant(body):
    """A lease for `body.owner` now, or None. Raises 503 when no phone is ready (none could be leased)."""
    candidates = [slot for slot in core.slots if watch.ready(slot) and body.slot in (None, slot)]
    if not candidates:
        raise HTTPException(503, "The phone is starting or reconnecting.")
    # `kept`: the owner already held this phone, so nobody else has used it since.
    kept = bool(core.store.rows("SELECT 1 FROM leases WHERE owner=?", (body.owner,)))
    slot = core.store.lease(candidates, body.owner)
    return {"slot": slot, "kept": kept} if slot else None


@router.post("/leases")
async def acquire(body: LeaseRequest):
    if body.slot is not None:
        core.require_slot(body.slot)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + body.wait
    while True:
        news = watch.listen()  # before deciding, so a release or a phone coming up meanwhile still wakes us
        try:
            granted = _grant(body)
            problem = None if granted else HTTPException(409, "The phone is busy")
        except HTTPException as exc:
            granted, problem = None, exc
        if granted:
            break
        if not await watch.wait(news, deadline - loop.time()):
            raise problem
    slot = granted["slot"]
    scope.keep(body.owner, body.apps)
    if not granted["kept"]:
        watch.notify()  # the device list changed hands
    # Held, it stays awake. A user's own tap leases it only for that tap, and decides itself (actions.py).
    if body.owner.startswith("manual:"):
        sleep.cancel(slot)
    else:
        await sleep.wake(slot)
    return granted


@router.get("/leases/{slot}")
async def lease_owner(slot: str):
    core.require_slot(slot)
    rows = core.store.rows("SELECT owner FROM leases WHERE slot=?", (slot,))
    return {"owner": rows[0]["owner"] if rows else None}


@router.delete("/leases/{owner:path}")
async def release(owner: str):
    rows = core.store.rows("SELECT slot FROM leases WHERE owner=?", (owner,))
    if rows and rows[0]["slot"] in core.locks:
        async with core.locks[rows[0]["slot"]]:
            core.store.execute("DELETE FROM leases WHERE owner=?", (owner,))
    else:
        core.store.execute("DELETE FROM leases WHERE owner=?", (owner,))
    if rows:
        sleep.idle(rows[0]["slot"])
        watch.notify()  # a lease waiting for this phone takes it now
    return {"released": True}


class WakeRequest(BaseModel):
    # The user is about to use penrose: keep the phone awake this long (sleep.keep_awake).
    for_seconds: float = Field(default=0, ge=0, le=600)


@router.post("/devices/{slot}/wake")
async def wake(slot: str, body: WakeRequest | None = None):
    """Wake the phone: the user pressed it in the window while it slept, or is about to use penrose
    (`for_seconds`). The middleware does it; after `phone_sleep_seconds` without use (and not before
    `for_seconds` have passed) it sleeps again."""
    core.require_slot(slot)
    if body and body.for_seconds:
        sleep.keep_awake(slot, body.for_seconds)
    return {"asleep": sleep.asleep(slot)}


async def apply_location(slot, fix):
    """Put the phone where the user's computer is, through the emulator's own GPS hardware
    (`emu geo fix`). Apps read it as a real satellite fix: unlike Android's test providers,
    it is never flagged as a mock location."""
    if slot not in gps_cleared:
        # Test providers left by older versions would shadow the GPS. Removing one needs the mock app-op,
        # which is handed back afterwards; on a phone without them this does nothing.
        with contextlib.suppress(HTTPException):
            await core.adb(slot, "shell", "appops set com.android.shell android:mock_location allow; "
                           "for p in gps network; do cmd location providers remove-test-provider $p >/dev/null 2>&1; done; "
                           "appops set com.android.shell android:mock_location default; true", include_stderr=True)
        gps_cleared.add(slot)
    output = (await core.adb(slot, "emu", "geo", "fix", f"{fix.lng:.7f}", f"{fix.lat:.7f}", "0", "9",
                             include_stderr=True)).decode(errors="replace")
    if "OK" not in output:
        raise HTTPException(503, f"The emulator did not take the location: {output[:200]}")


@router.post("/devices/{slot}/location")
async def set_location(slot: str, body: LocationRequest):
    """The phone goes where the user is: the computer's position, or the user's phone's while their latest
    message came from it. The computer's regular fix waits, kept, while the phone's holds."""
    core.require_slot(slot)
    if body.source == "computer":
        computer_fixes[slot] = body
        if slot in phone_held:
            return {**fix_view(slot), "held": True}
    async with core.locks[slot]:
        await apply_location(slot, body)
    locations[slot] = body
    if body.source == "phone":
        phone_held.add(slot)
    return {"lat": body.lat, "lng": body.lng, "accuracy": body.accuracy}


@router.post("/devices/{slot}/location/release")
async def release_location(slot: str):
    """A message came from the computer: the phone goes back to the computer's position."""
    core.require_slot(slot)
    if slot not in phone_held:
        return fix_view(slot)
    phone_held.discard(slot)
    if fix := computer_fixes.get(slot):
        async with core.locks[slot]:
            await apply_location(slot, fix)
        locations[slot] = fix
    return fix_view(slot)


@router.post("/devices/{slot}/timezone")
async def set_timezone(slot: str, body: TimeZoneRequest):
    """Put the phone's clock on the user's time zone, as their browser or phone reports it. The emulator boots on
    the computer's, and a hosted Relay's is UTC. Automatic detection goes off, or the emulated mobile network
    sets the computer's back within seconds. Needs root: the shell may not suggest a time zone."""
    core.require_slot(slot)
    script = ("cmd time_zone_detector set_auto_detection_enabled false; "
              f"cmd time_zone_detector suggest_manual_time_zone --zone_id {body.zone} >/dev/null; "
              "getprop persist.sys.timezone")
    async with core.locks[slot]:
        output = (await core.adb(slot, "shell", f"su -c '{script}'", include_stderr=True)).decode(errors="replace")
    if output.strip().splitlines()[-1:] != [body.zone]:
        raise HTTPException(503, f"The phone did not take the time zone: {output[:200]}")
    return {"zone": body.zone}


AUTOSCOPE = "sh /data/adb/modules/relay_autoscope/service.sh once"
BRIDGE_SCOPE = "sh /data/adb/modules/zygisk_vector/cli --json scope ls app.relay.bridge"


@router.post("/devices/{slot}/bridge-scope/{package}")
async def bridge_scope(slot: str, package: str):
    """An app opened without the bridge: run relay-autoscope's scope pass now (android/autoscope/service.sh), which
    puts every app with a launcher icon and every user-installed app in the bridge's scope, then say whether
    `package` is in it. Its pass at boot can miss apps (a phone that boots locked lists only the few apps that run
    before the unlock), and it runs once per boot. The app loads the bridge from its next start. Needs root."""
    core.require_slot(slot)
    if not PACKAGE.fullmatch(package):
        raise HTTPException(422, "package must be an Android package ID")
    async with core.locks[slot]:
        output = (await core.adb(slot, "shell", f"su -c '{AUTOSCOPE}; {BRIDGE_SCOPE}'", timeout=120)).decode(errors="replace")
    return {"scoped": f'"{package}"' in output}


def fix_view(slot):
    fix = locations.get(slot)
    return {"lat": fix.lat, "lng": fix.lng, "accuracy": fix.accuracy} if fix else {}


@router.get("/devices/locations")
async def all_locations():
    """The fix each phone was given, by slot (empty for a phone that was given none): what the agents' briefs
    say about where the phone is."""
    return {slot: fix_view(slot) for slot in core.slots}


@router.get("/devices/{slot}/location")
async def get_location(slot: str):
    core.require_slot(slot)
    return fix_view(slot)


@router.post("/devices/{slot}/location/refresh")
async def refresh_location(slot: str):
    """Send the phone its fix again (before a step in a location-aware app): a fix is delivered to an app
    the moment it listens, so a fresh one leaves no doubt about a stale position."""
    core.require_slot(slot)
    if fix := locations.get(slot):
        async with core.locks[slot]:
            await apply_location(slot, fix)
    return fix_view(slot)


@router.get("/devices/{slot}/accounts")
async def accounts(slot: str):
    """Google accounts signed in on the device (`dumpsys account`): read-only, so it needs no lease and
    waits for no action. The app's sign-in gate and the agents' briefs read it."""
    core.require_slot(slot)
    output = (await core.adb(slot, "shell", "dumpsys account", timeout=8)).decode(errors="replace")
    found = google_accounts(output)
    if accounts_seen.get(slot) != found:
        accounts_seen[slot] = found
        watch.notify()
    return {"accounts": found}


@router.get("/devices/{slot}/packages")
async def packages(slot: str, owner: str):
    """Read-only third-party package list; not an agent action."""
    core.require_slot(slot)
    async with core.locks[slot]:
        core.check_owner(slot, owner)
        output = (await core.adb(slot, "shell", "pm list packages -3")).decode(errors="replace")
    return {"packages": sorted(line.removeprefix("package:") for line in output.split() if line.startswith("package:"))}


@router.get("/devices/{slot}/apps")
async def apps(slot: str, owner: str):
    """Every app with a launcher icon, by name: {"apps": [{"name", "package", "shortcuts"}]}, Google's and the
    system's among them, each with the shortcuts its icon offers (the ops lane's `launchable`: the home app's list,
    or the package manager's IDs alone when it does not answer). Read-only; not an agent action."""
    core.require_slot(slot)
    async with core.locks[slot]:
        core.check_owner(slot, owner)
        try:
            return {"apps": await core.ops_lane[slot].launchable()}
        except OpError as exc:
            raise HTTPException(503, f"The phone's apps could not be listed: {exc}") from None
