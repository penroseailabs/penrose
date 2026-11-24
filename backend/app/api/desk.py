"""The reception desk's API (runtime/desk.py): the phone opens a Realtime session with the desk and keeps Relay's
log of it current.

- `POST /api/voice/desk/open`: a client secret for the desk's session, and what to put in it first: the last
  lines said (`items`), and `unheard`, the events the user has not heard: the phone shows them as cards
  (opening is silent: the desk tells one only when the user opens its card or asks).
- `POST /api/voice/desk/state`: penrose as it is now, as the desk reads it (runtime/deskstate.py); the phone asks
  before every reply and puts it last in the conversation, in place of the one before.
- `POST /api/voice/desk/tool`: a tool the desk called; runs it and returns what to tell the desk. Logged.
- `POST /api/voice/desk/log`: what the user said and what the desk said, as the session transcribes them.
- `GET /api/voice/desk/events?after=`: the events since `after` (the streams' `desk` value is the latest id),
  as items to add to the open session; `POST .../announced` once the desk has told them.
"""

import base64
import json
import typing

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..agents.catalog import Models
from ..agents.llm import provider_error
from ..config import settings
from ..runtime import desk, deskstate
from . import common, voice

router = APIRouter(prefix="/api/voice/desk")


def require_desk():
    if not settings.voice_desk:
        raise HTTPException(404, "The reception desk is off.")
    return voice.require_key()


def realtime_url():
    return voice.realtime_url().replace("?intent=transcription", f"?model={settings.voice_desk_model}")


@router.post("/open")
async def open_desk():
    headers = require_desk()
    language = (common.store.user_locale().get("language") or "")[:2].lower()
    body = {
        "expires_after": {"anchor": "created_at", "seconds": voice.SECRET_SECONDS},
        "session": desk.session_config(language),
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/realtime/client_secrets", headers=headers, json=body
            )
    except httpx.TransportError as exc:
        raise HTTPException(502, f"Could not reach OpenAI ({type(exc).__name__}).") from None
    if response.is_error:
        raise HTTPException(502, str(provider_error(response)))
    data = response.json()
    return {
        "secret": data.get("value", ""),
        "expires_at": data.get("expires_at"),
        "url": realtime_url(),
        "model": settings.voice_desk_model,
        "rate": 24000,
        "items": desk.items(common.store),
        "unheard": [desk.event_json(e) for e in desk.unannounced(common.store)],
        "latest_event": desk.latest_event_id(common.store),
    }


class Now(BaseModel):
    # The errand the phone holds as the one being talked about (a chat id), when it has one.
    about: str = Field(default="", max_length=64)
    # The user opened that errand's card on their screen, `at` (seconds since the epoch).
    opened: bool = False
    at: float = 0.0


@router.post("/state")
def state(body: Now):
    require_desk()
    return deskstate.state(common.store, body.about, body.opened, body.at)


class Call(BaseModel):
    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=40)
    arguments: str = Field(default="{}", max_length=16000)
    provider: typing.Literal["deepseek", "openai", "anthropic"] = "deepseek"
    models: Models | None = None
    # Whether the reply that made this call answers the user's words (False: it only tells news). None from a page
    # that predates it.
    for_user: bool | None = None


@router.post("/tool")
async def tool(body: Call):
    require_desk()
    desk.write(common.store, "call", {"call_id": body.call_id, "name": body.name, "arguments": body.arguments})
    provider = body.models.agent.provider if body.models else body.provider
    result = await desk.perform(
        common.store, body.name, body.arguments, provider, body.models.model_dump() if body.models else None,
        for_user=body.for_user,
    )
    # The desk reads the captions (`on_screen`); the pictures themselves are for the phone's screen.
    output = json.dumps({k: v for k, v in result.items() if k != "pictures"} if isinstance(result, dict) else result, ensure_ascii=False)
    desk.write(common.store, "output", {"call_id": body.call_id, "output": output})
    # The conversation the desk is now talking about, for the screen (the desk itself sees short ids only).
    chat_id = desk.resolve(common.store, result.get("session")) if isinstance(result, dict) else None
    focus = {"chat_id": chat_id, "title": result.get("title", "")} if chat_id else None
    return {"output": output, "result": result, "focus": focus}


class Said(BaseModel):
    role: typing.Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=8000)
    # A reply that was already cut off when its words came: what the user heard of it, and who cut it.
    heard: str | None = Field(default=None, max_length=8000)
    by: typing.Literal["user", "news"] | None = None
    how: typing.Literal["tap", "voice"] | None = None
    # Where a reply to the user spent its time, in ms (desk.ts `timing`): kept to find what makes one slow.
    timing: dict[str, float | bool] | None = None


@router.post("/log", status_code=204)
def log_said(body: Said):
    require_desk()
    text = " ".join(body.text.split())
    if text:
        payload = {"text": text}
        if body.role == "assistant" and body.heard is not None:
            payload.update(heard=" ".join(body.heard.split()), cut=body.by or "user", **({"how": body.how} if body.how else {}))
        if body.role == "assistant" and body.timing:
            payload["timing"] = body.timing
        desk.write(common.store, body.role, payload)


# --- the nods ---------------------------------------------------------------------------------------------------
# The page plays a short "mm-hm" when the user pauses in the middle of a long turn (desk.ts `listen`): a clip, never
# a reply, so it costs no round trip and is not a turn. The clips are the desk's voice saying these, made once with
# the speech model and kept on disk, as raw PCM at 24 kHz like the desk's own audio.
NODS = ("Mm-hm.", "Uh-huh.", "Mm.")
NOD_INSTRUCTIONS = (
    "A brief listening sound, as someone nodding along while a friend talks: soft, low, warm and unhurried. "
    "Not a word, not a question, no breath before or after."
)
_nods: dict[str, list[bytes]] = {}


async def nod_clips():
    """The clips for the desk's voice, from memory, disk, or the speech model (then kept). Fewer when some could
    not be made; none when the speech model is unreachable, and the page nods silently (not at all)."""
    key = settings.voice_tts_voice
    if key in _nods:
        return _nods[key]
    folder = settings.data_dir / "voice"
    clips = []
    for i, text in enumerate(NODS):
        path = folder / f"nod-{key}-{i}.pcm"
        try:
            clips.append(path.read_bytes())
            continue
        except OSError:
            pass
        pcm = await voice.synthesize(text, NOD_INSTRUCTIONS, "pcm")
        if not pcm:
            continue
        clips.append(pcm)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pcm)
        except OSError:
            pass
    if len(clips) == len(NODS):
        _nods[key] = clips  # all made: nothing left to try again next time
    return clips


@router.get("/nods")
async def nods():
    require_desk()
    return {"rate": 24000, "clips": [base64.b64encode(c).decode() for c in await nod_clips()]}


class Cut(BaseModel):
    heard: str = Field(default="", max_length=8000)
    by: typing.Literal["user", "news"] = "user"
    how: typing.Literal["tap", "voice"] | None = None  # the user's cut: a tap on the screen, or their voice over it


@router.post("/cut", status_code=204)
def log_cut(body: Cut):
    """The desk's latest reply, already logged, was stopped before its end: it is remembered as heard."""
    require_desk()
    desk.cut(common.store, " ".join(body.heard.split()), body.by, body.how)


@router.get("/events")
def events(after: int = 0, unannounced: bool = False):
    """The events since `after`; or, `unannounced`, the ones the user has not heard yet (the phone's cards)."""
    require_desk()
    found = desk.unannounced(common.store) if unannounced else desk.events_after(common.store, after)
    return {
        "events": [desk.event_json(e) for e in found],
        "latest": desk.latest_event_id(common.store),
    }


class Announced(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=500)
    chat_id: str | None = Field(default=None, max_length=64)


@router.post("/announced", status_code=204)
def announced(body: Announced):
    """The user has heard these events (the desk told them), or opened or dismissed that chat's card."""
    require_desk()
    desk.announced(common.store, ids=body.ids, chat_id=body.chat_id)
