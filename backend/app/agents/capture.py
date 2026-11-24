"""Every request to a model provider and its raw reply, kept as files while a switch file exists.

For reading exactly what an agent was sent (its instructions, its tools, the state, the screen) and what came
back, as the provider saw it: the bytes of the request's body and the bytes of the reply, nothing rebuilt.

The switch is the file <data>/traces/capture.on. Without it nothing is kept, and a request costs one look for
that file. With it, each request is written under <data>/traces/<run id>/ (or other/ for a call no run made)
once its reply has ended, so no call waits on the disk:
  <time>-<pid>-<n>.request.json   the body sent
  <time>-<pid>-<n>.response.txt   the reply (a stream's events as they came, or the JSON)
  <time>-<pid>-<n>.meta.json      when, where to, the status, how long, and which agent turn it was
A phone turn is about 150 KB, so a long run is tens of megabytes: delete the switch file when the look is over.

Never kept: the request's headers (they carry the API key) and the reply's.
"""

import contextlib
import contextvars
import itertools
import json
import logging
import os
import time
import zlib

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# Which agent turn the requests made now belong to: {"run", "chat", "stage", "node_id", "call_id"}.
turn = contextvars.ContextVar("capture_turn", default=None)
numbers = itertools.count(1)


def folder():
    return settings.data_dir / "traces"


def on():
    return (folder() / "capture.on").exists()


@contextlib.contextmanager
def labelled(**label):
    """The requests made inside belong to this agent turn (the runner's call id is the one in the run's events)."""
    token = turn.set(label)
    try:
        yield
    finally:
        turn.reset(token)


def inflate(raw, encoding):
    """The reply's body as text bytes: a hook sees it as it crossed the wire, before httpx unpacks it."""
    if encoding not in ("gzip", "deflate", "x-gzip"):
        return raw
    for bits in (47, -15):  # gzip or zlib by its header; then bare deflate
        try:
            return zlib.decompressobj(bits).decompress(raw)
        except zlib.error:
            continue
    return raw


class Tee:
    """The reply's stream, handed on unchanged, with a copy written when it is closed."""

    def __init__(self, stream, response, note):
        self.stream, self.response, self.note = stream, response, note
        self.chunks, self.whole, self.written = [], False, False

    async def __aiter__(self):
        async for chunk in self.stream:
            self.chunks.append(chunk)
            yield chunk
        self.whole = True

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            self.write()

    def write(self):
        if self.written:
            return
        self.written = True
        try:
            request, response, note = self.response.request, self.response, self.note
            reply = inflate(b"".join(self.chunks), response.headers.get("content-encoding", "").strip().lower())
            label = note["turn"] or {}
            where = folder() / str(label.get("run") or "other")
            where.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(note["started"])) + f"-{int(note['started'] * 1000) % 1000:03d}"
            name = f"{stamp}-{os.getpid()}-{next(numbers):04d}"
            (where / f"{name}.request.json").write_bytes(request.content)
            (where / f"{name}.response.txt").write_bytes(reply)
            (where / f"{name}.meta.json").write_text(json.dumps({
                "started": note["started"], "seconds": round(time.time() - note["started"], 3),
                "method": request.method, "url": str(request.url), "status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "request_bytes": len(request.content), "response_bytes": len(reply),
                # False: the reply was cut short (the turn was stopped, or the connection dropped). A stream is whole
                # at the provider's closing line: its readers stop there, before the connection closes.
                "whole": self.whole or reply.rstrip().endswith(b"data: [DONE]") or b"event: message_stop" in reply[-200:],
                **label,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # a capture must never cost the turn it watches
            logger.warning("A model request could not be captured.", exc_info=True)


async def sent(request):
    if on():
        request.extensions["capture"] = {"started": time.time(), "turn": turn.get()}


tees = {}


def tee(stream):
    """The Tee class for this stream's own library. The Anthropic client runs on httpx2, and each library closes
    only a stream of its own AsyncByteStream class: one of httpx's on an httpx2 reply fails the request
    ("Attempted to call an async close on a sync stream")."""
    base = next((c for c in type(stream).__mro__ if c.__name__ == "AsyncByteStream"), httpx.AsyncByteStream)
    if base not in tees:
        tees[base] = type("Tee", (Tee, base), {})
    return tees[base]


async def received(response):
    note = response.request.extensions.get("capture")
    if note is not None:
        response.stream = tee(response.stream)(response.stream, response, note)


def hooks():
    """For an httpx client's `event_hooks`: each client gets lists of its own."""
    return {"request": [sent], "response": [received]}
