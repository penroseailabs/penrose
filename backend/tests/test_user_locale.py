"""The user's time zone and language come from their browser or phone, never the server Relay runs on."""

import pytest
from fakes import FakeDevices, Script, make_state, runner_for
from test_api import api_client


def test_the_browser_reports_the_users_locale_and_the_phone_shown_goes_on_its_time_zone(store, monkeypatch):
    from app import api

    posts = []

    class Gateway:
        async def post(self, url, json=None, timeout=None):
            posts.append((url, json))

    monkeypatch.setattr(api.common, "gateway", Gateway())
    client = api_client(store, monkeypatch)
    assert client.get("/api/profile").json()["locale"] == {}  # nothing guessed from the server
    reply = client.post("/api/profile/locale", json={"timezone": "America/Chicago", "language": "en-US", "device": "device-1"})
    assert reply.json() == {"timezone": "America/Chicago", "language": "en-US"}
    assert posts == [("/devices/device-1/timezone", {"zone": "America/Chicago"})]
    assert client.get("/api/profile").json()["locale"]["timezone"] == "America/Chicago"


@pytest.mark.parametrize("body", [
    {"timezone": "America/Chicago; reboot", "language": "en-US"},  # it goes into a phone command
    {"timezone": "America/Chicago", "language": "en US'"},
])
def test_a_malformed_time_zone_or_language_is_refused(store, monkeypatch, body):
    assert api_client(store, monkeypatch).post("/api/profile/locale", json=body).status_code == 422
    assert store.user_locale() == {}


async def test_agents_read_the_users_locale_and_the_phone_follows_it_when_it_changes(store):
    devices = FakeDevices()
    runner = runner_for(store, Script(), devices=devices)
    state = make_state(store)
    assert runner.base_context(state)["locale"] == {}
    await runner.prepare_device("device-1", "run")
    assert devices.zones == []  # no browser has said yet: the phone's clock is left alone

    store.set_user_locale("Europe/Berlin", "de-DE")
    assert runner.base_context(state)["locale"] == {"timezone": "Europe/Berlin", "language": "de-DE"}
    await runner.prepare_device("device-1", "run")
    await runner.prepare_device("device-1", "run")
    assert devices.zones == [("device-1", "Europe/Berlin")]  # once, not every step
    store.set_user_locale("America/New_York", "de-DE")  # the user travelled
    await runner.prepare_device("device-1", "run")
    assert devices.zones[-1] == ("device-1", "America/New_York")


async def test_the_gateway_turns_automatic_time_zone_off_and_sets_the_users_as_root(monkeypatch):
    from app import gateway
    from fastapi import HTTPException

    calls = []

    async def adb(slot, *args, **kwargs):
        calls.append(args)
        return b"America/Chicago\n"

    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(gateway.core, "locks", {"device-1": gateway.core.asyncio.Lock()})
    monkeypatch.setattr(gateway.core, "adb", adb)
    request = gateway.devices.TimeZoneRequest(zone="America/Chicago")
    assert await gateway.devices.set_timezone("device-1", request) == {"zone": "America/Chicago"}
    shell = calls[0][1]
    # the emulated mobile network would put the computer's zone back if detection stayed on
    assert shell.startswith("su -c '") and "set_auto_detection_enabled false" in shell
    assert "--zone_id America/Chicago" in shell
    with pytest.raises(HTTPException):  # the phone kept another zone
        await gateway.devices.set_timezone("device-1", gateway.devices.TimeZoneRequest(zone="Asia/Tokyo"))
