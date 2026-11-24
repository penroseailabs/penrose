"""The device gateway (`relay-server gateway`): the only service that talks to the phone, over adb. The API and
the worker reach it over HTTP with the per-launch GATEWAY_TOKEN; a durable exclusive lease says who holds a phone.

    devices.py   which phones are up, leases, location, the Google account, installed packages
    actions.py   acting on a leased phone: the bridge in its apps, the ops lane, taps, keys, text, adb
    install.py   install_app from APKPure
    signin.py    Google sign-in on the phone, driven for the user up to the prompt on their own phone
    screen.py    screenshots and the live view
    home.py      the Relay home app's search (apps, contacts, settings, Gmail), read-only
    web.py       Google's results, read in a hidden tab of the phone's Chrome
    play.py      Google Play's apps for a search, marked with the ones the user has (a hidden tab too)
    watch.py     each phone's state (offline, starting, ready...), kept from what adb and the phone report; a
                 phone that freezes is noticed and the desktop app is asked to restart it
    notices.py   each phone's notifications, heard as they arrive (a read held open on the module's listener)
    scope.py     a lease limited to some apps (a task a notification woke), enforced on every action
    sleep.py     the phone sleeps when nobody uses it, and wakes before anything does
    grants.py    notification and location permissions granted as apps are installed, so no dialog asks
    simple_signins.py  the screen lock and Chrome sync that Google Password Manager's sign-ins need
    saved_signins.py   which accounts have a saved passkey or password for the site or app on screen, read-only
    app_data.py  read-only queries on an app's own database on the phone (a copy), for Highways
    core.py      the devices and the adb helpers the routers share
"""

import contextlib
import hmac
import typing
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..config import settings
from ..models import Action
from . import (
    actions,
    app_data,
    call_audio,
    core,
    devices,
    files,
    grants,
    history,
    home,
    notices,
    play,
    saved_signins,
    screen,
    signin,
    simple_signins,
    sleep,
    watch,
    web,
)


@asynccontextmanager
async def lifespan(app):
    # Each phone's state, from adb's device list and the phone itself (watch.py). The desktop app starts the gateway
    # with PHONE_SETUP=1 while it is updating the phone, so no lease is granted before it says it is done.
    if settings.phone_setup:
        for slot in core.slots:
            watch.set_setup(slot, True)
    await watch.start()
    yield
    await grants.close()
    await notices.close()
    await watch.close()
    for hub in screen.hubs.values():
        if hub.task:
            hub.task.cancel()
            with contextlib.suppress(BaseException):
                await hub.task
    for job in signin.jobs.values():
        if job.task:
            job.task.cancel()
    for phone in core.phones.values():
        await phone.close()
    await sleep.close()


app = FastAPI(title="penrose device gateway", lifespan=lifespan)
for module in (devices, actions, files, home, web, play, history, screen, saved_signins, app_data, signin, simple_signins, call_audio):
    app.include_router(module.router)


# Requests that use the phone, by the path after /devices/{slot}/: they wake it first. Reads of its state
# (screenshots, the live view, accounts, the lease, sign-in status) and the computer's location updates do not;
# /actions decides itself (a user's tap on a sleeping phone only wakes it).
QUIET = {"actions", "location", "screenshot", "stream", "accounts", "packages", "notifications/events", "setup"}
# Held open for as long as the phone is up (app/notifications.py): not a use of the phone, so it neither wakes
# it nor starts the countdown to sleep again (which would keep it awake for good).
UNCOUNTED = {"notifications/events"}
# What uses the phone without a lease (the look-up's searches) waits this long for a phone that is starting or
# being restarted, as a lease does (devices.acquire), rather than run into a phone that cannot answer yet.
UNLEASED = {"home/search", "web/search", "play/search"}
READY_WAIT_SECONDS = 45


@app.middleware("http")
async def phone_activity(request: Request, call_next):
    """Wake the phone before a request that uses it; after any request on it, count down to sleep again
    (gateway/sleep.py; not while someone holds a lease)."""
    parts = request.url.path.strip("/").split("/", 2)
    if len(parts) < 3 or parts[0] != "devices" or parts[1] not in core.slots:
        return await call_next(request)
    slot, rest = parts[1], parts[2]
    if rest in UNLEASED and not await watch.until_ready(slot, READY_WAIT_SECONDS):
        return JSONResponse({"detail": "The phone is starting. Try again in a minute."}, status_code=503)
    if rest not in QUIET and (request.method != "GET" or rest == "saved-signins"):
        await sleep.wake(slot)
    try:
        return await call_next(request)
    finally:
        if rest not in UNCOUNTED:
            sleep.idle(slot)


@app.middleware("http")
async def require_token(request: Request, call_next):
    """With GATEWAY_TOKEN set (always, in the desktop app), only the API and worker, which know it, may drive
    the phone."""
    if settings.gateway_token and not hmac.compare_digest(
        request.headers.get("x-gateway-token", "").encode(), settings.gateway_token.encode()
    ):
        return Response("Gateway token required", status_code=401)
    return await call_next(request)


@app.get("/health")
async def health():
    # Clients compare action kinds to detect a gateway running outdated code.
    return {
        "status": "ok", "slots": len(core.slots), "user_id": settings.user_id,
        "actions": list(typing.get_args(Action.model_fields["kind"].annotation)),
        "bridge": True,
    }
