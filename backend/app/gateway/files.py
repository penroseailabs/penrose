"""Files put on the phone: one folder, /sdcard/Download/penrose, where Files, Photos and every app's picker find them.

Used for what the user uploads to the phone and for what an agent puts there for them (app/attachments.py
put_on_phone): the same route. No lease: nothing on screen changes. No path comes from the caller, only a name.
"""

import itertools
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Request

from ..attachments import PHONE_FOLDER, PHONE_LIMIT
from . import core

router = APIRouter(prefix="/devices/{slot}")
PUSH_SECONDS = 900


def clean(name):
    """A file name as a single plain component, never a path."""
    name = re.sub(r"[\x00-\x1f\x7f]", "", PurePosixPath(str(name).replace("\\", "/")).name).strip(" .")
    if not name:
        raise HTTPException(422, "That file has no usable name")
    return name[:120]


async def exists(serial, path):
    stdout, _, _ = await core.command_result("-s", serial, "shell", f"[ -e {shlex.quote(path)} ] && echo yes")
    return b"yes" in stdout


async def free_name(serial, name):
    """`name`, or `name (2).ext`, `name (3).ext`... when the folder already has one: a file is never overwritten."""
    path = PurePosixPath(name)
    for n in itertools.chain([None], itertools.count(2)):
        candidate = name if n is None else f"{path.stem} ({n}){path.suffix}"
        if not await exists(serial, f"{PHONE_FOLDER}/{candidate}"):
            return candidate


async def scan(serial, path):
    """Tell the phone's media index about the file, so Photos and the pickers list it at once."""
    quoted = shlex.quote(path)
    _, _, failed = await core.command_result(
        "-s", serial, "shell", f"content call --uri content://media --method scan_file --arg {quoted}")
    if failed:
        await core.command_result(
            "-s", serial, "shell",
            f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{quoted} >/dev/null")


@router.put("/files/{name}")
async def put_file(slot: str, name: str, request: Request):
    core.require_slot(slot)
    serial, name = core.slots[slot], clean(name)
    with tempfile.TemporaryDirectory() as directory:
        local = Path(directory) / "upload"  # the phone's name is not the computer's: adb does not take every name
        size = 0
        with open(local, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > PHONE_LIMIT:
                    raise HTTPException(413, "That file is too big for the phone")
                f.write(chunk)
        if not size:
            raise HTTPException(422, "That file is empty")
        await core.command_result("-s", serial, "shell", f"mkdir -p {shlex.quote(PHONE_FOLDER)}")
        target = f"{PHONE_FOLDER}/{await free_name(serial, name)}"
        _, stderr, failed = await core.command_result("-s", serial, "push", str(local), target, timeout=PUSH_SECONDS)
        if failed:
            raise HTTPException(
                502, "The phone did not take the file" + (f": {stderr.decode(errors='replace').strip()[-200:]}" if stderr else "."))
    await scan(serial, target)
    return {"path": target, "size": size}


@router.delete("/files/{name}")
async def delete_file(slot: str, name: str):
    """Take a file out of the folder (a file the user took back before sending its message)."""
    core.require_slot(slot)
    path = f"{PHONE_FOLDER}/{clean(name)}"
    await core.command_result("-s", core.slots[slot], "shell", f"rm -f {shlex.quote(path)}")
    await scan(core.slots[slot], path)
    return {"deleted": True}
