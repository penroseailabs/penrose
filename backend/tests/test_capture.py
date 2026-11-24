"""Requests to a model provider are kept as files only while the switch file exists (agents/capture.py)."""

import asyncio
import gzip
import json

import httpx
from app.agents import capture
from app.config import settings

STREAM = b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\ndata: {"choices":[{"delta":{"content":"lo"}}]}\n\ndata: [DONE]\n\n'


class Slow(httpx.AsyncByteStream):
    """A reply that arrives over the wire, as a real one does (one built with `content=` counts as read already)."""

    def __init__(self, parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            yield part


def provider(request):
    if request.url.path.endswith("/stream"):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Slow([STREAM[:40], STREAM[40:]]))
    if request.url.path.endswith("/packed"):
        return httpx.Response(200, headers={"content-encoding": "gzip", "content-type": "application/json"},
                              stream=Slow([gzip.compress(b'{"ok": true}')]))
    return httpx.Response(429, stream=Slow([b'{"error": {"message": "slow down"}}']))


def client():
    return httpx.AsyncClient(transport=httpx.MockTransport(provider), event_hooks=capture.hooks())


def kept(tmp_path, run="other"):
    files = sorted((tmp_path / "traces" / run).glob("*")) if (tmp_path / "traces" / run).exists() else []
    return {kind: [f for f in files if f.name.endswith(kind)] for kind in ("request.json", "response.txt", "meta.json")}


def switch(tmp_path, monkeypatch, state=True):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    if state:
        (tmp_path / "traces").mkdir()
        (tmp_path / "traces" / "capture.on").write_text("")


def test_nothing_is_kept_without_the_switch_file(tmp_path, monkeypatch):
    switch(tmp_path, monkeypatch, state=False)

    async def go():
        async with client() as c:
            assert (await c.post("https://p/packed", json={"a": 1})).json() == {"ok": True}

    asyncio.run(go())
    assert not (tmp_path / "traces").exists()


def test_a_request_is_kept_as_the_bytes_sent_and_the_reply_as_text_without_its_headers(tmp_path, monkeypatch):
    switch(tmp_path, monkeypatch)
    body = {"model": "m", "messages": [{"role": "user", "content": "Screen:\n[1] ünïcode"}]}

    async def go():
        async with client() as c:
            with capture.labelled(run="run1", stage="Subagent", call_id="c1"):
                reply = await c.post("https://p/packed", headers={"Authorization": "Bearer sk-secret"}, json=body)
            assert reply.json() == {"ok": True}  # the caller still gets the reply, unpacked as ever

    asyncio.run(go())
    files = kept(tmp_path, "run1")
    assert json.loads(files["request.json"][0].read_bytes()) == body
    assert files["response.txt"][0].read_bytes() == b'{"ok": true}'
    meta = json.loads(files["meta.json"][0].read_text(encoding="utf-8"))
    assert (meta["status"], meta["stage"], meta["call_id"], meta["whole"]) == (200, "Subagent", "c1", True)
    assert meta["url"] == "https://p/packed"
    assert not any(b"sk-secret" in f.read_bytes() for kind in files.values() for f in kind)


def test_a_stream_is_kept_as_it_came_and_one_cut_short_says_so(tmp_path, monkeypatch):
    switch(tmp_path, monkeypatch)

    async def go():
        async with client() as c:
            async with c.stream("POST", "https://p/stream", json={"stream": True}) as response:
                async for line in response.aiter_lines():
                    if line.strip() == "data: [DONE]":
                        break  # as the app reads a stream: it stops at the provider's last word, not at the close
            async with c.stream("POST", "https://p/stream", json={"stream": True}) as response:
                async for _ in response.aiter_raw():
                    break  # the turn was stopped

    asyncio.run(go())
    files = kept(tmp_path)
    assert files["response.txt"][0].read_bytes() == STREAM
    whole = [json.loads(f.read_text(encoding="utf-8"))["whole"] for f in files["meta.json"]]
    assert whole == [True, False]
    assert files["response.txt"][1].read_bytes() == STREAM[:40]


def test_an_error_reply_is_kept_too_and_each_retry_is_its_own_request(tmp_path, monkeypatch):
    switch(tmp_path, monkeypatch)

    async def go():
        async with client() as c:
            for _ in range(2):
                assert (await c.post("https://p/refused", json={})).status_code == 429

    asyncio.run(go())
    files = kept(tmp_path)
    assert len(files["request.json"]) == 2
    assert json.loads(files["response.txt"][0].read_bytes())["error"]["message"] == "slow down"


def test_a_reply_from_the_anthropic_clients_library_is_kept_and_still_closes(tmp_path, monkeypatch):
    """The Anthropic client runs on httpx2, which closes only a stream of its own class."""
    import httpx2

    class Slow2(httpx2.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: message_stop\ndata: {"type": "message_stop"}\n\n'

    switch(tmp_path, monkeypatch)

    async def go():
        transport = httpx2.MockTransport(lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Slow2()))
        async with httpx2.AsyncClient(transport=transport, event_hooks=capture.hooks()) as c:
            async with c.stream("POST", "https://p/v1/messages", json={"a": 1}) as reply:
                assert b"message_stop" in await reply.aread()

    asyncio.run(go())
    files = kept(tmp_path)
    assert json.loads(files["meta.json"][0].read_text(encoding="utf-8"))["whole"] is True
    assert b"message_stop" in files["response.txt"][0].read_bytes()
