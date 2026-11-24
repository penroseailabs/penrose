"""Simplify sign-ins on the phone: Google Password Manager's passwords and passkeys, ready for Relay to sign in to
websites with. It needs three things, all after the user's Google sign-in:

- A screen lock. Passkeys and Google's encrypted password data only work on a phone with one. Relay sets a random
  8-digit PIN, made on the phone and kept only there (`persist.relay.lock_pin`), and relay-bridge's unlocker in
  SystemUI (android/bridge, Unlocker.java) uses it wherever Android asks: nobody ever sees a lock
  screen or types it. Google later trusts this PIN to unlock the account's encrypted data, which is why it is not
  a fixed one.
- Chrome signed in with sync on. Relay starts Chrome with its first-run screens off, and those are where Chrome
  would have offered it. Relay opens Chrome's own "Turn on sync?" screen for the account, from inside Chrome
  (relay-bridge `start_activity`: the screen is not exported and takes a nested Bundle), and the user presses
  "Yes, I'm in": it is their consent to give.
- Once per phone, Google's "Verify it's you" for the account's encrypted data: the screen lock of another of the
  user's devices, or their password. Google shows it the first time a passkey or password is used; only the user
  can answer it.

Turning it off removes the PIN (passkeys stop working); Chrome stays signed in."""

import asyncio
import re
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..android import google_accounts
from ..bridge.devices import DeviceError
from . import core

router = APIRouter(prefix="/devices/{slot}/simple-signins")

PIN_PROP = "persist.relay.lock_pin"
VECTOR_CLI = "sh /data/adb/modules/zygisk_vector/cli"
CHROME = "com.android.chrome"
CHROME_MAIN = f"{CHROME}/com.google.android.apps.chrome.Main"
CHROME_PREFS = f"/data/data/{CHROME}/app_chrome/Default/Preferences"
SYNC_CONSENT = "org.chromium.chrome.browser.signin.SyncConsentActivity"
SETTINGS_ACCESS_POINT = 3  # Chrome's signin_metrics::AccessPoint::SETTINGS
POLL = 0.4  # seconds between looks while SystemUI or Chrome comes up
# Made on the phone so the PIN never crosses adb, and checked by the same script.
SET_PIN = (f'p=$(tr -dc 0-9 < /dev/urandom | head -c 8); setprop {PIN_PROP} "$p"; '
           'locksettings set-pin "$p" >/dev/null && echo pin-set')
CLEAR_PIN = (f'p=$(getprop {PIN_PROP}); [ -n "$p" ] && locksettings clear --old "$p" >/dev/null '
             f'&& setprop {PIN_PROP} "" && echo pin-cleared')


class Change(BaseModel):
    enabled: bool


async def sh(slot, script, timeout=25):
    """The script's output; a non-zero exit is an answer here (pidof, a missing file), not a failure."""
    stdout, _, _ = await core.command_result("-s", core.slots[slot], "shell", script, timeout=timeout)
    return stdout.decode(errors="replace")


async def su(slot, script, timeout=25):
    quoted = script.replace("'", "'\\''")
    return await sh(slot, f"su -c '{quoted}'", timeout=timeout)


async def credential(slot):
    found = re.search(r"CredentialType:\s*(\w+)", await sh(slot, "dumpsys lock_settings"))
    return found.group(1).upper() if found else "?"


async def has_pin_prop(slot):
    return bool((await sh(slot, f"getprop {PIN_PROP}")).strip())


async def chrome_signed_in(slot):
    prefs = await su(slot, f"cat {CHROME_PREFS} 2>/dev/null")
    return '"consented_to_sync":true' in prefs.replace(" ", "")


async def unlocker_loaded(slot):
    """relay-bridge's unlocker is hooked in the running SystemUI (it logs once per SystemUI start)."""
    pid = (await sh(slot, "pidof com.android.systemui")).strip()
    if not pid:
        return False
    log = await sh(slot, f"logcat -d -s relay_unlocker --pid={pid}")
    return "unlock_keyguard=ok" in log and "unlock_prompt=ok" in log


async def ensure_scoped(slot, package):
    """relay-bridge loads into `package`; True when it was not before (the app must restart to get it)."""
    if re.search(rf"^{re.escape(package)}\s", await su(slot, f"{VECTOR_CLI} scope ls app.relay.bridge"), re.M):
        return False
    await su(slot, f"{VECTOR_CLI} scope add app.relay.bridge {package}/0")
    return True


async def load_unlocker(slot):
    """Scope relay-bridge to SystemUI and restart SystemUI if its unlocker is not running yet. Only ever while the
    phone has no screen lock: a SystemUI restart then shows nothing."""
    await ensure_scoped(slot, "com.android.systemui")
    if await unlocker_loaded(slot):
        return
    old = (await sh(slot, "pidof com.android.systemui")).strip()
    await su(slot, "pkill -f com.android.systemui")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL)
        pid = (await sh(slot, "pidof com.android.systemui")).strip()
        if pid and pid != old and await unlocker_loaded(slot):
            return
    raise HTTPException(503, "penrose’s screen-lock helper didn’t start on the phone. Restart penrose and try again.")


async def open_sync_consent(slot, account):
    """Chrome's "Turn on sync?" for `account`, opened by Chrome itself through relay-bridge. Android lets an app
    open screens only while it is in front, so Chrome is brought up first."""
    phone = core.phones[slot]
    await sh(slot, f"am start -n {CHROME_MAIN}")
    deadline = time.monotonic() + 20
    while CHROME not in await bridges(phone):
        if time.monotonic() > deadline:
            raise HTTPException(503, "Chrome on the phone isn’t connected to penrose. Restart penrose and try again.")
        await asyncio.sleep(POLL)
    result = await phone.call("start_activity", process=CHROME, timeout=10, **{
        "class": SYNC_CONSENT,
        "extras": {"SigninActivity.FragmentArgs": {
            "SyncConsentFragmentBase.SigninFlowType": 0,
            "SyncConsentFragmentBase.AccessPoint": SETTINGS_ACCESS_POINT,
            "SyncConsentFragmentBase.AccountName": account,
        }},
    })
    if not result.get("ok"):
        raise HTTPException(502, f"Chrome didn’t open its sync screen: {result.get('error') or 'no answer'}")


async def bridges(phone):
    try:
        return await phone.bridges()
    except DeviceError:  # grep finds no socket at all while apps start: none yet
        return []


async def state(slot):
    accounts = google_accounts(await sh(slot, "dumpsys account", timeout=8))
    lock = await credential(slot)
    return {
        "account": accounts[0] if accounts else None,
        "enabled": lock == "PIN" and await has_pin_prop(slot),
        "chrome_signed_in": await chrome_signed_in(slot),
        "foreign_lock": lock not in ("NONE", "PIN", "?") or (lock == "PIN" and not await has_pin_prop(slot)),
    }


@router.get("")
async def get_state(slot: str):
    """Whether sign-ins are simplified: Relay's screen lock is on, and whether Chrome is signed in with sync."""
    core.require_slot(slot)
    return await state(slot)


@router.post("")
async def change(slot: str, body: Change):
    """Turn simplified sign-ins on (after Google sign-in only) or off. On: the screen lock, then Chrome's "Turn on
    sync?" screen when Chrome is not signed in yet; `chrome` says which ("signed_in", or "consent": the user
    presses "Yes, I'm in" on the phone). Off: the screen lock goes."""
    core.require_slot(slot)
    async with core.locks[slot]:
        now = await state(slot)
        if not body.enabled:
            if now["enabled"] and "pin-cleared" not in await su(slot, CLEAR_PIN):
                raise HTTPException(502, "The phone’s screen lock couldn’t be removed.")
            return {**await state(slot), "chrome": None}
        if not now["account"]:
            raise HTTPException(409, "Sign in to Google on the phone first.")
        if now["foreign_lock"]:
            raise HTTPException(409, "The phone has a screen lock penrose didn’t set. Remove it in the phone’s "
                                     "Settings, then try again.")
        if not now["enabled"]:
            await load_unlocker(slot)
            if "pin-set" not in await su(slot, SET_PIN):
                raise HTTPException(502, "The phone’s screen lock couldn’t be set.")
        chrome = "signed_in"
        if not now["chrome_signed_in"]:
            if await ensure_scoped(slot, CHROME):
                await sh(slot, f"am force-stop {CHROME}")
            await open_sync_consent(slot, now["account"])
            chrome = "consent"
        return {**await state(slot), "chrome": chrome}
