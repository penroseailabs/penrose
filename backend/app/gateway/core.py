"""The gateway's devices and the one way it reaches them: adb.

Routers reach this state and these helpers as `core.<name>`, so tests can swap any of them in one place."""

import asyncio
import re
import shlex
import time

from fastapi import HTTPException

from .. import processes
from ..bridge import adbwire
from ..bridge.devices import XposedDevice
from ..bridge.ops import Ops
from ..config import settings
from ..models import PRIVATE_MARK
from ..store import Store

# Only the gateway opens this database (its leases); the API and worker coordinate over HTTP.
store = Store(settings.user_dir / "gateway")
targets = [s.strip() for s in settings.adb_targets.split(",") if s.strip()]
slots = {f"device-{i + 1}": target for i, target in enumerate(targets)}
locks = {slot: asyncio.Lock() for slot in slots}
dimensions = {}  # slot -> {"width", "height"} of its screen, read when it connects
# What an app may put between the characters of a value it shows: a card number in groups, an expiry with a slash.
SEPARATORS = r"[\s\-/.]{0,3}"
SHORT = 8  # characters (separators aside) below which a value is hidden only as a whole word, and only for a while
SHORT_SECONDS = 30 * 60


class Redactions:
    """The values typed privately on one phone (codes, passwords, the user's private entries): every screen read
    is scrubbed of them, also when the app shows them reformatted. A short one (a CVV, a 4-digit code) is hidden
    only as a whole word and only for SHORT_SECONDS, so it does not blank every "123" on the phone for good."""

    def __init__(self):
        self.values = {}  # value -> (pattern, typed at)

    def add(self, value):
        if not value:
            return
        bare = re.sub(SEPARATORS[:-5], "", value)  # without separators: what the app may regroup
        pattern = SEPARATORS.join(map(re.escape, bare)) if bare else re.escape(value)
        if len(bare) < SHORT:
            pattern = rf"(?<![0-9A-Za-z]){pattern}(?![0-9A-Za-z])"
        self.values[value] = (re.compile(pattern), time.monotonic())

    def patterns(self):
        now = time.monotonic()
        for value, (pattern, at) in list(self.values.items()):
            short = len(re.sub(SEPARATORS[:-5], "", value)) < SHORT
            if short and now - at > SHORT_SECONDS:
                del self.values[value]
                continue
            yield value, pattern

    def __bool__(self):
        return bool(self.values)

    def __contains__(self, value):
        return value in self.values

    def __iter__(self):
        return (value for value, _ in self.patterns())


# Values typed privately: every screen read is scrubbed of them.
redactions = {slot: Redactions() for slot in slots}
# The bridge driver per device; it keeps the forwarded sockets to each app's bridge.
phones = {slot: XposedDevice(serial=target) for slot, target in slots.items()}
# The ops lane (bridge/ops.py): deterministic device commands the subagent runs beside the UI agent.
ops_lane = {slot: Ops(phone) for slot, phone in phones.items()}


async def process_result(executable, *args, timeout=25, outlive=False):
    """`outlive`: what it starts stays running after the gateway ends (adb's server, which others share). Anything
    else the gateway starts ends with it (app/processes.py)."""
    proc = await asyncio.create_subprocess_exec(
        executable, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({"creationflags": processes.outlive_flags()} if outlive and processes.outlive_flags() else {}),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    return stdout, stderr, proc.returncode


async def command_result(*args, timeout=25):
    try:
        # Without starting the adb program, when it is a request adb's server takes directly (bridge/adbwire.py).
        return await adbwire.run(args, timeout=timeout)
    except adbwire.Unavailable:
        return await process_result(settings.adb_path, *args, timeout=timeout)


async def command(*args, timeout=25, include_stderr=False):
    stdout, stderr, returncode = await command_result(*args, timeout=timeout)
    if returncode:
        raise HTTPException(503, "ADB command failed. Check that the emulator is running and authorized.")
    return stdout + stderr if include_stderr else stdout


unanswered = []  # callbacks(slot): an adb call to the phone ran out of time (watch.py asks whether it still answers)


async def adb(slot, *args, timeout=25, include_stderr=False):
    if slot not in slots:
        raise HTTPException(404, "Unknown emulator slot")
    try:
        return await command("-s", slots[slot], *args, timeout=timeout, include_stderr=include_stderr)
    except TimeoutError:
        for callback in unanswered:
            callback(slot)
        raise


async def shell(slot, *args):
    # ADB invokes an Android shell: quote each argument for that remote shell too.
    return await adb(slot, "shell", shlex.join([str(a) for a in args]), include_stderr=True)


def require_slot(slot):
    if slot not in slots:
        raise HTTPException(404, "Unknown device")


def check_owner(slot, owner):
    rows = store.rows("SELECT owner FROM leases WHERE slot=?", (slot,))
    if not rows or rows[0]["owner"] != owner:
        raise HTTPException(409, "Device is not leased to this caller")


def scrub(value, secrets):
    """A bridge answer with every privately typed value replaced, at any depth: `secrets` is a Redactions (each value
    as typed and as the app may have regrouped it) or plain values."""
    if isinstance(value, str):
        found = secrets.patterns() if isinstance(secrets, Redactions) else ((s, None) for s in secrets)
        for secret, pattern in list(found):
            value = pattern.sub(PRIVATE_MARK, value) if pattern is not None else value.replace(secret, PRIVATE_MARK)
        return value
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    return value
