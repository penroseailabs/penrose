"""The phone's side of a voice call penrose holds (runtime/call.py): one WebSocket per call.

The worker that holds the phone's lease connects to /devices/{slot}/call-audio?owner=<lease owner>. Then:
- the gateway sends what the phone plays (everyone else on the call) as binary frames of 24 kHz mono s16le PCM,
  straight from the emulator (gRPC streamAudio; the phone must be started with audio, `-audio none`);
- the worker sends penrose's voice the same way. relay-bridge's CallAudio, run as root, feeds it to every app
  recording "the mic" for a call (an Android audio policy: no app is changed or hooked for this);
- text frames: "flush" drops penrose's voice that was not heard yet (the other person started talking);
  "hangup" ends the call in the app and closes; "release" closes without touching the app (the line was opened
  for a call that was never placed).
The gateway answers "ready" once both directions are up, or "error: <why>" and closes. It sends "connected" once
Android counts a call as connected (`picked_up`): the other person picked up, heard or not. However the socket closes
(but "release"), the call is hung up; the mic is the phone's own again either way.
"""

import array
import asyncio
import contextlib
import hmac
import logging
import re
import struct

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..config import settings
from . import core, emulator_frames

log = logging.getLogger("gateway.call_audio")
router = APIRouter()

SOCKET = "relay_callaudio"
RATE = 24000  # what the Realtime model speaks and hears; the injector plays 48 kHz
HELPER = (
    "export CLASSPATH=$(pm path app.relay.bridge | head -n 1 | cut -d: -f2); "
    f"exec app_process /system/bin --nice-name=relay-callaudio app.relay.bridge.CallAudio serve {SOCKET}"
)
# The end-call control of the call apps (Meet, Teams, WhatsApp, the dialer), by its label or content description.
HANG_UP = re.compile(r'(?:text|content-desc)="(?:Leave call|End call|Hang up|End|Leave)"', re.I)


# A call Android's telecom counts as connected, as `dumpsys telecom` lists it.
ACTIVE_CALL = re.compile(r"Call id=[^,\]\s]+, state=ACTIVE")


async def picked_up(serial):
    """Returns once Android counts a call on the phone as connected. The call apps that hold their calls through
    telecom (WhatsApp, Teams, the dialer) mark one ACTIVE when the other person picks up; for an app that keeps its
    calls to itself this never returns, and the call goes by what is heard alone. Asked once a second, only while
    a call's line is open and nobody has picked up."""
    while True:
        stdout, _, code = await core.command_result("-s", serial, "shell", "dumpsys telecom", timeout=10)
        if not code and ACTIVE_CALL.search(stdout.decode(errors="replace")):
            return
        await asyncio.sleep(1)


class Upsampler:
    """24 kHz to 48 kHz mono s16le: each sample after the midpoint from the one before (that one carries over
    from the previous chunk, so chunk edges do not click)."""

    def __init__(self):
        self.last = 0

    def __call__(self, pcm: bytes) -> bytes:
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if not samples:
            return b""
        before = array.array("h", [self.last])
        before.extend(samples[:-1])
        out = array.array("h", bytes(4 * len(samples)))
        out[0::2] = array.array("h", ((a + b) >> 1 for a, b in zip(before, samples)))
        out[1::2] = samples
        self.last = samples[-1]
        return out.tobytes()


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    """One message to CallAudio on the phone: [kind][4-byte big-endian length][payload]."""
    return kind + struct.pack(">I", len(payload)) + payload


END_LABEL = re.compile(r"(?:leave call|end call|end the call|hang up|end|leave)", re.I)
END_ID = re.compile(r"(?:end_?call|hang_?up|leave_?call)", re.I)


def end_control(nodes):
    """The end-call control among a bridge snapshot's nodes: a button named "End call", "Leave call"..."""
    for node in nodes:
        label = (node.get("desc") or node.get("text") or "").strip()
        if END_LABEL.fullmatch(label) or END_ID.search(node.get("id") or ""):
            return node
        if found := end_control(node.get("c") or []):
            return found
    return None


async def hang_up(slot):
    """Tap the call app's end-call control, if one is on screen (the other person may have hung up already).
    The screen is read through the bridge, at once: uiautomator waits for a still screen, which a call's ticking
    timer never gives it, so its dump came ~20 s late and the call stayed up that long after penrose had gone.
    uiautomator only for an app no bridge reads."""
    phone = core.phones.get(slot)
    if phone is not None:
        try:
            snapshot = await asyncio.wait_for(phone.snapshot_display(), 8)
            for window in reversed(snapshot.get("windows") or []):  # top first
                if (node := end_control(window.get("nodes") or [])) and node.get("h"):
                    await phone.call("click", process=window.get("process"), h=node["h"])
                    return True
        except Exception as exc:  # noqa: BLE001 - the dump below is the fallback
            log.warning("hang-up through the bridge on %s failed: %r", slot, exc)
    serial = core.slots[slot]
    await core.command_result("-s", serial, "shell", "rm -f /data/local/tmp/relay-call.xml; "
                              "uiautomator dump /data/local/tmp/relay-call.xml", timeout=20)
    stdout, _, _ = await core.command_result("-s", serial, "shell", "cat /data/local/tmp/relay-call.xml", timeout=10)
    for node in re.findall(r"<node [^>]*>", stdout.decode(errors="replace")):
        if HANG_UP.search(node) and (box := re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)):
            x1, y1, x2, y2 = map(int, box.groups())
            await core.command_result("-s", serial, "shell", f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
            return True
    return False


@router.websocket("/devices/{slot}/call-audio")
async def call_audio(ws: WebSocket, slot: str, owner: str = ""):
    if settings.gateway_token and not hmac.compare_digest(
        ws.headers.get("x-gateway-token", "").encode(), settings.gateway_token.encode()
    ):
        await ws.close(code=4401)
        return
    await ws.accept()

    async def fail(reason):
        with contextlib.suppress(Exception):
            await ws.send_text(f"error: {reason}")
            await ws.close()

    if slot not in core.slots:
        return await fail("unknown phone")
    try:
        core.check_owner(slot, owner)
    except HTTPException:
        return await fail("the phone is not leased to this caller")
    serial = core.slots[slot]
    found = emulator_frames.discover(serial)
    if not found:
        return await fail("the phone's audio controls are off (it was started without gRPC): restart penrose")

    helper = port = writer = channel = pickup = None
    hang = True  # however the socket closes, the call is hung up (but "release")
    try:
        helper = await asyncio.create_subprocess_exec(
            settings.adb_path, "-s", serial, "shell", f"su -c '{HELPER}'",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        lines = []
        try:
            async with asyncio.timeout(20):
                while line := await helper.stdout.readline():
                    lines.append(line.decode(errors="replace").strip())
                    if "listening" in lines[-1]:
                        break
        except TimeoutError:
            pass
        if not lines or "listening" not in lines[-1]:
            return await fail("the call audio helper did not start (is relay-bridge 0.5.17 installed?): "
                              + " | ".join(lines[-3:])[:300])
        log.info("call audio on %s: %s", serial, " | ".join(lines))
        port = (await core.command("-s", serial, "forward", "tcp:0", f"localabstract:{SOCKET}")).decode().strip()
        if not port.isdigit():
            return await fail(f"adb forward returned {port!r}")
        _, writer = await asyncio.open_connection("127.0.0.1", int(port))
        grpc_port, token = found
        channel, speaker = emulator_frames.open_audio(grpc_port, token, RATE)
        upsample = Upsampler()
        await ws.send_text("ready")

        async def hear():
            async for pcm in speaker:
                if pcm:
                    await ws.send_bytes(pcm)

        async def speak():
            nonlocal hang
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes"):
                    writer.write(frame(b"A", upsample(message["bytes"])))
                    await writer.drain()
                elif message.get("text") == "flush":
                    writer.write(frame(b"F"))
                    await writer.drain()
                elif message.get("text") == "hangup":
                    return
                elif message.get("text") == "release":
                    hang = False
                    return

        async def connected():
            with contextlib.suppress(Exception):  # the line may close first
                await picked_up(serial)
                await ws.send_text("connected")

        pickup = asyncio.create_task(connected())
        tasks = [asyncio.create_task(hear()), asyncio.create_task(speak())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            if (exc := task.exception()) and not isinstance(exc, WebSocketDisconnect):
                log.warning("call audio on %s ended: %r", serial, exc)
    except WebSocketDisconnect:
        pass
    finally:
        if pickup:
            pickup.cancel()
        if hang:
            with contextlib.suppress(Exception):
                await hang_up(slot)
        if writer:
            with contextlib.suppress(Exception):
                writer.write(frame(b"Q"))
                await writer.drain()
                writer.close()
        if channel:
            with contextlib.suppress(Exception):
                await channel.close()
        if port and port.isdigit():
            await core.command_result("-s", serial, "forward", "--remove", f"tcp:{port}")
        if helper and helper.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                helper.kill()
        # The helper ends when its client goes; this makes sure no injector outlives the call.
        await core.command_result("-s", serial, "shell", "su -c 'pkill -f relay-callaudio'", timeout=10)
        with contextlib.suppress(Exception):
            await ws.close()
