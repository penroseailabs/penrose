"""The phone's screen: screenshots, and the live view: frames from the emulator's own stream (or, for a phone
started without it, an H.264 recording made inside the phone), fanned out as JPEG frames to every viewer."""

import asyncio
import contextlib
import io
import logging
import shutil
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from PIL import Image

from ..config import settings
from . import core, emulator_frames

router = APIRouter()
log = logging.getLogger(__name__)


MODEL_LONGEST_SIDE = 1600  # as WhatsApp's standard quality: a phone-sized picture is kept whole, bigger ones shrink to this
MODEL_JPEG_QUALITY = 85
# A screenshot takes well under a second on a phone that is keeping up. One that has not come in this long is not
# coming: a hanging app had each one wait out adb's 25 s, five times in a row (2026-11-24).
SCREENSHOT_TIMEOUT = 8


def shrink_screenshot(png: bytes, width: int, quality: int = 80) -> bytes:
    """A phone screenshot is 2 MB of PNG; a JPEG is a fraction of that. Never enlarged, and no wider than `width`
    or longer than MODEL_LONGEST_SIDE."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    scale = min(1, width / image.width, MODEL_LONGEST_SIDE / max(image.size))
    if scale < 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


@router.get("/devices/{slot}/screenshot")
async def screenshot(slot: str, width: int = Query(0, ge=0, le=2400)):
    """Full-size PNG for the live viewer; with ?width=, a downscaled JPEG for a model."""
    try:
        png = await core.adb(slot, "exec-out", "screencap", "-p", timeout=SCREENSHOT_TIMEOUT)
    except TimeoutError:
        raise HTTPException(503, "The phone did not give a picture in time: it is too busy right now.") from None
    if width:
        return Response(
            await asyncio.to_thread(shrink_screenshot, png, width, MODEL_JPEG_QUALITY),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


def rgb_to_jpeg(width: int, height: int, rgb: bytes) -> bytes:
    out = io.BytesIO()
    Image.frombuffer("RGB", (width, height), rgb, "raw", "RGB", 0, 1).save(out, "JPEG", quality=80)
    return out.getvalue()


class FrameHub:
    """One capture per device; latest-frame fanout avoids slow-client queues."""

    def __init__(self, slot):
        self.slot = slot
        self.condition = asyncio.Condition()
        self.frame = b""
        self.sequence = 0
        self.clients = 0
        self.task = None
        self.changed = 0.0

    async def publish(self, frame: bytes):
        async with self.condition:
            self.frame = frame
            self.sequence += 1
            self.condition.notify_all()

    async def settle(self):
        """Raw H.264 over a pipe holds back the newest frame until the next one starts, so a screen that
        stops changing would show the frame before last (and a still screen nothing at all). Half a second
        after the video goes quiet, a screenshot fills in the exact current screen."""
        settled = 0.0
        while True:
            await asyncio.sleep(0.25)
            if self.changed > settled and time.monotonic() - self.changed > 0.5:
                settled = time.monotonic()
                with contextlib.suppress(Exception):
                    png = await core.adb(self.slot, "exec-out", "screencap", "-p")
                    await self.publish(await asyncio.to_thread(shrink_screenshot, png, self.capture_size()[0]))

    async def capture(self):
        """Runs while anyone watches. A failure must not end it: viewers would keep the last frame forever."""
        while True:
            try:
                await self.capture_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Live view capture failed; starting it again")
                await asyncio.sleep(1)

    async def capture_once(self):
        while True:
            if await self.stream_from_emulator():
                await asyncio.sleep(
                    1
                )  # it ran, then ended (the phone restarted): look for it again
                continue
            self.changed = time.monotonic()  # the first screenshot arrives at once, video or not
            settle = asyncio.create_task(self.settle())
            try:
                await self.record()  # until the emulator's stream is offered
            finally:
                settle.cancel()

    def capture_size(self):
        """Frames are made no wider than the view needs (stream_width)."""
        size = core.dimensions.get(self.slot) or {"width": 1080, "height": 2400}
        width = min(settings.stream_width, size["width"])
        return width, round(width * size["height"] / size["width"] / 2) * 2

    async def stream_from_emulator(self) -> bool:
        """Frames from the emulator's own stream, while it offers one. They come only when the screen
        changes, and the newest becomes a JPEG at most stream_fps times a second. False if there is no
        stream (a phone started without gRPC), so the recorder is used instead."""
        found = emulator_frames.discover(core.slots[self.slot])
        if not found:
            return False
        channel, stream = emulator_frames.open_stream(*found, *self.capture_size())
        latest: list = [None]
        arrived = asyncio.Event()

        async def pull():
            async for frame in stream:
                if frame[0]:  # an inactive display sends 0 x 0
                    latest[0] = frame
                    arrived.set()

        puller = asyncio.create_task(pull())
        delivered = False
        try:
            while True:
                waiter = asyncio.create_task(arrived.wait())
                await asyncio.wait({waiter, puller}, return_when=asyncio.FIRST_COMPLETED)
                if not waiter.done():  # the stream ended: cancelled, or the emulator went away
                    waiter.cancel()
                    break
                arrived.clear()
                begun = time.monotonic()
                await self.publish(await asyncio.to_thread(rgb_to_jpeg, *latest[0]))
                delivered = True
                await asyncio.sleep(max(0.0, 1 / settings.stream_fps - (time.monotonic() - begun)))
        finally:
            stream.cancel()
            puller.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await puller
            await channel.close()
        return delivered

    async def clear_recorders(self):
        """Stop recorders a killed gateway left in the phone. Its adb lives on as an orphan and keeps the
        recording going, encoding every change for up to three minutes. Only the live view records."""
        with contextlib.suppress(Exception):
            await core.adb(self.slot, "shell", "pkill -x screenrecord", timeout=5)

    async def poll_screenshots(self):
        """Without ffmpeg (the desktop app ships none: its phone always offers its own stream), screenshots
        twice a second stand in until the emulator's stream is on offer. At least two seconds of them first,
        so a stale offer can't make this spin."""
        started = time.monotonic()
        while time.monotonic() - started < 2 or not emulator_frames.discover(core.slots[self.slot]):
            with contextlib.suppress(Exception):
                png = await core.adb(self.slot, "exec-out", "screencap", "-p")
                await self.publish(await asyncio.to_thread(shrink_screenshot, png, self.capture_size()[0]))
            await asyncio.sleep(0.5)

    async def record(self):
        """Record inside the phone, until a recording ends and the emulator's own stream is on offer (the
        phone restarted with it). Always one recording first, so a stale offer can't make this spin."""
        if not shutil.which(settings.ffmpeg_path):
            return await self.poll_screenshots()
        await self.clear_recorders()
        while True:
            capture = decoder = pump = None
            started = time.monotonic()
            try:
                width, height = self.capture_size()
                capture = await asyncio.create_subprocess_exec(
                    settings.adb_path,
                    "-s",
                    core.slots[self.slot],
                    "exec-out",
                    "screenrecord",
                    "--output-format=h264",
                    "--size",
                    f"{width}x{height}",
                    "--bit-rate",
                    "2000000",
                    "--time-limit",
                    "180",
                    "-",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                decoder = await asyncio.create_subprocess_exec(
                    settings.ffmpeg_path,
                    "-loglevel",
                    "error",
                    "-probesize",
                    "32768",
                    "-analyzeduration",
                    "0",
                    "-f",
                    "h264",
                    "-i",
                    "pipe:0",
                    # One JPEG per video frame. An fps filter would invent duplicates: raw H.264 carries
                    # no timestamps, so ffmpeg assumes 25 fps and pads a 60 fps phone out to 70.
                    "-fps_mode",
                    "passthrough",
                    "-vf",
                    f"scale={width}:-2",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "mjpeg",
                    "-q:v",
                    "5",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )

                async def feed(capture=capture, decoder=decoder):
                    try:
                        while chunk := await capture.stdout.read(65536):
                            decoder.stdin.write(chunk)
                            await decoder.stdin.drain()
                    finally:
                        decoder.stdin.close()

                pump = asyncio.create_task(feed())
                buffer = b""
                sent = 0.0
                # A still screen sends nothing for minutes; the recorder ends itself at its time limit.
                while chunk := await asyncio.wait_for(decoder.stdout.read(65536), 200):
                    buffer += chunk
                    while (end := buffer.find(b"\xff\xd9")) >= 0:
                        frame, buffer = buffer[: end + 2], buffer[end + 2 :]
                        start = frame.find(b"\xff\xd8")
                        if start >= 0:
                            self.changed = time.monotonic()
                            # At most stream_fps frames; the settle screenshot brings the last one.
                            if self.changed - sent >= 1 / settings.stream_fps:
                                sent = self.changed
                                await self.publish(frame[start:])
                    if len(buffer) > 8_000_000:
                        raise RuntimeError("Oversized video frame")
                if time.monotonic() - started < 3:
                    await asyncio.sleep(2)  # a recorder that ends at once: don't spin restarting it
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(2)
            finally:
                if pump:
                    pump.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await pump
                for proc in (capture, decoder):
                    if proc and proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError):
                            proc.kill()
                        await proc.wait()
            if emulator_frames.discover(core.slots[self.slot]):
                return

    async def frames(self, request, fps=0):
        """Every new frame for one viewer, or with `fps` at most that many a second: always the newest, so a
        slower viewer skips frames rather than falling behind."""
        self.clients += 1
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self.capture())
        sequence = -1
        sent = 0.0
        try:
            while not await request.is_disconnected():
                if fps:
                    await asyncio.sleep(max(0.0, sent + 1 / fps - time.monotonic()))
                    sent = time.monotonic()
                async with self.condition:
                    try:
                        await asyncio.wait_for(
                            self.condition.wait_for(
                                lambda seen=sequence: self.sequence != seen and bool(self.frame)
                            ),
                            5,  # the viewer treats 12 s of silence as a dead stream
                        )
                    except TimeoutError:
                        # A still screen sends no frames: repeat the last one so the viewer's
                        # connection stays open for the next change.
                        if not self.frame:
                            continue
                    sequence, frame = self.sequence, self.frame
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
        finally:
            self.clients -= 1
            if self.clients == 0 and self.task:
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task


hubs = {slot: FrameHub(slot) for slot in core.slots}


@router.get("/devices/{slot}/stream")
async def stream(slot: str, request: Request, fps: int = Query(0, ge=0, le=60)):
    """The live view as MJPEG; `fps` caps it for a viewer on a slow link (the companion phone app)."""
    if slot not in hubs:
        raise HTTPException(404, "Unknown device")
    return StreamingResponse(
        hubs[slot].frames(request, fps),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )
