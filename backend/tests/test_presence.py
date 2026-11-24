"""The user's presence wakes the phone early: the window or phone app came up, or they are typing."""

import asyncio
import json

import httpx

from app.api import devices


def gateway(calls):
    def handle(request):
        calls.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.url.path == "/devices":
            return httpx.Response(200, json=[{"id": "device-1", "connected": True}, {"id": "device-2", "connected": False}])
        return httpx.Response(200, json={"asleep": False})
    return httpx.AsyncClient(base_url="http://gateway", transport=httpx.MockTransport(handle))


async def test_presence_wakes_each_connected_phone_for_a_minute_and_signs_close_together_are_one(monkeypatch):
    calls = []
    monkeypatch.setattr(devices.common, "gateway", gateway(calls))
    monkeypatch.setattr(devices, "presence_seen", {"at": float("-inf")})
    assert await devices.presence() == {"waking": True}
    assert await devices.presence() == {"waking": False}  # typing again a moment later: nothing more to do
    await asyncio.gather(*devices.waking)
    assert calls == [("GET", "/devices", None), ("POST", "/devices/device-1/wake", {"for_seconds": 60})]
