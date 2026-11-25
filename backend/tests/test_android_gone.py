"""Android can be gone for good under a phone that still answers adb: system_server ended by Android's watchdog
and left a zombie (2026-11-26; nothing was done for 40 minutes). The gateway asks with its "do you answer"
(app/gateway/watch.py), leaves an Android that starts again by itself alone, has the desktop app restart a phone
whose Android stays gone, and stops restarting a phone that goes down again after every restart."""

import asyncio

import pytest
from app.store import Store


class Phone:
    """adb for one phone whose Android (not its adb daemon) can go."""

    def __init__(self):
        self.android = True
        self.booted = True
        self.commands = []

    async def adb(self, slot, *args, timeout=25, **kwargs):
        command = " ".join(args)
        self.commands.append(command)
        if "echo alive" in command:
            return b"alive\n"
        if "service check activity" in command:
            found = b"found" if self.android else b"not found"
            return (b"1" if self.booted else b"") + b"\nService activity: " + found + b"\n"
        if "pm path" in command:
            return b"" if self.android else b"cmd: Can't find service: package\n"
        if "wm size" in command:
            return b"Physical size: 720x1600\n"
        return b""


@pytest.fixture
def phone(tmp_path, monkeypatch):
    from app.gateway import core, framework, watch

    for name in ("states", "setup", "tasks", "watchdogs", "doubts", "restarted", "_asking"):
        monkeypatch.setattr(watch, name, {})
    monkeypatch.setattr(watch, "frozen", set())
    monkeypatch.setattr(watch, "_news", None)  # made in another test's event loop
    monkeypatch.setattr(watch, "_on_ready", [])
    monkeypatch.setattr(watch, "alive_checks", [])
    monkeypatch.setattr(framework, "failed_restarts", {})
    monkeypatch.setattr(framework, "restarting", {})
    monkeypatch.setattr(core, "store", Store(tmp_path / "gateway"))
    monkeypatch.setattr(core, "slots", {"device-1": "emulator-5600"})
    monkeypatch.setattr(watch, "ANDROID_GRACE", 0.1)
    monkeypatch.setattr(watch, "ANDROID_PAUSE", 0.02)
    monkeypatch.setattr(watch, "RETRY_SECONDS", 0.01)
    monkeypatch.setattr(watch.settings, "phone_alive_seconds", 30)
    fake = Phone()
    monkeypatch.setattr(core, "adb", fake.adb)

    class Bridge:
        async def call(self, cmd, **kwargs):
            return {"ok": True}

        async def reset(self):
            pass

    monkeypatch.setattr(core, "phones", {"device-1": Bridge()})
    asked = []

    async def ask_restart(slot):
        asked.append(slot)

    monkeypatch.setattr(watch, "ask_restart", ask_restart)
    return watch, fake, asked


async def settle(condition, seconds=2):
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "the gateway's tasks did not get there"
        await asyncio.sleep(0.01)


async def up(watch, fake):
    watch.present("device-1", True)
    await settle(lambda: watch.ready("device-1") and any("service check activity" in c for c in fake.commands))


async def test_an_android_that_is_up_is_asked_and_left_alone(phone):
    watch, fake, asked = phone
    await up(watch, fake)
    await asyncio.sleep(0.05)
    assert not asked and watch.ready("device-1")
    watch.present("device-1", False)


async def test_an_android_that_stays_gone_has_the_phone_restarted(phone):
    watch, fake, asked = phone
    await up(watch, fake)
    fake.android = False
    watch.doubt("device-1")  # the next "do you answer"
    await settle(lambda: asked)
    assert asked == ["device-1"] and not watch.ready("device-1")
    assert watch.view("device-1")["detail"] == watch.STOPPED
    # Asked more than once before anything was done: one glance at a restarting Android restarts nothing.
    assert sum("service check activity" in c for c in fake.commands) >= 3
    watch.present("device-1", False)


async def test_an_android_that_starts_again_by_itself_is_left_alone(phone, monkeypatch):
    watch, fake, asked = phone
    monkeypatch.setattr(watch, "ANDROID_GRACE", 1)
    await up(watch, fake)
    fake.android = False  # its watchdog ended system_server
    watch.doubt("device-1")
    await asyncio.sleep(0.08)
    fake.android = True  # zygote started it again, as it does
    await asyncio.sleep(0.2)
    assert not asked
    watch.present("device-1", False)


async def test_nothing_is_judged_while_the_phone_boots_or_is_being_updated(phone):
    watch, fake, asked = phone
    await up(watch, fake)
    fake.android, fake.booted = False, False  # booting: no services yet, and it has not said it booted
    watch.doubt("device-1")
    await asyncio.sleep(0.3)
    assert not asked
    fake.booted = True
    watch.setup["device-1"] = 1.0  # device-ready restarts Android itself
    watch.doubt("device-1")
    await asyncio.sleep(0.3)
    assert not asked
    watch.present("device-1", False)


async def test_a_phone_without_android_does_not_count_as_ready_for_lack_of_a_home_app(phone):
    watch, fake, asked = phone
    fake.android = False  # "pm path" finds no package service: that is not "no home app"
    watch.present("device-1", True)
    await asyncio.sleep(0.1)
    assert not watch.ready("device-1") and watch.state("device-1") == watch.STARTING
    fake.android = True
    await settle(lambda: watch.ready("device-1"))
    watch.present("device-1", False)


async def test_never_a_loop_a_phone_that_goes_down_after_every_restart_is_left_as_broken(phone):
    watch, fake, asked = phone
    await up(watch, fake)
    watch.restarted["device-1"] = watch.MAX_RESTARTS  # restarted that often, and gone again each time
    fake.android = False
    watch.doubt("device-1")
    await settle(lambda: watch.state("device-1") == watch.BROKEN)
    watch.doubt("device-1")
    await asyncio.sleep(0.3)
    assert not asked and watch.state("device-1") == watch.BROKEN
    # A frozen phone uses the same count.
    await watch._frozen("device-1")
    assert not asked
    watch.present("device-1", False)


async def test_a_phone_that_stays_up_after_a_restart_gets_its_restarts_back(phone, monkeypatch):
    watch, fake, asked = phone
    monkeypatch.setattr(watch, "STEADY_SECONDS", 0)
    await up(watch, fake)
    watch.restarted["device-1"] = 2
    watch.doubt("device-1")
    await settle(lambda: "device-1" not in watch.restarted)
    watch.present("device-1", False)


async def test_a_restart_the_desktop_app_did_is_counted_even_when_the_asking_task_ends_first(phone, monkeypatch):
    watch, fake, asked = phone
    monkeypatch.undo()  # the real ask_restart
    for name in ("restarted", "_asking"):
        monkeypatch.setattr(watch, name, {})
    monkeypatch.setattr(watch.settings, "desktop_control_url", "http://desktop")
    monkeypatch.setattr(watch.settings, "desktop_phone_token", "secret")
    answers = [{"restarted": True, "pids": [1]}, {"restarted": False, "reason": "it was restarted less than ten minutes ago"}]

    class Reply:
        def __init__(self, body):
            self.body, self.text = body, str(body)

        def json(self):
            return self.body

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers):
            await asyncio.sleep(0.05)  # the emulator is ended meanwhile, and adb loses the phone
            return Reply(answers.pop(0))

    monkeypatch.setattr(watch.httpx, "AsyncClient", Client)
    asking = asyncio.create_task(watch.ask_restart("device-1"))
    await asyncio.sleep(0.01)
    asking.cancel()  # what present(slot, False) does to the task that asked
    await asyncio.sleep(0.1)
    assert watch.restarted == {"device-1": 1}
    await watch.ask_restart("device-1")  # refused: nothing was restarted, nothing is counted
    assert watch.restarted == {"device-1": 1}


async def test_a_phone_adb_lists_as_offline_for_long_is_restarted(phone, monkeypatch):
    watch, fake, asked = phone
    monkeypatch.setattr(watch, "unreachable", {})
    monkeypatch.setattr(watch, "UNREACHABLE_SECONDS", 0.05)
    watch.listed("device-1", "offline")  # an emulator that is booting says so for a few seconds
    await asyncio.sleep(0.02)
    assert not asked
    await settle(lambda: asked)  # one that stays so is not coming up
    assert watch.view("device-1")["detail"] == watch.UNREACHABLE
    watch.listed("device-1", "device")  # it came up (or was restarted): nothing more is asked
    await asyncio.sleep(0.02)
    count = len(asked)
    await asyncio.sleep(0.12)
    assert len(asked) == count and "device-1" not in watch.unreachable


async def test_a_booting_phone_that_comes_up_in_time_is_not_restarted(phone, monkeypatch):
    watch, fake, asked = phone
    monkeypatch.setattr(watch, "unreachable", {})
    monkeypatch.setattr(watch, "UNREACHABLE_SECONDS", 0.1)
    watch.listed("device-1", "offline")
    await asyncio.sleep(0.03)
    watch.listed("device-1", "device")
    await asyncio.sleep(0.15)
    assert not asked
    watch.listed("device-1", None)  # gone from adb's list: nothing left counting
    assert not watch.unreachable


async def test_an_offline_phone_that_used_up_its_restarts_is_left_as_broken(phone, monkeypatch):
    watch, fake, asked = phone
    monkeypatch.setattr(watch, "unreachable", {})
    monkeypatch.setattr(watch, "UNREACHABLE_SECONDS", 0.03)
    watch.restarted["device-1"] = watch.MAX_RESTARTS
    watch.listed("device-1", "offline")
    await settle(lambda: watch.state("device-1") == watch.BROKEN)
    await asyncio.sleep(0.1)
    assert not asked
    watch.listed("device-1", None)
