"""Simplify sign-ins (gateway/simple_signins.py and its API in api/devices.py): after the user's Google sign-in, a
random screen-lock PIN made on the phone, relay-bridge's unlocker loaded first, and Chrome's "Turn on sync?" opened
from inside Chrome; set up once by itself per new account while the switch is on. The phone's shell is faked."""

import asyncio

import httpx
import pytest
from app.gateway import core, simple_signins
from app.store import Store

SLOT = next(iter(core.slots))
ACCOUNT = "name@gmail.com"


class Phone:
    def __init__(self, account=True, credential="NONE", prop="", unlocker=False, chrome_signed_in=False):
        self.account, self.credential, self.prop = account, credential, prop
        self.unlocker, self.chrome_signed_in = unlocker, chrome_signed_in
        self.scoped = {"com.android.chrome"}
        self.pid, self.log, self.calls = 100, [], []

    async def sh(self, slot, script, timeout=25):
        self.log.append(script)
        if script == "dumpsys account":
            return f"Account {{name={ACCOUNT}, type=com.google}}\n" if self.account else ""
        if script == "dumpsys lock_settings":
            return f"    CredentialType: {self.credential}\n"
        if script == f"getprop {simple_signins.PIN_PROP}":
            return self.prop + "\n"
        if script == "pidof com.android.systemui":
            return f"{self.pid}\n"
        if script.startswith("logcat -d -s relay_unlocker"):
            return "I relay_unlocker: installed: {unlock_keyguard=ok, unlock_skip_bouncer=ok, unlock_prompt=ok}\n" \
                if self.unlocker else ""
        return ""

    async def su(self, slot, script, timeout=25):
        self.log.append("su " + script)
        if script.startswith("cat " + simple_signins.CHROME_PREFS):
            return '{"google":{"services":{"consented_to_sync":true}}}' if self.chrome_signed_in else "{}"
        if script.endswith("scope ls app.relay.bridge"):
            return "".join(f"{p}  0\n" for p in sorted(self.scoped))
        if " scope add app.relay.bridge " in script:
            self.scoped.add(script.rsplit(" ", 1)[1].split("/")[0])
        if script == "pkill -f com.android.systemui":
            self.pid += 1
            self.unlocker = "com.android.systemui" in self.scoped
        if script == simple_signins.SET_PIN:
            self.credential, self.prop = "PIN", "48213907"
            return "pin-set\n"
        if script == simple_signins.CLEAR_PIN:
            self.credential, self.prop = "NONE", ""
            return "pin-cleared\n"
        return ""

    # core.phones[slot]: relay-bridge sockets and commands
    async def bridges(self):
        return ["com.android.chrome"]

    async def call(self, cmd, process=None, timeout=10, **args):
        self.calls.append((cmd, process, args))
        return {"ok": True}


@pytest.fixture
def phone(monkeypatch):
    def install(**kwargs):
        p = Phone(**kwargs)
        monkeypatch.setattr(simple_signins, "sh", p.sh)
        monkeypatch.setattr(simple_signins, "su", p.su)
        monkeypatch.setitem(core.phones, SLOT, p)
        monkeypatch.setattr(simple_signins, "POLL", 0)
        return p
    return install


async def test_it_waits_for_the_google_sign_in(phone):
    p = phone(account=False)
    with pytest.raises(simple_signins.HTTPException) as refused:
        await simple_signins.change(SLOT, simple_signins.Change(enabled=True))
    assert refused.value.status_code == 409 and "Google" in refused.value.detail
    assert p.credential == "NONE" and not p.calls


async def test_the_unlocker_is_loaded_before_the_pin_and_chrome_asks_to_sync(phone):
    p = phone()
    result = await simple_signins.change(SLOT, simple_signins.Change(enabled=True))
    assert result["enabled"] and result["chrome"] == "consent"
    restart, pin = p.log.index("su pkill -f com.android.systemui"), p.log.index("su " + simple_signins.SET_PIN)
    assert restart < pin
    assert not any(c.isdigit() and len(c) == 8 for c in " ".join(p.log).split())  # the PIN never crosses adb
    assert p.log.index(f"am start -n {simple_signins.CHROME_MAIN}") > pin
    (cmd, process, args), = p.calls
    assert (cmd, process, args["class"]) == ("start_activity", "com.android.chrome", simple_signins.SYNC_CONSENT)
    assert args["extras"]["SigninActivity.FragmentArgs"]["SyncConsentFragmentBase.AccountName"] == ACCOUNT


async def test_systemui_is_not_restarted_when_the_unlocker_runs_already(phone):
    p = phone(unlocker=True)
    p.scoped.add("com.android.systemui")
    await simple_signins.change(SLOT, simple_signins.Change(enabled=True))
    assert "su pkill -f com.android.systemui" not in p.log and p.credential == "PIN"


async def test_a_chrome_signed_in_already_is_left_alone(phone):
    p = phone(unlocker=True, chrome_signed_in=True)
    result = await simple_signins.change(SLOT, simple_signins.Change(enabled=True))
    assert result["chrome"] == "signed_in" and not p.calls
    assert not any(c.startswith("am start") for c in p.log)


async def test_chrome_restarts_when_it_was_not_scoped_to_the_bridge(phone):
    p = phone(unlocker=True)
    p.scoped = {"com.android.systemui"}
    await simple_signins.change(SLOT, simple_signins.Change(enabled=True))
    assert p.log.index("am force-stop com.android.chrome") < p.log.index(f"am start -n {simple_signins.CHROME_MAIN}")


async def test_a_lock_relay_did_not_set_is_not_replaced(phone):
    p = phone(credential="PIN")
    with pytest.raises(simple_signins.HTTPException) as refused:
        await simple_signins.change(SLOT, simple_signins.Change(enabled=True))
    assert refused.value.status_code == 409
    assert not any(simple_signins.SET_PIN in c for c in p.log)


async def test_turning_it_off_removes_the_pin_and_keeps_chrome(phone):
    p = phone(credential="PIN", prop="48213907", unlocker=True, chrome_signed_in=True)
    assert (await simple_signins.get_state(SLOT))["enabled"]
    result = await simple_signins.change(SLOT, simple_signins.Change(enabled=False))
    assert not result["enabled"] and result["chrome_signed_in"]
    assert p.credential == "NONE"


@pytest.fixture
def api(tmp_path, monkeypatch):
    from app.api import common, devices

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json={"accounts": [ACCOUNT]})
        return httpx.Response(200, json={"account": ACCOUNT, "enabled": True, "chrome_signed_in": False,
                                         "foreign_lock": False, "chrome": "consent"})

    monkeypatch.setattr(common, "store", Store(tmp_path))
    monkeypatch.setattr(common, "gateway", httpx.AsyncClient(base_url="http://gateway",
                                                             transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(devices, "simple_signin_tasks", {})
    return devices, seen


async def settle(devices):
    await asyncio.gather(*devices.simple_signin_tasks.values())


async def test_a_new_google_account_is_set_up_once(api):
    devices, seen = api
    for _ in range(3):  # the window polls the account
        await devices.device_google(SLOT)
        await settle(devices)
    assert seen.count(("POST", f"/devices/{SLOT}/simple-signins")) == 1


async def test_nothing_happens_by_itself_with_the_switch_off(api):
    devices, seen = api
    devices.common.store.profile_set(devices.SIMPLE_SIGNINS, {"on": False})
    await devices.device_google(SLOT)
    await settle(devices)
    assert ("POST", f"/devices/{SLOT}/simple-signins") not in seen
    assert (await devices.simple_signins_state(SLOT))["on"] is False


async def test_the_switch_is_saved_and_sent_to_the_phone(api):
    devices, seen = api
    result = await devices.change_simple_signins(SLOT, devices.SimpleSigninsInput(enabled=True))
    assert result["on"] and result["chrome"] == "consent"
    assert devices.simple_signins_pref()["on"] is True
    assert seen == [("POST", f"/devices/{SLOT}/simple-signins")]
