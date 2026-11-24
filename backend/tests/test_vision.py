"""Screenshots: the gateway makes small JPEGs, the client takes only those, the trace keeps what an agent saw,
and an agent sees a picture only when it asks for one."""

import io
import os

import httpx
from app import api, gateway
from app.device.client import Devices
from fakes import (
    FakeDevices,
    Script,
    finish,
    make_state,
    orch,
    phone,
    report,
    run_step,
    runner_for,
    set_plan,
    step_spec,
    verdict,
    verify,
)
from fastapi.testclient import TestClient
from PIL import Image

JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


def png(width=1080, height=2400):
    out = io.BytesIO()
    # Noise, like a real screenshot; a flat colour would compress to nothing on its own.
    Image.frombytes("RGBA", (width, height), os.urandom(width * height * 4)).save(out, "PNG")
    return out.getvalue()


def test_gateway_shrinks_a_screenshot_to_a_small_jpeg():
    original = png()
    small = gateway.screen.shrink_screenshot(original, 540)
    image = Image.open(io.BytesIO(small))
    assert image.format == "JPEG" and image.size == (540, 1200)
    assert len(small) < len(original) / 5


async def test_gateway_serves_png_by_default_and_jpeg_on_request(monkeypatch):
    async def adb(slot, *args, **kwargs):
        return png()

    monkeypatch.setattr(gateway.core, "adb", adb)
    client = TestClient(gateway.app)  # no lifespan: no device polling
    assert client.get("/devices/device-1/screenshot").headers["content-type"] == "image/png"
    small = client.get("/devices/device-1/screenshot", params={"width": 540})
    assert small.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(small.content)).width == 540


async def test_devices_screenshot_needs_a_jpeg_from_the_gateway():
    def devices(response):
        d = Devices()
        d.client = httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(lambda r: response))
        return d

    ok = await devices(httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})).screenshot("device-1")
    assert ok == JPEG
    # A gateway that predates ?width= answers with the full PNG: better none than 2 MB of it.
    assert await devices(httpx.Response(200, content=png(10, 10), headers={"content-type": "image/png"})).screenshot("device-1") is None
    assert await devices(httpx.Response(500)).screenshot("device-1") is None


def test_the_trace_can_fetch_a_screenshot_the_agent_saw(store, monkeypatch):
    monkeypatch.setattr(api.common, "store", store)
    make_state(store)
    image_id = store.image("run", "a", JPEG)
    with TestClient(api.app) as client:
        response = client.get(f"/api/runs/run/images/{image_id}")
        assert response.content == JPEG and response.headers["content-type"] == "image/jpeg"
        assert client.get("/api/runs/run/images/999").status_code == 404
        client.delete("/api/chats/chat")
    assert store.rows("SELECT * FROM images") == []


async def test_the_verifier_reads_the_devices_state_and_sees_the_screenshot_it_asks_for(store):
    def orchestrator(context, n):
        if n == 0:
            return orch(set_plan(step_spec("wifi", "Turn Wi-Fi off")), run_step("wifi"))
        if n == 1:
            return orch(verify("wifi", "Wi-Fi is off", ["Read the device state"]))
        return orch(finish("Wi-Fi is off."))

    model = Script(Orchestrator=orchestrator, Subagent=lambda c, n: phone(report(summary="Turned it off.")),
                   Verifier=[phone({"tool": "request_screenshot"}), phone(verdict(True, "The tile shows Wi-Fi off"))])
    state = make_state(store)
    await runner_for(store, model, devices=FakeDevices(jpeg=JPEG)).run(state)
    first, second = model.of("Verifier")
    assert first["images"] == [] and "device_state:\nwifi: on" in first["context"]
    assert second["images"] == [JPEG]
    assert state["plan"][0]["verified"] is True
    # Every picture an agent saw is kept for the trace, and only those: the subagent asked for none.
    assert len(store.rows("SELECT id FROM images")) == 1
