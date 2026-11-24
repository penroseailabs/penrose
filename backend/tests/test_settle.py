"""Reading the screen after an action: wait for it to stop changing (never longer than the old fixed wait), read
every app at once with its events, and never send an action twice."""

import asyncio
import time

import pytest
from app.bridge import devices
from app.bridge.devices import DeviceError, StaleReply, XposedDevice
from app.device.driver import Driver

from phone import FakePhone, snapshot


class FakeConnection:
    """A bridge socket: answers with `replies` in turn (an exception is raised instead of answered)."""

    def __init__(self, replies, sent):
        self.replies, self.sent, self.port = replies, sent, 1

    async def call(self, payload, timeout):
        self.sent.append(payload)
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return await reply(payload)
        return dict(reply)

    def close(self):
        pass


@pytest.fixture
def device(monkeypatch):
    phone = XposedDevice(serial="emulator-5570")
    phone.sent, phone.replies = [], []

    async def connect(process):
        return FakeConnection(phone.replies, phone.sent)

    async def drop(process):
        pass

    monkeypatch.setattr(phone, "connect", connect)
    monkeypatch.setattr(phone, "drop", drop)
    return phone


async def test_a_tap_that_timed_out_is_not_sent_again(device):
    device.replies[:] = [TimeoutError(), {"ok": True}]
    with pytest.raises(DeviceError):
        await device._call("click", "com.example", h=3)
    assert [p["cmd"] for p in device.sent] == ["click"]


async def test_a_tap_whose_reply_belongs_to_an_older_request_is_not_sent_again(device):
    device.replies[:] = [StaleReply("reply id 1 does not match request 2"), {"ok": True}]
    with pytest.raises(DeviceError):
        await device._call("set_text", "com.example", h=3, text="hi")
    assert len(device.sent) == 1


async def test_a_tap_to_a_bridge_that_had_gone_is_sent_again(device):
    device.replies[:] = [ConnectionError("bridge closed the connection"), {"ok": True}]
    assert (await device._call("click", "com.example", h=3))["ok"]
    assert len(device.sent) == 2


async def test_a_read_that_timed_out_is_asked_again(device):
    device.replies[:] = [TimeoutError(), {"ok": True, "windows": []}]
    assert (await device._call("snapshot", "com.example"))["ok"]
    assert len(device.sent) == 2


def window(process, focused=True):
    return {"token": process, "title": f"{process}/.Main", "type": "BASE_APPLICATION", "package": process,
            "pid": 1, "frame": [0, 0, 720, 1600], "mode": "fullscreen", "focused": focused, "process": process}


async def test_one_adb_call_reads_the_window_list_and_the_bridges(device, monkeypatch):
    shells = []

    async def shell(command, timeout=20):
        shells.append(command)
        return ("Window #0 Window{abc u0 com.example/com.example.Main}:\n  mDisplayId=0 isVisible=true ty=BASE_APPLICATION package=com.example\n"
                f"{device.SOCKETS_MARK}\n0000: 00000002 @relaybridge.com.example\n0000: @relaybridge.app.relay.home\n")

    monkeypatch.setattr(device.adb, "shell", shell)
    windows, bridges = await device.display_and_bridges()
    assert len(shells) == 1
    assert bridges == ["app.relay.home", "com.example"]
    assert [w["package"] for w in windows] == ["com.example"]


async def test_every_app_is_read_at_once_with_its_events(device, monkeypatch):
    started = []

    async def answer(payload):
        started.append(time.monotonic())
        await asyncio.sleep(0.2)
        return {"ok": True, "windows": [], "events_seq": 7, "events": [{"type": "toast", "seq": 7}]}

    device.replies[:] = [answer, answer]
    visible = [window("com.example"), window("com.other", focused=False)]
    begun = time.monotonic()
    shot = await device.snapshot_display(visible, ["com.example", "com.other"], events_since={"com.example": 5})
    assert time.monotonic() - begun < 0.35  # both at once, not 0.4 s one after the other
    assert sorted(p.get("events_since") for p in device.sent) == [0, 5]
    assert shot["events"]["com.example"] == {"seq": 7, "events": [{"type": "toast", "seq": 7}]}


async def test_after_a_tap_the_look_waits_for_the_app_not_a_fixed_time(device):
    device.replies[:] = [{"ok": True, "settled": True, "waited_ms": 160}, {"ok": True, "windows": []}]
    settle = {"quiet": 150, "max": 500, "min": 0, "process": "com.example"}
    begun = time.monotonic()
    shot = await device.snapshot_display([window("com.example")], ["com.example"], settle=settle)
    assert time.monotonic() - begun < 0.2  # the bridge said settled: no extra sleep here
    assert device.sent[0]["cmd"] == "settle" and device.sent[0]["from_call"] is True
    assert device.sent[1]["cmd"] == "snapshot" and device.sent[1]["settle"]["from_call"] is False
    assert shot["settle"]["settled"] is True


async def test_a_key_press_keeps_the_fixed_wait_and_never_waits_longer(device):
    device.replies[:] = [{"ok": True, "windows": []}]
    settle = {"quiet": 150, "max": 300, "min": 300, "process": "com.example"}
    begun = time.monotonic()
    await device.snapshot_display([window("com.example")], ["com.example"], settle=settle)
    took = time.monotonic() - begun
    assert 0.28 < took < 0.45
    assert [p["cmd"] for p in device.sent] == ["snapshot"]  # no settle call: the whole cap was the fixed wait


async def test_without_a_readable_app_the_look_waits_the_fixed_time(device):
    device.replies[:] = [DeviceError("no bridge"), DeviceError("no bridge"), {"ok": True, "windows": []}]

    async def failing(cmd, process=None, timeout=10, **args):
        if cmd == "settle":
            raise DeviceError("no bridge")
        return {"ok": True, "windows": []}

    device.call = failing
    begun = time.monotonic()
    answer, left = await device.settle({"quiet": 150, "max": 250, "min": 0, "process": "com.gone"})
    assert answer is None and left == 0 and time.monotonic() - begun >= 0.24


def test_taps_and_keys_settle_on_the_app_and_swipes_wait_for_their_fling_first():
    driver = Driver(FakePhone(snapshot()))
    driver.focused = "com.example"
    tap = driver.settle_for("com.example")
    assert tap["min"] == 0 and tap["process"] == "com.example" and tap["max"] == 500
    # A key or a device command has landed when adb returns: the app in front settles from then.
    key = driver.settle_for(None)
    assert key["min"] == 0 and key["max"] == 500 and key["process"] == "com.example"
    swipe = driver.settle_for(None, gesture=True)
    assert swipe["min"] == 300 and swipe["max"] == 500


async def test_the_driver_takes_events_from_the_look_itself():
    class Inline(FakePhone):
        async def snapshot_display(self, visible=None, bridges=None, settle=None, events_since=None):
            shot = await super().snapshot_display()
            shot["events"] = {"com.example": {"seq": 3, "events": [{"type": "toast", "text": "Saved", "t": 1}]}}
            return shot

    phone = Inline(snapshot())
    driver = Driver(phone)
    await driver.observe()
    assert driver.seq["com.example"] == 3
    assert any(e.get("text") == "Saved" for e in driver.pending_events)
    assert not phone.calls  # no separate "events" call was needed


def test_the_gateway_reply_sends_each_app_once():
    source = (devices.__file__.replace("bridge\\devices.py", "gateway\\actions.py")
              .replace("bridge/devices.py", "gateway/actions.py"))
    assert 'result.pop("bridge_snapshots", None)' in open(source, encoding="utf-8").read()
