"""The phone sleeps when nobody uses it and wakes before anything does (app/gateway/sleep.py)."""

import asyncio

import httpx
import pytest
from app.config import settings
from app.store import Store


@pytest.fixture
def trims(monkeypatch):
    """The serials whose emulator was asked to hand its memory back, in order."""
    from app.gateway import memory

    asked = []

    async def trim(serial):
        asked.append(serial)
        return True

    monkeypatch.setattr(memory, "trim", trim)
    return asked


@pytest.fixture
def phone(tmp_path, monkeypatch, trims):
    """A gateway with one phone whose adb commands are recorded."""
    from app import gateway
    from app.gateway import sleep

    monkeypatch.setattr(settings, "phone_sleep_seconds", 0.02)
    monkeypatch.setattr(settings, "phone_doze_seconds", 0.02)
    monkeypatch.setattr(settings, "phone_trim_seconds", 0.02)
    monkeypatch.setattr(settings, "phone_trim_after_seconds", 0.02)
    monkeypatch.setattr(gateway.core, "store", Store(tmp_path / "gateway"))
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5600"})
    monkeypatch.setattr(sleep, "state", {})
    monkeypatch.setattr(sleep, "timers", {})
    monkeypatch.setattr(sleep, "awake_until", {})
    commands = []

    async def adb(slot, *args, **kwargs):
        commands.append(" ".join(args))
        if "getprop" in args[-1]:
            return b"1\ntrue\nPhysical size: 720x1600\n"
        return b"  isKeyguardShowing=false\n" if "isKeyguardShowing" in args[-1] else b""

    async def shell(slot, *args):
        commands.append("shell " + " ".join(map(str, args)))
        return b""

    async def command(*args, **kwargs):
        return b"List of devices attached\nemulator-5600\tdevice\n"

    monkeypatch.setattr(gateway.core, "adb", adb)
    monkeypatch.setattr(gateway.core, "shell", shell)
    monkeypatch.setattr(gateway.core, "command", command)
    monkeypatch.setattr(gateway.watch, "ready", lambda slot: True)
    return gateway, sleep, commands


def client(gateway):
    return httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app))


async def until(condition, seconds=2.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_idle_phone_goes_home_screen_off_then_doze(phone, trims):
    gateway, sleep, commands = phone
    sleep.idle("device-1")
    # In Doze its memory goes back to the computer, and again as it dozes on.
    await until(lambda: sleep.state.get("device-1") == sleep.DOZING and len(trims) >= 2)
    assert commands == ["shell input keyevent KEYCODE_HOME; input keyevent KEYCODE_SLEEP",
                        # Doze, with its maintenance window at penrose's pace (what Google holds back arrives in it).
                        "shell " + sleep.doze_pace() + "dumpsys deviceidle force-idle deep"]
    assert set(trims) == {"emulator-5600"}
    async with client(gateway) as c:
        assert (await c.get("/devices")).json()[0]["asleep"] is True


async def test_waking_stops_the_memory_trims(phone, trims):
    gateway, sleep, commands = phone
    sleep.idle("device-1")
    await until(lambda: len(trims) >= 1)
    assert await sleep.wake("device-1") is True
    assert "device-1" not in sleep.timers
    seen = len(trims)
    await asyncio.sleep(0.1)
    assert len(trims) == seen


async def test_a_phone_that_has_only_just_dozed_keeps_its_memory(phone, trims, monkeypatch):
    # Memory handed back may be read from disk again at the wake (slow when the computer reused it meanwhile), so a
    # phone that may be used again soon keeps it: the first trim waits for phone_trim_after_seconds of Doze.
    gateway, sleep, commands = phone
    monkeypatch.setattr(settings, "phone_trim_after_seconds", 30)
    sleep.idle("device-1")
    await until(lambda: sleep.state.get("device-1") == sleep.DOZING)
    await asyncio.sleep(0.1)
    assert trims == []
    assert await sleep.wake("device-1") is True  # and a wake meanwhile calls it off
    assert "device-1" not in sleep.timers


async def test_no_trims_when_trim_seconds_is_zero(phone, trims, monkeypatch):
    gateway, sleep, commands = phone
    monkeypatch.setattr(settings, "phone_trim_seconds", 0)
    sleep.idle("device-1")
    await until(lambda: sleep.state.get("device-1") == sleep.DOZING and "device-1" not in sleep.timers)
    assert trims == []


async def test_a_lease_wakes_it_and_keeps_it_awake_until_released(phone):
    gateway, sleep, commands = phone
    sleep.state["device-1"] = sleep.DOZING
    async with client(gateway) as c:
        assert (await c.post("/leases", json={"owner": "run-1"})).status_code == 200
        assert any("deviceidle unforce" in c and "KEYCODE_WAKEUP" in c for c in commands)
        assert sleep.state["device-1"] == sleep.AWAKE
        # Held, and used: no countdown.
        assert (await c.post("/devices/device-1/ops", json={"owner": "run-1", "ops": [{"op": "wait", "seconds": 0}]})
                ).status_code == 200
        assert "device-1" not in sleep.timers
        await asyncio.sleep(0.1)
        assert sleep.state["device-1"] == sleep.AWAKE
        # Given back: it sleeps.
        await c.delete("/leases/run-1")
        await until(lambda: sleep.state.get("device-1") == sleep.DOZING)


async def test_the_users_tap_on_a_sleeping_phone_only_wakes_it(phone):
    gateway, sleep, commands = phone
    sleep.state["device-1"] = sleep.OFF
    async with client(gateway) as c:
        await c.post("/leases", json={"owner": "manual:device-1:x", "slot": "device-1"})
        answer = (await c.post("/devices/device-1/actions",
                               json={"owner": "manual:device-1:x", "action": {"kind": "tap", "x": 5, "y": 5}})).json()
        assert answer["woke"] is True
        assert not any("input tap" in c for c in commands)
        # Awake now: the next tap lands.
        answer = (await c.post("/devices/device-1/actions",
                               json={"owner": "manual:device-1:x", "action": {"kind": "tap", "x": 5, "y": 5}})).json()
        assert "woke" not in answer and commands[-1] == "shell input tap 5 5"


async def test_reads_and_location_updates_leave_it_asleep(phone):
    gateway, sleep, commands = phone
    sleep.state["device-1"] = sleep.DOZING
    async with client(gateway) as c:
        await c.get("/devices")
        await c.get("/leases/device-1")
    assert not any("KEYCODE_WAKEUP" in c for c in commands)
    assert sleep.asleep("device-1")


async def test_the_wake_button_wakes_it_and_it_sleeps_again(phone):
    gateway, sleep, commands = phone
    sleep.state["device-1"] = sleep.DOZING
    async with client(gateway) as c:
        assert (await c.post("/devices/device-1/wake")).json() == {"asleep": False}
    await until(lambda: sleep.state.get("device-1") == sleep.DOZING)


async def test_a_sign_of_the_user_keeps_it_awake_for_a_while_then_it_sleeps(phone):
    gateway, sleep, commands = phone
    sleep.state["device-1"] = sleep.DOZING
    async with client(gateway) as c:
        assert (await c.post("/devices/device-1/wake", json={"for_seconds": 0.3})).json() == {"asleep": False}
    assert any("deviceidle unforce" in c for c in commands)  # woken now, out of Doze
    await asyncio.sleep(0.15)  # the usual countdown (0.02 s) has long passed
    assert sleep.state["device-1"] == sleep.AWAKE
    await until(lambda: sleep.state.get("device-1") == sleep.DOZING)  # and once the window has, it sleeps


async def test_off_when_sleep_seconds_is_zero(phone, monkeypatch):
    gateway, sleep, commands = phone
    monkeypatch.setattr(settings, "phone_sleep_seconds", 0)
    sleep.idle("device-1")
    assert await sleep.wake("device-1") is False
    assert not sleep.timers and not commands


async def test_waking_starts_chromes_browser_before_the_phone_counts_as_awake(phone, monkeypatch):
    """Chrome started by a push message has no browser and no DevTools socket (2026-11-04): each wake opens its
    window if so, and goes Home, before anything that waits for the wake uses the phone."""
    gateway, sleep, commands = phone
    states = []

    async def adb(slot, *args, **kwargs):
        commands.append(" ".join(args))
        if "chrome_devtools_remote" in args[-1]:
            states.append(sleep.state.get(slot))
            return b"started\n"
        return b"  isKeyguardShowing=false\n" if "isKeyguardShowing" in args[-1] else b""

    monkeypatch.setattr(gateway.core, "adb", adb)
    sleep.state["device-1"] = sleep.DOZING
    assert await sleep.wake("device-1") is True
    browser = [c for c in commands if "chrome_devtools_remote" in c]
    assert len(browser) == 1 and "am start -W -n com.android.chrome/" in browser[0] and "KEYCODE_HOME" in browser[0]
    assert states == [sleep.DOZING] and sleep.state["device-1"] == sleep.AWAKE
    # Awake: no check until the next wake.
    await sleep.wake("device-1")
    assert len([c for c in commands if "chrome_devtools_remote" in c]) == 1


async def test_a_wake_that_cannot_reach_chrome_still_wakes(phone, monkeypatch):
    gateway, sleep, commands = phone

    async def adb(slot, *args, **kwargs):
        if "chrome_devtools_remote" in args[-1]:
            raise TimeoutError()
        return b"  isKeyguardShowing=false\n" if "isKeyguardShowing" in args[-1] else b""

    monkeypatch.setattr(gateway.core, "adb", adb)
    sleep.state["device-1"] = sleep.OFF
    assert await sleep.wake("device-1") is True and sleep.state["device-1"] == sleep.AWAKE


async def test_a_phone_that_comes_up_again_is_checked_in_full_at_its_next_wake(phone):
    """An emulator started on demand, or one that restarted under a running gateway, has Chrome as it booted."""
    gateway, sleep, commands = phone
    sleep.state["device-1"] = sleep.AWAKE
    await gateway.devices.became_ready("device-1")
    assert "device-1" not in sleep.state
    assert await sleep.wake("device-1") is True
    assert any("chrome_devtools_remote" in c for c in commands)
