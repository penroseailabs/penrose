"""Files for a message: a chunked upload that works the same from the desktop window and the phone app (whose tunnel
carries one small JSON frame at a time), then the file's pictures. See app/attachments.py.

begin -> chunks (each at its offset; sending one again is harmless) -> complete. A file for the phone is pushed to the
phone at `complete`, with no agent step. `GET` says how much has arrived, so an upload cut short carries on.

Also what penrose keeps for a chat: a call's recording, read back a part at a time (`audio`)."""

import asyncio
import base64
import binascii
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import attachments
from ..errors import DeviceError
from . import common

router = APIRouter(prefix="/api/uploads")


class Begin(BaseModel):
    chat_id: str
    name: str = Field(min_length=1, max_length=300)
    size: int = Field(ge=1)
    mime: str = Field(default="", max_length=100)
    purpose: str


class Chunk(BaseModel):
    offset: int = Field(ge=0)
    data: str = Field(max_length=attachments.CHUNK * 2)  # base64


AUDIO_PART = 1024 * 1024  # of a recording at a time: about 1.4 MB as base64, well under a tunnel frame


def require(attachment_id):
    row = attachments.get(common.store, attachment_id)
    if not row:
        raise HTTPException(404, "File not found")
    return row


@router.post("", status_code=201)
def begin(body: Begin):
    common.require_chat(body.chat_id)
    try:
        row = attachments.begin(common.store, body.chat_id, body.name, body.size, body.mime, body.purpose)
    except attachments.Rejected as exc:
        raise HTTPException(422, str(exc)) from None
    return {"id": row["id"], "chunk": attachments.CHUNK, "received": 0, "attachment": attachments.view(row)}


@router.post("/{attachment_id}/chunks")
def chunk(attachment_id: int, body: Chunk):
    row = require(attachment_id)
    if row["done"]:
        raise HTTPException(409, "That file is already complete")
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "That chunk is not valid") from None
    try:
        return {"received": attachments.write_chunk(row, body.offset, data)}
    except attachments.Rejected as exc:
        have = str(exc).removeprefix("received:")
        if have != str(exc):
            raise HTTPException(409, {"received": int(have)}) from None
        raise HTTPException(422, str(exc)) from None


@router.get("/{attachment_id}")
def status(attachment_id: int):
    row = require(attachment_id)
    return {"received": row["size"] if row["done"] else attachments.received(row), "done": bool(row["done"]),
            "attachment": attachments.view(row)}


@router.post("/{attachment_id}/complete")
async def complete(attachment_id: int):
    """The file is all here. Looks at it (a photo, a document's text), and for the phone puts it there. Calling it
    again for a file that did not reach the phone tries the phone again."""
    row = require(attachment_id)
    if not row["done"]:
        try:
            changes = await asyncio.to_thread(attachments.finish, common.store, row)
        except attachments.Rejected as exc:
            raise HTTPException(422, str(exc)) from None
        attachments.update(common.store, attachment_id, changes)
        row = require(attachment_id)
    if row["purpose"] == "phone" and not row["phone_path"]:
        try:
            await attachments.put_on_phone(common.store, common.gateway, row)
        except DeviceError as exc:
            # The file is kept on the computer: the window can try again without sending it again.
            raise HTTPException(502, str(exc)) from None
    return attachments.view(require(attachment_id))


@router.delete("/{attachment_id}")
async def discard(attachment_id: int):
    """Take back a file that no message carries: gone from the computer, and from the phone when it was put there."""
    row = require(attachment_id)
    if row["sent"]:
        raise HTTPException(409, "That file is already on a message")
    Path(row["path"] or "").unlink(missing_ok=True)
    common.store.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    if row["phone_path"]:
        slot = await attachments.pick_slot(common.gateway)
        if slot:
            try:
                await common.gateway.delete(f"/devices/{slot}/files/{Path(row['phone_path']).name}")
            except Exception:
                pass  # the phone's copy is the user's to remove then
    return {"deleted": True}


@router.get("/{attachment_id}/audio")
def audio(attachment_id: int, part: int = 0):
    """A call's recording (runtime/call.py), to play: its `part`th megabyte. The window joins the parts (the file's
    size says how many), so it plays the same on the computer and in the phone app, whose tunnel carries one small
    frame at a time."""
    row = require(attachment_id)
    if row["kind"] != "audio" or not row["done"] or part < 0:
        raise HTTPException(404, "That file is not a recording")
    try:
        with open(row["path"], "rb") as f:
            f.seek(part * AUDIO_PART)
            data = f.read(AUDIO_PART)
    except OSError:
        raise HTTPException(404, "That recording is no longer kept") from None
    if not data:
        raise HTTPException(404, "The recording ends before that part")
    return Response(data, media_type=row["mime"] or "audio/wav", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/{attachment_id}/{size}")
def picture(attachment_id: int, size: str):
    """A photo, as a JPEG: `thumb` (640 px) for the chat and the pill, `view` (1600 px) to look at."""
    if size not in ("thumb", "view"):
        raise HTTPException(404, "Not found")
    row = require(attachment_id)
    if row["kind"] != "image" or not row["done"]:
        raise HTTPException(404, "That file is not a picture")
    try:
        jpeg = attachments.thumb(row["path"], 640 if size == "thumb" else 1600)
    except Exception:
        raise HTTPException(404, "That picture cannot be shown") from None
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
