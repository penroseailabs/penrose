"""The emulator's own screen stream: the gRPC service Android Studio's phone view uses.

Frames come straight from the emulator's display on this computer, already scaled to the size asked for, and
only when the screen changes. The phone does no video encoding, so the live view costs it almost nothing.
Started with `-grpc <port> -grpc-use-token`, the emulator serves it on 127.0.0.1 only, behind a token it
makes for each launch and advertises, with the port, in its discovery file.

Two messages of emulator_controller.proto (Android SDK, emulator/lib) are all the live view needs, so they
are encoded here by hand rather than generating the whole API:

    message ImageFormat { ImgFormat format = 1; uint32 width = 3; uint32 height = 4; }   // RGB888 = 2
    message Image { ImageFormat format = 1; bytes image = 4; uint32 seq = 5; }
"""

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import grpc

METHOD = "/android.emulation.control.EmulatorController/streamScreenshot"
RGB888 = 2


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, i
        shift += 7


def _fields(data: bytes) -> Iterator[tuple[int, int | bytes]]:
    """(field number, value) for each field: ints for varints, bytes for length-delimited; others skipped."""
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(data, i)
            yield field, value
        elif wire == 2:
            length, i = _read_varint(data, i)
            yield field, data[i : i + length]
            i += length
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError(f"Unexpected protobuf wire type {wire}")


def image_format(width: int, height: int) -> bytes:
    """An ImageFormat asking for RGB pixels at this size."""
    return b"".join(
        (
            _varint(1 << 3),
            _varint(RGB888),
            _varint(3 << 3),
            _varint(width),
            _varint(4 << 3),
            _varint(height),
        )
    )


def parse_image(data: bytes) -> tuple[int, int, bytes]:
    """An Image as (width, height, RGB pixels). An inactive display sends 0 x 0."""
    width = height = 0
    pixels = b""
    for field, value in _fields(data):
        if field == 1 and isinstance(value, bytes):
            for inner, number in _fields(value):
                if inner == 3:
                    width = number
                elif inner == 4:
                    height = number
        elif field == 4 and isinstance(value, bytes):
            pixels = value
    return width, height, pixels


def _discovery_dirs() -> list[Path]:
    """Where a running emulator advertises itself (one pid_<n>.ini per emulator)."""
    if sys.platform == "win32":
        return [
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Temp/avd/running"
        ]
    if sys.platform == "darwin":
        return [Path.home() / "Library/Caches/TemporaryItems/avd/running"]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    user = os.environ.get("USER", "")
    return [
        *([Path(runtime) / "avd/running"] if runtime else []),
        Path(f"/tmp/android-{user}/avd/running"),
    ]


def _advertised(serial: str) -> Iterator[tuple[str, dict[str, str]]]:
    """(the file's pid_<n> number, its entries) for each discovery file naming this adb serial (emulator-5570).
    One left by an emulator that crashed stays behind, so there can be more than one."""
    console = serial.removeprefix("emulator-")
    for folder in _discovery_dirs():
        for ini in folder.glob("pid_*.ini"):
            entries = {}
            with contextlib.suppress(OSError):
                entries = dict(
                    line.split("=", 1)
                    for line in ini.read_text(errors="ignore").splitlines()
                    if "=" in line
                )
            if entries.get("port.serial") == console:
                yield ini.stem.removeprefix("pid_"), entries


def discover(serial: str) -> tuple[int, str] | None:
    """The gRPC port and per-launch token of the emulator with this adb serial (emulator-5570), if it was
    started with `-grpc <port> -grpc-use-token`."""
    for _, entries in _advertised(serial):
        if entries.get("grpc.port") and entries.get("grpc.token"):
            return int(entries["grpc.port"]), entries["grpc.token"]
    return None


def emulator_pids(serial: str) -> list[int]:
    """The process ids of the emulators advertising this adb serial (the qemu process: it names the file)."""
    return [int(number) for number, _ in _advertised(serial) if number.isdigit()]


AUDIO_METHOD = "/android.emulation.control.EmulatorController/streamAudio"
AUD_FMT_S16 = 1  # mono is channels = 0, the default, so it is not written


def audio_format(rate: int) -> bytes:
    """An AudioFormat asking for mono 16-bit little-endian PCM at this rate:
    message AudioFormat { uint64 samplingRate = 1; Channels channels = 2; SampleFormat format = 3; }"""
    return _varint(1 << 3) + _varint(rate) + _varint(3 << 3) + _varint(AUD_FMT_S16)


def parse_audio(data: bytes) -> bytes:
    """An AudioPacket's samples: message AudioPacket { AudioFormat format = 1; uint64 timestamp = 2; bytes audio = 3; }"""
    for field, value in _fields(data):
        if field == 3 and isinstance(value, bytes):
            return value
    return b""


def open_audio(port: int, token: str, rate: int):
    """The channel (close it when done) and an async iterator of what the phone plays, as PCM chunks of about
    20-30 ms. The emulator sends nothing while the phone is silent."""
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    call = channel.unary_stream(AUDIO_METHOD, response_deserializer=parse_audio)
    return channel, call(audio_format(rate), metadata=[("authorization", f"Bearer {token}")])


def open_stream(port: int, token: str, width: int, height: int):
    """The channel (close it when done) and an async iterator of (width, height, rgb) frames. The asyncio
    client, not the blocking one in a thread: that one polls for the GIL and starves the event loop."""
    channel = grpc.aio.insecure_channel(
        f"127.0.0.1:{port}", options=[("grpc.max_receive_message_length", 64 << 20)]
    )
    call = channel.unary_stream(METHOD, response_deserializer=parse_image)
    return channel, call(
        image_format(width, height), metadata=[("authorization", f"Bearer {token}")]
    )
