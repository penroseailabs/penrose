import httpx
import pytest
from app.device.client import rejection_detail
from app.models import Action
from app.store import Store


@pytest.mark.parametrize("command,args", [
    ('adb shell pm list packages "amazon"', ('shell', 'pm list packages "amazon"')),
    ('adb shell pm list packages "maps"', ('shell', 'pm list packages "maps"')),
    ('adb shell am start -a android.intent.action.VIEW -d "market://search?q=Google+Maps"',
     ('shell', 'am start -a android.intent.action.VIEW -d "market://search?q=Google+Maps"')),
    ('adb shell am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.example.app',
     ('shell', 'am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.example.app')),
    ('adb shell am start -n com.example.app/.MainActivity',
     ('shell', 'am start -n com.example.app/.MainActivity')),
    ('adb shell pm list packages | grep maps', ('shell', 'pm list packages | grep maps')),
    ('adb shell echo "one; two" && echo three', ('shell', 'echo "one; two" && echo three')),
    # What a host shell would send: one quoted expression unquoted, chained `adb shell` calls joined.
    ('adb shell "echo \'a b\' > /data/local/tmp/f && cat /data/local/tmp/f"',
     ('shell', "echo 'a b' > /data/local/tmp/f && cat /data/local/tmp/f")),
    ('adb shell am force-stop com.android.chrome; adb shell am start -d "https://x.test/?a=1&b=2"',
     ('shell', 'am force-stop com.android.chrome; am start -d "https://x.test/?a=1&b=2"')),
    ('adb version', ('version',)),
    ('adb pull /sdcard/example.txt "local file.txt"',
     ('pull', '/sdcard/example.txt', 'local file.txt')),
])
async def test_gateway_executes_adb_without_rewriting(command, args, tmp_path, monkeypatch):
    from app import gateway

    lease_store = Store(tmp_path)
    lease_store.lease(["device-1"], "agent")
    monkeypatch.setattr(gateway.core, "store", lease_store)
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    calls = []

    async def command_result(*values, **kwargs):
        calls.append(values)
        return b"package:com.example.app\n", b"", 0

    monkeypatch.setattr(gateway.core, "command_result", command_result)
    async with httpx.AsyncClient(base_url="http://gateway", transport=httpx.ASGITransport(app=gateway.app)) as client:
        action = Action(kind="adb_command", command=command).model_dump()
        denied = await client.post("/devices/device-1/actions", json={"owner": "other", "action": action})
        assert denied.status_code == 409
        assert calls == []
        response = await client.post("/devices/device-1/actions", json={"owner": "agent", "action": action})
        assert response.status_code == 200
        assert response.json() == {
            "ok": True, "kind": "adb_command", "exit_code": 0,
            "output": "package:com.example.app\n", "stderr": "",
        }
    # No implicit package check or redirect to a different command.
    assert calls == [("-s", "emulator-5554", *args)]


@pytest.mark.parametrize("exit_code", [0, 1])
async def test_gateway_returns_command_errors_to_agent(exit_code, monkeypatch):
    from app import gateway

    calls = []

    class Process:
        returncode = exit_code

        async def communicate(self):
            return b"Error: unable to resolve Intent\n", b"diagnostic\n"

    async def create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(gateway.core.asyncio, "create_subprocess_exec", create_subprocess_exec)
    monkeypatch.setattr(gateway.core, "check_owner", lambda *args: None)
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    result = await gateway.actions.perform("device-1", gateway.actions.ActionRequest(owner="agent", action=Action(
        kind="adb_command", command='adb shell am start -n com.missing.app/.MainActivity')))
    assert result["ok"] == (exit_code == 0)
    assert result["exit_code"] == exit_code
    assert "unable to resolve" in result["output"]
    assert result["stderr"] == "diagnostic\n"
    assert calls == [(gateway.core.settings.adb_path, "-s", "emulator-5554", "shell",
                      "am start -n com.missing.app/.MainActivity")]


@pytest.mark.parametrize("command", ["", "adb", "adb   ", "powershell echo hi", 'adb pull "unterminated'])
async def test_gateway_rejects_malformed_adb_request(command, monkeypatch):
    from app import gateway
    from fastapi import HTTPException

    monkeypatch.setattr(gateway.core, "check_owner", lambda *args: None)

    async def command_result(*args, **kwargs):
        pytest.fail("Malformed requests must not start a process")

    monkeypatch.setattr(gateway.core, "command_result", command_result)
    with pytest.raises(HTTPException) as error:
        await gateway.actions.perform("device-1", gateway.actions.ActionRequest(owner="agent", action=Action(
            kind="adb_command", command=command)))
    assert error.value.status_code == 422


def test_gateway_validation_errors_are_explained():
    response = httpx.Response(422, json={"detail": [
        {"loc": ["body", "action", "kind"], "msg": "Input should be 'tap'"},
    ]})
    message = rejection_detail(response)
    assert "action.kind: Input should be 'tap'" in message
    assert "outdated" in message


async def test_gateway_puts_the_computers_location_on_the_emulator_gps(monkeypatch):
    from app import gateway

    calls = []

    async def adb(slot, *args, **kwargs):
        calls.append(args)
        return b"OK" if args[0] == "emu" else b""

    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(gateway.core, "locks", {"device-1": gateway.core.asyncio.Lock()})
    monkeypatch.setattr(gateway.core, "adb", adb)
    result = await gateway.devices.set_location(
        "device-1", gateway.devices.LocationRequest(lat=12.9716, lng=77.5946, accuracy=8)
    )
    assert result["lat"] == 12.9716
    # the emulator's GPS hardware (longitude first), never a mock test provider
    assert ("emu", "geo", "fix", "77.5946000", "12.9716000", "0", "9") in calls
    assert not any("set-test-provider" in " ".join(c) for c in calls)
    assert gateway.devices.locations["device-1"].lng == 77.5946


async def test_the_users_phone_location_holds_until_a_message_comes_from_the_computer(monkeypatch):
    from app import gateway

    fixes = []

    async def adb(slot, *args, **kwargs):
        if args[0] == "emu":
            fixes.append((float(args[4]), float(args[3])))  # lat, lng
        return b"OK" if args[0] == "emu" else b""

    devices = gateway.devices
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(gateway.core, "locks", {"device-1": gateway.core.asyncio.Lock()})
    monkeypatch.setattr(gateway.core, "adb", adb)
    monkeypatch.setattr(devices, "locations", {})
    monkeypatch.setattr(devices, "computer_fixes", {})
    monkeypatch.setattr(devices, "phone_held", set())

    await devices.set_location("device-1", devices.LocationRequest(lat=40.7, lng=-74.0))  # the computer
    await devices.set_location("device-1", devices.LocationRequest(lat=37.7, lng=-122.4, source="phone"))
    # the computer's regular update waits while the user's phone's fix holds
    held = await devices.set_location("device-1", devices.LocationRequest(lat=40.8, lng=-74.1))
    assert held["held"] and held["lat"] == 37.7
    assert fixes == [(40.7, -74.0), (37.7, -122.4)]
    # a message from the computer: back to the computer's latest position
    assert (await devices.release_location("device-1"))["lat"] == 40.8
    assert fixes[-1] == (40.8, -74.1)
    # nothing held: releasing again changes nothing
    await devices.release_location("device-1")
    assert len(fixes) == 3


def test_location_rejects_out_of_range():
    from app import gateway
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        gateway.devices.LocationRequest(lat=91, lng=0)


async def test_gateway_reports_when_the_emulator_refuses_the_location(monkeypatch):
    from app import gateway
    from fastapi import HTTPException

    async def adb(slot, *args, **kwargs):
        return b"KO: bad command" if args[0] == "emu" else b""

    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator-5554"})
    monkeypatch.setattr(gateway.core, "locks", {"device-1": gateway.core.asyncio.Lock()})
    monkeypatch.setattr(gateway.core, "adb", adb)
    with pytest.raises(HTTPException) as error:
        await gateway.devices.set_location("device-1", gateway.devices.LocationRequest(lat=1, lng=2))
    assert error.value.status_code == 503
