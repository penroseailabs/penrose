import json

import httpx
import pytest
from app.config import settings
from app.device.client import Devices
from app.errors import DeviceError
from fakes import (
    FakeDevices,
    Script,
    click,
    finish,
    make_state,
    orch,
    phone,
    report,
    run_step,
    runner_for,
    sections,
    set_plan,
    step_spec,
)
from fastapi.testclient import TestClient
from phone import FakePhone, screen_of


def one_step(context, n):
    if n == 0:
        return orch(set_plan(step_spec("a")), run_step("a"))
    return orch(finish("Done."))


async def test_a_busy_device_is_waited_for_without_asking_the_user(store, monkeypatch):
    import app.runtime.runner as runner_module

    real_sleep = runner_module.asyncio.sleep

    async def instant(seconds):
        await real_sleep(0)

    monkeypatch.setattr(runner_module.asyncio, "sleep", instant)
    state = make_state(store)
    devices = FakeDevices()
    attempts = []

    async def acquire(owner, wait=0):
        attempts.append(owner)
        return None if len(attempts) == 1 else "device-1"

    devices.acquire = acquire
    await runner_for(store, Script(Orchestrator=one_step, Subagent=lambda c, n: phone(report())), devices=devices).run(state)
    assert attempts == ["run", "run"] and state["status"] == "completed"
    assert "questions" not in [r["kind"] for r in store.rows("SELECT kind FROM events")]


def park(store, run_id, status):
    """Another chat's run, saved with `status`."""
    store.execute("INSERT OR IGNORE INTO chats VALUES (?,?,?)", ("other", "other", 0))
    state = {"id": run_id, "chat_id": "other", "status": status}
    store.execute("INSERT INTO runs VALUES (?,?,?,?,?)", (run_id, "other", status, json.dumps(state), 0))


def held_by(devices, holder):
    """`devices` whose one phone `holder` holds until it is released."""

    async def acquire(owner, wait=0):
        devices.acquired.append(owner)
        return None if holder not in devices.released else "device-1"

    async def holders():
        return {} if holder in devices.released else {"device-1": holder}

    devices.acquire, devices.holders = acquire, holders
    return devices


async def test_a_run_waiting_for_the_user_gives_its_phone_to_a_new_task(store):
    park(store, "parked", "waiting_user")
    state = make_state(store)
    devices = held_by(FakeDevices(), "parked")
    await runner_for(store, Script(Orchestrator=one_step, Subagent=lambda c, n: phone(report())), devices=devices).run(state)
    assert state["status"] == "completed" and devices.released[0] == "parked"


async def test_a_working_run_keeps_its_phone(store, monkeypatch):
    import app.runtime.runner as runner_module

    real_sleep = runner_module.asyncio.sleep

    async def instant(seconds):
        await real_sleep(0)

    monkeypatch.setattr(runner_module.asyncio, "sleep", instant)
    monkeypatch.setattr(settings, "device_wait_seconds", 0.05)
    park(store, "busy", "running")
    state = make_state(store)
    devices = held_by(FakeDevices(), "busy")
    await runner_for(store, Script(Orchestrator=one_step, Subagent=lambda c, n: phone(report())), devices=devices).run(state)
    assert "busy" not in devices.released
    assert "stayed busy" in json.dumps(state["plan"])


async def test_an_uncertain_tap_is_shown_to_the_next_turn_and_never_replayed(store):
    class Flaky(FakePhone):
        async def call(self, cmd, process=None, timeout=10, **args):
            if cmd == "click":
                self.calls.append({"cmd": cmd, **args})
                raise DeviceError("bridge com.example: TimeoutError")
            return await super().call(cmd, process, timeout, **args)

    def subagent(context, n):
        if n == 0:
            return phone(click(1))
        assert "TimeoutError" in sections(context)["Last result"] and "Incident (1 in a row)" in context
        return phone(report(summary="Order state checked on screen"))

    flaky = Flaky(screen_of("Item"))
    state = make_state(store)
    await runner_for(store, Script(Orchestrator=one_step, Subagent=subagent), phone=flaky).run(state)
    assert len(flaky.calls) == 1
    assert [t["status"] for t in state["plan"][0]["turns"] if t.get("action")] == ["uncertain"]
    assert "action_uncertain" in [r["kind"] for r in store.rows("SELECT kind FROM events")]


def test_sign_in_protects_workspace_and_streams(store, monkeypatch):
    from app import api

    monkeypatch.setattr(api.common, "store", store)
    monkeypatch.setattr(settings, "login_username", "tester")
    monkeypatch.setattr(settings, "login_password", "test-password")
    with TestClient(api.app) as client:
        for path in (
            "/api/chats",
            "/api/devices",
            "/api/runs/run/stream",
            "/api/devices/device-1/stream",
        ):
            assert client.get(path).status_code == 401
        assert (
            client.post(
                "/api/auth/login", json={"username": "tester", "password": "wrong"}
            ).status_code
            == 401
        )
        response = client.post(
            "/api/auth/login", json={"username": "tester", "password": "test-password"}
        )
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/api/chats").status_code == 200
        assert client.get("/api/auth/session").json()["user_id"] == settings.user_id


async def test_cloud_unavailability_is_retryable():
    devices = Devices()
    await devices.client.aclose()
    devices.client = httpx.AsyncClient(
        base_url="http://gateway",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    try:
        assert await devices.acquire("run:a") is None
    finally:
        await devices.close()


async def test_adb_connection_is_not_ready_until_android_boots(monkeypatch):
    from app import gateway

    async def command(*args, **kwargs):
        return b"List of devices attached\nemulator:5555\tdevice\n"

    async def adb(*args, **kwargs):
        return b"0\n"

    monkeypatch.setattr(gateway.core, "command", command)
    monkeypatch.setattr(gateway.core, "adb", adb)
    monkeypatch.setattr(gateway.core, "slots", {"device-1": "emulator:5555"})
    assert not (await gateway.devices.device_list())[0]["connected"]


async def test_action_precondition_failure_reaches_agent_repair():
    devices = Devices()
    await devices.client.aclose()
    devices.client = httpx.AsyncClient(
        base_url="http://gateway",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(422, json={"detail": "App is not installed"})
        ),
    )
    try:
        with pytest.raises(ValueError, match="not installed"):
            await devices.action(
                "device-1", "owner", {"kind": "install_app", "package": "com.example.missing"}
            )
    finally:
        await devices.close()
