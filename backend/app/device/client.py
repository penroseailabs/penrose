"""The device gateway, as the worker reaches it over HTTP: leases, the bridge, ops, actions, screenshots.
The gateway itself is app/gateway, the only service with adb."""

import contextlib

import httpx

from ..config import settings
from ..errors import DeviceError
from .driver import BridgeDevice

# A look-up's search: the gateway waits up to 45 s for a phone that is starting or being restarted
# (gateway/__init__.py READY_WAIT_SECONDS), then searches (up to 35 s for Google).
SEARCH_TIMEOUT = 90


class Slot(str):
    """A leased slot. `kept` is True when the owner already held it, so nobody else used the phone since."""

    kept = True


class Devices:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=settings.emulator_gateway_url, headers=settings.gateway_headers, timeout=40)

    async def close(self):
        await self.client.aclose()

    async def acquire(self, owner, wait=0, apps=None):
        """Lease a phone; with `wait`, the gateway answers as soon as one is free and ready, within that many seconds.
        `apps`: the package IDs the lease is limited to (gateway/scope.py), None for any; leasing again sets it anew."""
        try:
            r = await self.client.post("/leases", json={"owner": owner, "wait": wait, "apps": apps or None}, timeout=wait + 15)
        except httpx.TransportError:
            return None
        if r.status_code in (409, 502, 503, 504):
            return None
        if r.is_error:
            raise DeviceError("The cloud device service could not assign this profile's emulator.")
        body = r.json()
        slot = Slot(body["slot"])
        slot.kept = body.get("kept", True)
        return slot

    async def phones(self):
        """Every phone as the gateway lists it ({id, connected, owner, ...}); [] when it cannot say."""
        try:
            r = await self.client.get("/devices")
        except httpx.TransportError:
            return []
        return r.json() if r.is_success else []

    async def holders(self):
        """{slot: owner} for each connected phone that someone holds; empty when the gateway cannot say."""
        try:
            r = await self.client.get("/devices")
        except httpx.TransportError:
            return {}
        if r.is_error:
            return {}
        return {d["id"]: d["owner"] for d in r.json() if d.get("connected") and d.get("owner")}

    async def release(self, owner):
        r = await self.client.delete(f"/leases/{owner}")
        r.raise_for_status()

    async def screenshot(self, slot, width=None):
        """A small JPEG of the screen, or None when the gateway cannot supply one."""
        try:
            r = await self.client.get(
                f"/devices/{slot}/screenshot", params={"width": width or settings.screenshot_width}
            )
        except httpx.TransportError:
            return None
        # A gateway started before this feature ignores ?width= and answers with the full PNG.
        if r.is_error or r.headers.get("content-type") != "image/jpeg":
            return None
        return r.content

    def call_audio(self, slot, owner):
        """The phone's audio for a call (gateway/call_audio.py), as an async WebSocket context manager."""
        from urllib.parse import quote

        from websockets.asyncio.client import connect

        base = settings.emulator_gateway_url.replace("http", "ws", 1).rstrip("/")
        return connect(f"{base}/devices/{slot}/call-audio?owner={quote(owner)}",
                       additional_headers=settings.gateway_headers, max_size=None, open_timeout=30)

    def device(self, slot, owner):
        """The leased device as the screen driver uses it."""
        return BridgeDevice(self, slot, owner)

    async def bridge(self, slot, owner, method, **body):
        """One call on the gateway's bridge driver (gateway.bridge). Never retried here: a
        call may be an action inside the app."""
        # The gateway retries a bridge call once and a snapshot visits every app, so give it
        # time to answer with its own error instead of cutting it off at the default.
        timeout = 90 if method == "snapshot_display" else 2 * body.get("timeout", 10) + 15
        try:
            r = await self.client.post(
                f"/devices/{slot}/bridge", json={"owner": owner, "method": method, **body},
                timeout=timeout,
            )
        except httpx.TransportError as exc:
            raise DeviceError(f"The device gateway did not answer ({type(exc).__name__}).") from None
        if r.is_error:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
            if isinstance(detail, str):
                raise DeviceError(detail[:500])
            raise DeviceError(f"The device gateway could not reach the app's bridge (HTTP {r.status_code}).")
        return r.json()

    async def packages(self, slot, owner):
        r = await self.client.get(f"/devices/{slot}/packages", params={"owner": owner})
        if r.is_error:
            raise DeviceError(f"Unable to list installed packages (HTTP {r.status_code}).")
        return r.json()["packages"]

    async def apps(self, slot, owner):
        """Every app with a launcher icon ([{"name", "package", "shortcuts"}], by name): Google's and the system's
        too, each with the shortcuts its icon offers. `name` is empty when the phone's home app could not be asked."""
        r = await self.client.get(f"/devices/{slot}/apps", params={"owner": owner}, timeout=40)
        if r.is_error:
            raise DeviceError(f"Unable to list installed apps (HTTP {r.status_code}).")
        return r.json()["apps"]

    async def notification_events(self, slot, since, wait=0):
        """The phone's notifications posted since event `since` ({"seq", "events"}), read without a lease; None
        when the gateway or the phone's listener cannot say. With `wait` the gateway holds the answer until there
        is one, at most that many seconds ("seq" None: the phone is not up)."""
        try:
            r = await self.client.get(f"/devices/{slot}/notifications/events", params={"since": since, "wait": wait},
                                      timeout=wait + 20)
            return r.json() if r.is_success else None
        except (httpx.TransportError, ValueError):
            return None

    async def location(self, slot, refresh=False):
        """Where the phone is: the fix the gateway gave it ({"lat", "lng", "accuracy"}), or None when it was
        given none or the gateway cannot say. With `refresh`, the fix is sent to the phone again first."""
        try:
            if refresh:
                r = await self.client.post(f"/devices/{slot}/location/refresh")
            else:
                r = await self.client.get(f"/devices/{slot}/location")
            return (r.json() or None) if r.is_success else None
        except (httpx.TransportError, ValueError):
            return None

    async def set_timezone(self, slot, zone):
        """Put the phone's clock on this time zone; False when the gateway could not."""
        try:
            r = await self.client.post(f"/devices/{slot}/timezone", json={"zone": zone}, timeout=30)
            return r.is_success
        except httpx.TransportError:
            return False

    async def locations(self):
        """The fix of every phone, by slot; {} when the gateway cannot say."""
        try:
            r = await self.client.get("/devices/locations")
            return r.json() if r.is_success else {}
        except (httpx.TransportError, ValueError):
            return {}

    async def accounts(self, slot, owner):
        """Google accounts on the device, or None when the gateway cannot say."""
        try:
            r = await self.client.get(f"/devices/{slot}/accounts")
            return r.json()["accounts"] if r.is_success else None
        except (httpx.TransportError, ValueError, KeyError):
            return None

    async def saved_signins(self, slot, url="", package=""):
        """Account names with a saved passkey or password for the page at `url` (else the app `package`):
        {"site", "passkeys", "passwords"}. Read-only: no lease. None when the gateway cannot say."""
        try:
            r = await self.client.get(f"/devices/{slot}/saved-signins", params={"url": url, "package": package}, timeout=40)
            return r.json() if r.is_success else None
        except (httpx.TransportError, ValueError):
            return None

    async def app_db(self, slot, package, file, queries):
        """Read-only queries on an app's own database on the phone, as of now (gateway/app_data.py): {"ok", "file",
        "stamp", "utc_offset", "results": {name: {"columns", "rows"} or {"error"}}}. No lease. Raises DeviceError
        when the gateway cannot read it."""
        try:
            r = await self.client.post(f"/devices/{slot}/app-db",
                                       json={"package": package, "file": file, "queries": queries}, timeout=60)
        except httpx.TransportError:
            raise DeviceError("The device gateway did not answer.") from None
        if r.is_error:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
            raise DeviceError(detail if isinstance(detail, str) else f"The app's data could not be read (HTTP {r.status_code}).")
        body = r.json()
        if not body.get("ok"):
            raise DeviceError(body.get("error") or "The app's data could not be read.")
        return body

    async def wake(self, slot, for_seconds):
        """Wake the phone and keep it awake `for_seconds` (gateway/sleep.py keep_awake): a task is starting and will
        likely search or use it. Never raises: the phone is only being got ready."""
        with contextlib.suppress(httpx.HTTPError):
            await self.client.post(f"/devices/{slot}/wake", json={"for_seconds": for_seconds}, timeout=15)

    async def home_search(self, slot, queries, email=True):
        """The Relay home app's search for each query ([{"query", "found"} or {"query", "error"}]): what is on the
        phone (apps and their screens, contacts, settings) and, with `email`, in Gmail. Read-only: no lease. Raises
        DeviceError when the gateway or the home app does not answer."""
        try:
            r = await self.client.post(f"/devices/{slot}/home/search", json={"queries": queries, "email": email}, timeout=SEARCH_TIMEOUT)
        except httpx.TransportError:
            raise DeviceError("The device gateway did not answer.") from None
        if r.is_error:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
            raise DeviceError(detail if isinstance(detail, str) else f"The phone could not be searched (HTTP {r.status_code}).")
        return r.json()["results"]

    async def web_search(self, slot, queries):
        """Google's results for each query, read in the phone's own Chrome ([{"query", "found"} or {"query", "error"}]):
        the AI Overview, an answer card and the top results. Read-only: no lease, nothing on screen. Raises
        DeviceError when the gateway or Chrome does not answer."""
        try:
            r = await self.client.post(f"/devices/{slot}/web/search", json={"queries": queries}, timeout=SEARCH_TIMEOUT)
        except httpx.TransportError:
            raise DeviceError("The device gateway did not answer.") from None
        if r.is_error:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
            raise DeviceError(detail if isinstance(detail, str) else f"Google could not be searched (HTTP {r.status_code}).")
        return r.json()["results"]

    async def play_search(self, slot, queries):
        """Google Play's apps for each query, read in the phone's own Chrome and marked with the ones the user has
        on this phone or ever installed ([{"query", "found"} or {"query", "error"}]); a query that is a package ID
        gets that app's own page. Read-only: no lease, nothing on screen. Raises DeviceError when the gateway or
        Chrome does not answer."""
        try:
            r = await self.client.post(f"/devices/{slot}/play/search", json={"queries": queries}, timeout=SEARCH_TIMEOUT)
        except httpx.TransportError:
            raise DeviceError("The device gateway did not answer.") from None
        if r.is_error:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
            raise DeviceError(detail if isinstance(detail, str) else f"Google Play could not be searched (HTTP {r.status_code}).")
        return r.json()["results"]

    async def history_search(self, slot, queries):
        """What the user searched for and the pages they went to on their other devices, for each query's words, as
        Chrome keeps it on the phone ([{"query", "found"} or {"query", "error"}]; gateway/history.py). Read-only: no
        lease, nothing on screen. Raises DeviceError when the gateway cannot read it."""
        try:
            r = await self.client.post(f"/devices/{slot}/history/search", json={"queries": queries}, timeout=SEARCH_TIMEOUT)
        except httpx.TransportError:
            raise DeviceError("The device gateway did not answer.") from None
        if r.is_error:
            detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
            raise DeviceError(detail if isinstance(detail, str) else f"Chrome's history could not be read (HTTP {r.status_code}).")
        return r.json()["results"]

    async def ops(self, slot, owner, ops):
        """Run ops-lane commands on the device (gateway.run_ops); one result per op that ran, in order.
        Never retried: a command may already have changed the device."""
        try:
            r = await self.client.post(
                f"/devices/{slot}/ops", json={"owner": owner, "ops": ops},
                # wait_for may poll up to 5 s; the rest take milliseconds.
                timeout=70 + 10 * sum(1 for op in ops if op.get("op") == "wait_for"),
            )
        except httpx.TransportError:
            raise DeviceError(
                "The device ops may or may not have run: the gateway did not answer. Check current_screen."
            ) from None
        if r.status_code == 422:
            raise ValueError(rejection_detail(r))
        if r.is_error:
            raise DeviceError(f"The device gateway could not run the ops (HTTP {r.status_code}).")
        return r.json()["results"]

    async def action(self, slot, owner, action):
        # A device action is NEVER automatically retried on network failure.
        # install_app downloads and installs an APK, which takes minutes.
        timeout = (
            2 * settings.install_timeout_seconds + 60
            if action["kind"] == "install_app" else self.client.timeout
        )
        try:
            r = await self.client.post(
                f"/devices/{slot}/actions", json={"owner": owner, "action": action}, timeout=timeout
            )
        except httpx.TransportError:
            raise DeviceError(
                "Android action failed or has uncertain results. Inspect the current screen before retrying."
            ) from None
        if r.status_code == 422:
            # Validation/precondition failures occur before a side effect. Feed them
            # back to the agent so it can install the app or revise its action.
            raise ValueError(rejection_detail(r))
        if r.is_error:
            raise DeviceError(
                "Android action failed or has uncertain results. Inspect the current screen before retrying."
            )
        return r.json()


def rejection_detail(response):
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        # FastAPI request validation: the gateway does not understand this request.
        problems = "; ".join(
            f"{'.'.join(map(str, d.get('loc', [])[1:]))}: {d.get('msg')}"
            for d in detail if isinstance(d, dict)
        )
        return (
            f"The device gateway rejected the request format ({problems[:400]}). "
            "The gateway may be running outdated code; restart it."
        )
    return "Android rejected the requested action."
