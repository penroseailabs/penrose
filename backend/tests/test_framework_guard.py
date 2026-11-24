"""The gateway follows each phone from boot to ready (app/gateway/watch.py), and a phone that came back up without
relay-bridge (Magisk dropped Zygisk) is restarted, never in a loop (app/gateway/framework.py)."""

import asyncio

import httpx
import pytest
from app.store import Store


class Phone:
    """adb for one phone: what each shell command answers. A wait that never ends on the phone times out."""

    def __init__(self, pin=False, home=True, hooked=True, unlocks=True):
        self.pin, self.home, self.hooked, self.unlocks = pin, home, hooked, unlocks
        self.commands = []

    async def adb(self, slot, *args, timeout=25, **kwargs):
        command = " ".join(args)
        self.commands.append(command)
        if "until" in command:
            done = (("ce_available" not in command or self.unlocks)
                    and ("/proc/net/unix" not in command or self.hooked))
            if not done:
                raise TimeoutError
            return b""
        if "persist.relay.lock_pin" in command:
            return b"0000\n" if self.pin else b"\n"
        if "pm path" in command:
            return b"package:/data/app/home.apk\n" if self.home else b""
        if "wm size" in command:
            return b"Physical size: 720x1600\n"
        return b""


@pytest.fixture
def phone(tmp_path, monkeypatch):
    from app.gateway import core, framework, watch

    for module, name in ((watch, "states"), (watch, "setup"), (watch, "tasks"), (framework, "failed_restarts"),
                         (framework, "restarting")):
        monkeypatch.setattr(module, name, {})
    monkeypatch.setattr(watch, "_on_ready", [])
    monkeypatch.setattr(core, "store", Store(tmp_path / "gateway"))
    monkeypatch.setattr(core, "slots", {"device-1": "emulator-5600"})
    fake = Phone()
    monkeypatch.setattr(core, "adb", fake.adb)

    class Bridge:
        async def call(self, cmd, **kwargs):
            fake.commands.append(f"bridge {cmd} {kwargs.get('process')}")
            return {"ok": True}

        async def reset(self):
            pass

    monkeypatch.setattr(core, "phones", {"device-1": Bridge()})
    return watch, framework, fake


async def test_a_hooked_phone_becomes_ready_and_its_screen_size_is_read(phone):
    watch, framework, fake = phone
    await watch._check("device-1")
    assert watch.state("device-1") == watch.READY and watch.ready("device-1")
    assert watch.dimensions["device-1"] == {"width": 720, "height": 1600}
    assert "bridge hello app.relay.home" in fake.commands
    # Booted, unlocked and hooked are each one wait on the phone, not a check per round trip.
    assert sum("until" in c for c in fake.commands) == 3


async def test_a_home_app_without_relay_bridge_restarts_the_phone(phone):
    watch, framework, fake = phone
    fake.hooked = False
    await watch._check("device-1")
    assert watch.state("device-1") == watch.STARTING
    await asyncio.sleep(0)
    assert "shell su -c reboot" in fake.commands
    assert framework.failed_restarts == {"device-1": 1}


async def test_a_phone_left_on_its_lock_screen_is_restarted(phone):
    watch, framework, fake = phone
    fake.pin, fake.unlocks = True, False
    await watch._check("device-1")
    assert "restarting" in watch.states["device-1"]["detail"]
    assert "still locked" in watch.states["device-1"]["detail"]


async def test_restarts_stop_after_two_that_brought_nothing_back(phone):
    watch, framework, fake = phone
    fake.hooked = False
    for _ in range(framework.MAX_FAILED_RESTARTS + 1):
        await watch._check("device-1")
        await asyncio.sleep(0)
    assert fake.commands.count("shell su -c reboot") == framework.MAX_FAILED_RESTARTS
    assert watch.state("device-1") == watch.BROKEN
    # Hooks seen again start the count over.
    fake.hooked = True
    await watch._check("device-1")
    assert framework.failed_restarts == {} and watch.ready("device-1")


async def test_no_home_app_is_counted_as_ready(phone):
    watch, framework, fake = phone
    fake.home, fake.hooked = False, False
    await watch._check("device-1")
    assert watch.ready("device-1") and "shell su -c reboot" not in fake.commands


async def test_while_setup_updates_the_phone_it_is_not_leased_or_restarted(phone):
    watch, framework, fake = phone
    watch.set_setup("device-1", True)
    fake.hooked = False
    await watch._check("device-1")
    assert "shell su -c reboot" not in fake.commands
    assert watch.view("device-1")["state"] == "setup" and not watch.ready("device-1")
    # Once setup is done, the phone is checked again.
    fake.hooked = True
    watch.set_setup("device-1", False)
    for _ in range(20):
        await asyncio.sleep(0)
    assert watch.ready("device-1")
    for task in watch.tasks.values():
        task.cancel()


async def test_an_adb_call_that_fails_in_passing_is_tried_again(phone, monkeypatch):
    watch, framework, fake = phone
    monkeypatch.setattr(watch, "RETRY_SECONDS", 0)
    real = fake.adb
    failures = [RuntimeError("503: ADB command failed")]

    async def flaky(slot, *args, **kwargs):
        if failures and "wm size" in " ".join(args):
            raise failures.pop()
        return await real(slot, *args, **kwargs)

    monkeypatch.setattr(watch.core, "adb", flaky)
    held = asyncio.Event()

    async def hold(slot):
        held.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(watch, "_hold", hold)
    task = asyncio.create_task(watch._follow("device-1"))
    await asyncio.wait_for(held.wait(), 2)
    assert watch.ready("device-1")
    task.cancel()


async def test_a_phone_adb_loses_is_offline_at_once(phone):
    watch, framework, fake = phone
    await watch._check("device-1")
    watch.present("device-1", False)
    assert watch.state("device-1") == watch.OFFLINE and not watch.ready("device-1")


async def test_the_device_list_and_leases_follow_the_phones_state(phone):
    from app import gateway

    watch, framework, fake = phone
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)) as c:
        listed = (await c.get("/devices")).json()
        assert listed[0]["connected"] is False and listed[0]["state"] == "offline"
        assert (await c.post("/leases", json={"owner": "run"})).status_code == 503

        # A lease that waits is answered the moment the phone is ready.
        waiting = asyncio.create_task(c.post("/leases", json={"owner": "run", "wait": 5}))
        await asyncio.sleep(0.05)
        assert not waiting.done()
        await watch._check("device-1")
        assert (await waiting).json() == {"slot": "device-1", "kept": False}

        listed = (await c.get("/devices")).json()
        assert listed[0]["connected"] is True and listed[0]["width"] == 720 and listed[0]["owner"] == "run"

        # And one waiting for a busy phone, the moment it is released.
        other = asyncio.create_task(c.post("/leases", json={"owner": "other", "wait": 5}))
        await asyncio.sleep(0.05)
        assert not other.done()
        await c.delete("/leases/run")
        assert (await other).json()["slot"] == "device-1"
        assert (await c.post("/leases", json={"owner": "third"})).status_code == 409
