"""Voice mode: the user talks to Relay and hears it (frontend/src/voice), from the phone app or the window.

Three calls, in the order a turn uses them:

- `POST /api/voice/listen`: a short-lived OpenAI client secret for a transcription session. The browser streams its
  microphone straight to OpenAI with it (nothing but the secret crosses the companion tunnel) and gets the words
  back; OpenAI's voice detection also tells it when the user starts speaking, which is how a reply is interrupted.
- `POST /api/voice/utterance`: what the user said. The front desk (runtime/frontdesk.py) places it: the
  conversation it continues (or a new one), or the code a waiting step asked for. The task then runs as any
  other, with `voice` set so the agents also write what to say aloud. Returns at once with the acknowledgment.
- `POST /api/voice/speak`: a reply as speech (OpenAI text-to-speech, MP3), streamed. The agents' `spoken` text
  when there is one; otherwise the written reply, with its markdown taken out.

Transcription and speech are OpenAI's whichever provider the tasks run on, so voice needs the OpenAI key."""

import logging
import re
import time
import typing
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import delivery
from ..agents.catalog import Models
from ..agents.llm import LLM, provider_error
from ..config import settings
from ..runtime import frontdesk
from ..store import uid
from . import chats, common, runs

router = APIRouter(prefix="/api/voice")
log = logging.getLogger(__name__)

SECRET_SECONDS = 3600  # a client secret opens sessions for this long; the browser asks for another when it lapses
SILENCE_MS = 700  # the pause that ends what the user is saying
# The most that is read aloud at once: about two minutes of speech, which also keeps the MP3 within one companion
# tunnel frame (4 MB, app/companion/tunnel.py) when it crosses to the phone. The agents' spoken forms are far shorter;
# this bounds a written reply read aloud in their place.
SPEAK_CHARS = 1500

# The front desk's model, opened on the first utterance.
model: LLM | None = None


def the_model():
    global model
    if model is None:
        model = LLM()
    return model


def require_key():
    if not settings.openai_api_key:
        raise HTTPException(503, "Voice needs an OpenAI API key. Add one in Settings.")
    return {"Authorization": f"Bearer {settings.openai_api_key}"}


def realtime_url():
    """The transcription session's WebSocket address, beside the REST base (https://api.openai.com/v1 ->
    wss://api.openai.com/v1/realtime?intent=transcription)."""
    parts = urlsplit(settings.openai_base_url.rstrip("/"))
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}{parts.path}/realtime?intent=transcription"


@router.post("/listen")
async def listen():
    """A client secret for a transcription session: the browser connects to `url` with it as the
    `openai-insecure-api-key.<secret>` WebSocket subprotocol and streams 24 kHz PCM."""
    headers = require_key()
    language = (common.store.user_locale().get("language") or "")[:2].lower()
    session = {
        "type": "transcription",
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "noise_reduction": {"type": "near_field"},
                "transcription": {
                    "model": settings.voice_transcribe_model,
                    **({"language": language} if language else {}),
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": SILENCE_MS,
                },
            }
        },
    }
    body = {
        "expires_after": {"anchor": "created_at", "seconds": SECRET_SECONDS},
        "session": session,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{settings.openai_base_url.rstrip('/')}/realtime/client_secrets",
                headers=headers,
                json=body,
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
        "model": settings.voice_transcribe_model,
        "rate": 24000,
    }


class Utterance(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    # The conversation the user last spoke with in this sitting (frontend/src/voice), if any.
    focus_chat_id: str | None = None
    provider: typing.Literal["deepseek", "openai", "anthropic"] = "deepseek"
    models: Models | None = None


@router.post("/utterance", status_code=202)
async def utterance(body: Utterance):
    """Place what the user said and start or continue the work. Returns the conversation it went to, its run and
    the acknowledgment to say now."""
    text = " ".join(body.text.split())
    if not text:
        raise HTTPException(422, "Nothing was said")
    focus = (
        body.focus_chat_id
        if body.focus_chat_id
        and common.store.rows("SELECT id FROM chats WHERE id=?", (body.focus_chat_id,))
        else None
    )
    picks = body.models.model_dump() if body.models else None
    decision = await frontdesk.decide(the_model(), common.store, text, focus, body.provider, picks)
    log.info("front desk: %s", frontdesk.trace(decision, text))
    if decision.code:
        waiting = frontdesk.waiting_code(common.store, decision.chat_id)
        if waiting:
            run_id, node_id = waiting
            state = await runs.reply(
                run_id,
                runs.ReplyInput(content=frontdesk.digits(text), node_id=node_id, sensitive=True),
            )
            return {**placed(decision, state), "kind": "code"}
        # The step stopped waiting meanwhile: the words go to the conversation as they are.
    chat_id = decision.chat_id
    started = not chat_id
    if started:
        chat_id = uid()
        common.store.execute(
            "INSERT INTO chats VALUES (?,?,?)", (chat_id, "New conversation", time.time())
        )
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        working = (delivery.latest_run(db, chat_id) or {"status": None})[
            "status"
        ] in delivery.WORKING
        state = chats.deliver(
            db, chat_id, text, body.provider, "steer", body.models, channel="voice"
        )
    common.store.event(state["id"], "message_steer" if working else "queued", {})
    return {
        **placed(decision, state),
        "kind": "started" if started else "steered" if working else "continued",
    }


def placed(decision, state):
    title = common.store.rows("SELECT title FROM chats WHERE id=?", (state["chat_id"],))
    return {
        "chat_id": state["chat_id"],
        "run_id": state["id"],
        "status": state["status"],
        "title": title[0]["title"] if title else "",
        "acknowledgment": decision.acknowledgment,
        "decided_by": decision.decided_by,
    }


class Speech(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    voice: str | None = Field(default=None, max_length=40)


# Markdown as speech: what the agents wrote for the eye, read aloud when they wrote nothing for the ear.
FENCE = re.compile(r"```.*?```", re.DOTALL)
IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.MULTILINE)
HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s*)?", re.MULTILINE)
QUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)(?=\S)(.+?)(?<=\S)\1")
MATH = re.compile(r"\$\$.*?\$\$", re.DOTALL)
IMAGE_REF = re.compile(r"\bimage:\d+\b")


def plain(text):
    """`text` without its markdown, as sentences to be read aloud."""
    text = FENCE.sub(" ", text)
    text = MATH.sub(" ", text)
    text = IMAGE.sub(r"\1", text)
    text = LINK.sub(r"\1", text)
    text = TABLE_RULE.sub("", text)
    lines = []
    for line in text.splitlines():
        if "|" in line and line.strip().startswith("|") or line.count("|") >= 2:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            line = ", ".join(c for c in cells if c) + "."
        lines.append(line)
    text = "\n".join(lines)
    text = HEADING.sub("", text)
    text = BULLET.sub("", text)
    text = QUOTE.sub("", text)
    for _ in range(3):
        text = EMPHASIS.sub(r"\2", text)
    text = IMAGE_REF.sub("", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def shorten(text, limit=SPEAK_CHARS):
    """`text` cut to `limit` characters at the last sentence end before it (a whole sentence is easier on the ear)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind(".\n"), cut.rfind("\n"))
    return (cut[: end + 1] if end > limit // 3 else cut).rstrip()


async def synthesize(text, instructions, response_format="mp3"):
    """`text` as speech, whole: the bytes, or b"" when OpenAI could not be reached or refused (logged)."""
    headers = require_key()
    payload = {
        "model": settings.voice_tts_model,
        "voice": settings.voice_tts_voice,
        "input": text,
        "instructions": instructions,
        "response_format": response_format,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
            response = await client.post(f"{settings.openai_base_url.rstrip('/')}/audio/speech", headers=headers, json=payload)
    except httpx.TransportError as exc:
        log.warning("voice: speech could not be made (%s)", type(exc).__name__)
        return b""
    if response.is_error:
        log.warning("voice: speech refused: %s", provider_error(response, response.content))
        return b""
    return response.content


@router.post("/speak")
async def speak(body: Speech):
    """`text` as speech, streamed as MP3 while it is made."""
    headers = require_key()
    text = shorten(plain(body.text))
    if not text:
        raise HTTPException(422, "Nothing to say")
    payload = {
        "model": settings.voice_tts_model,
        "voice": body.voice or settings.voice_tts_voice,
        "input": text,
        "instructions": settings.voice_tts_instructions,
        "response_format": "mp3",
    }
    client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
    try:
        response = await client.send(
            client.build_request(
                "POST",
                f"{settings.openai_base_url.rstrip('/')}/audio/speech",
                headers=headers,
                json=payload,
            ),
            stream=True,
        )
    except httpx.TransportError as exc:
        await client.aclose()
        raise HTTPException(502, f"Could not reach OpenAI ({type(exc).__name__}).") from None
    if response.is_error:
        detail = await response.aread()
        await response.aclose()
        await client.aclose()
        raise HTTPException(502, str(provider_error(response, detail)))

    async def audio():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        audio(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"}
    )
