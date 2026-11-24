"""Which sign-ins Google Password Manager has saved for the site or app on screen: account names only, never a
password or key. The phone agent gets them as one line of its turn ("passkeys for a@b.com; passwords for a@b.com")
so it uses Chrome's or Google's own offer, which a tap on the sign-in field brings up, instead of asking the user
for a password.

relay-bridge's SavedSignins (android/bridge) reads Google Play services' index of the account's
synced data on the phone, as root, and prints site and account names. Its answer is kept per phone until that
index changes (or relay-bridge is updated); a phone whose relay-bridge predates SavedSignins has none."""

import asyncio
import json
from collections import defaultdict
from urllib.parse import urlparse

from fastapi import APIRouter

from . import core
from .simple_signins import su

router = APIRouter(prefix="/devices/{slot}/saved-signins")

STORE = "/data/data/com.google.android.gms/databases/chromesync.data_store"
HELPER = ("export CLASSPATH=$(pm path app.relay.bridge | head -n 1 | cut -d: -f2); "
          "exec app_process /system/bin --nice-name=relay-signins app.relay.bridge.SavedSignins")
# The second level of country domains that sell names under it (amazon.co.uk, uber.com.au).
SECOND_LEVELS = {"co", "com", "net", "org", "gov", "edu", "ac", "gob", "or", "ne", "go"}

cache: dict[str, tuple[str, dict | None]] = {}  # slot -> (stamp, index or None when the helper gave none)
refreshing: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def site_of(host):
    """The registrable part of a host: www.amazon.com -> amazon.com, www.amazon.co.uk -> amazon.co.uk."""
    labels = (host or "").lower().strip(".").split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVELS else 2
    return ".".join(labels[-keep:])


def realm_place(realm):
    """("app", package) for an android:// realm, ("web", host) for a website's, None otherwise."""
    parsed = urlparse(realm)
    if parsed.scheme == "android":
        return ("app", parsed.netloc.rsplit("@", 1)[-1]) if parsed.netloc else None
    if parsed.scheme in ("http", "https") and parsed.hostname:
        return ("web", parsed.hostname.lower())
    return None


def rp_covers(rp, host):
    """A passkey made for `rp` works on `host` (WebAuthn: rp is the host or a parent domain of it)."""
    rp = rp.lower()
    return host == rp or host.endswith("." + rp)


def match(index, url="", package=""):
    """{"site", "passkeys", "passwords"}: the account names saved for the page at `url` (its site), else for the
    app `package` (its own and its linked websites'), in Google's order, each once."""
    host = (urlparse(url).hostname or "").lower() if url else ""
    if host:
        hosts, label = [host], site_of(host)
    elif package:
        linked = index.get("apps", {}).get(package) or index.get("app_groups", {}).get(package) or []
        hosts, label = [h for h in ((urlparse(u).hostname or "").lower() for u in linked) if h], package
    else:
        return {"site": "", "passkeys": [], "passwords": []}
    sites = {site_of(h) for h in hosts}
    passwords = [entry.get("user") for entry in index.get("passwords", [])
                 if (place := realm_place(entry.get("realm", "")))
                 and (place == ("app", package) if place[0] == "app" else site_of(place[1]) in sites)]
    passkeys = [key.get("user") for key in index.get("passkeys", [])
                if key.get("rp") and any(rp_covers(key["rp"], h) for h in hosts)]
    return {"site": label, "passkeys": once(passkeys), "passwords": once(passwords)}


def once(names):
    return list(dict.fromkeys(name for name in names if name))


async def stamp(slot):
    """Changes when Google's index or relay-bridge changes."""
    return (await su(slot, f"stat -c %Y-%s {STORE} 2>/dev/null; pm path app.relay.bridge", timeout=10)).strip()


async def index(slot):
    """The phone's saved sign-ins ({"passwords", "passkeys", "apps", "app_groups"}), or None when it cannot say."""
    now = await stamp(slot)
    if not now.split("\n", 1)[0]:
        return None  # no Google Password Manager data on this phone (no Google sign-in yet)
    async with refreshing[slot]:
        kept = cache.get(slot)
        if kept and kept[0] == now:
            return kept[1]
        found = None
        for line in reversed((await su(slot, HELPER, timeout=30)).splitlines()):
            try:
                answer = json.loads(line)
            except ValueError:
                continue
            found = answer if isinstance(answer, dict) and answer.get("ok") else None
            break
        cache[slot] = (now, found)
        return found


@router.get("")
async def saved_signins(slot: str, url: str = "", package: str = ""):
    """Account names Google Password Manager has passkeys and passwords for, for the page at `url` (else the app
    `package`). Read-only: no lease."""
    core.require_slot(slot)
    found = await index(slot)
    return match(found, url, package) if found else {"site": "", "passkeys": [], "passwords": []}
