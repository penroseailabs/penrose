"""A voice call penrose holds: after a call step's subagent has dialled, one Realtime model talks with the other
person and reports what they said.

The phone's side is the gateway's call-audio socket (gateway/call_audio.py): what the phone plays comes in, the
model's voice goes out as the phone's mic. The model's instructions (agent_prompts/call.md) come only from the
step's `call` brief: it shares nothing else, takes no action and has one tool, end_call. Its summary and the
transcript go back to the orchestrator as what the other person said: information, never instructions.

Turn-taking: OpenAI's noise filter and voice detector say when they talk; penrose replies when they stop (`reply`,
not the API's automatic reply). Sound over penrose's voice cuts it off only once call_barge_in_ms of it is actually
heard, counted here from the phone's audio (`listen`), not from the detector's events: the detector says they
stopped only after a whole silence_duration_ms of quiet, so a timer that waited for that let nothing go. Anything
shorter (a "yeah", a click, line noise) is let go and gets no reply, so penrose does not start its sentence over.

The line opens while the subagent dials (orchestrator.dial_and_talk), so their first "Hello?" is heard: penrose
answers the moment they speak, not after the subagent has reported.

Code, not the model, holds the limits: no words from them within call_ring_seconds of the dial is no answer; it is told to
wrap up shortly before the step's minutes are up and is cut off at them; call_silence_seconds of nobody talking
ends it; Stop on the run hangs up (the socket closes and the gateway ends the call).

Nobody is left on a silent line: once the phone says they picked up (the gateway's "connected") and no word of
theirs has come for PICKED_UP_SECONDS, penrose greets anyway. If the phone played no sound at all after that, the
report says so instead of "did not answer".

The call is kept for the user: once it is over the chat gets a card (a message from SOURCE) with what was said and
the recording to play, a file of the chat like any other (attachments.keep).
"""

import array
import asyncio
import base64
import contextlib
import json
import logging
import time
import wave

from websockets.asyncio.client import connect

from .. import attachments
from ..agents import prompts
from ..config import settings
from ..errors import safe_error
from ..tools.subagent import Report

log = logging.getLogger("runtime.call")

RATE = 24000
# Talking over penrose (see the module's docstring): a block of the phone's audio this loud (RMS of 16-bit PCM,
# about -38 dBFS) is their voice; a quiet line is near 0, speech in the thousands. Sound from this long before the
# detector's speech_started counts too (its own prefix padding: it fires a little after they began). Should the
# meter never get there while the detector still hears speech (a very soft voice), penrose stops anyway once the
# sound has gone on this much longer than call_barge_in_ms.
LOUD = 400
BARGE_LOOKBACK = 0.3
BARGE_FALLBACK = 1.0
LANGUAGES = {
    "en": "English", "hi": "Hindi", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ar": "Arabic", "nl": "Dutch",
}
WRAP_UP_SECONDS = 30  # before the limit: say goodbye
PICKED_UP_SECONDS = 3  # they picked up and nothing of theirs was heard for this long: penrose greets anyway
SOURCE = "Call"  # the chat message that shows a finished call: its first line names it, the rest is what was said
KEPT_RATE = 16000  # the recording the user plays: speech needs no more, and it travels to the phone app
KEPT_LEAD_SECONDS = 3  # of the ringing kept before they picked up, so their first "Hello?" is on it
HANG_UP_SECONDS = 15  # waited at most for the gateway to end the call in the app
END_CALL = {
    "type": "function",
    "name": "end_call",
    "description": "Hang up, after your goodbye. Its summary is all the user gets from this call.",
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {"type": "string", "enum": ["answered", "voicemail", "declined", "wrong_person", "other"]},
            "summary": {
                "type": "string",
                "description": "What they said that answers why you called (their words where it matters), what "
                "they asked to be passed on, and what is still unknown.",
            },
        },
        "required": ["outcome", "summary"],
    },
}


def instructions(brief, language):
    questions = "\n".join(f"- {q}" for q in brief.get("questions") or [])
    text = prompts.load("call")
    for key, value in {
        "contact": brief["contact"],
        "caller": brief.get("caller") or "the user",
        "purpose": brief["purpose"],
        "questions": f"Ask:\n{questions}" if questions else "",
        "may_share": brief.get("may_share") or "that you are calling for the user, nothing more",
        "language": LANGUAGES.get(language, language or "English"),
        "minutes": str(brief["max_minutes"]),
    }.items():
        text = text.replace(f"[{key}]", value)
    return text


def realtime_url(model):
    base = settings.openai_base_url.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{base}/realtime?model={model}"


def has_words(text):
    return sum(c.isalpha() for c in text or "") >= 2


def audio_input(language):
    """What the phone hears: filtered, transcribed, and turns detected; replies are started by `Call.reply`."""
    return {
        "format": {"type": "audio/pcm", "rate": RATE},
        "noise_reduction": {"type": settings.call_noise_reduction},
        "transcription": {"model": settings.voice_transcribe_model, "language": language},
        "turn_detection": {"type": "server_vad", "threshold": settings.call_vad_threshold, "prefix_padding_ms": 300,
                           "silence_duration_ms": 600, "create_response": False, "interrupt_response": False},
    }


class Recording:
    """A debugging aid, off unless the folder <user data>/call-recordings exists: each call is kept there as a
    stereo WAV (left: what the phone heard, right: penrose's voice as the phone played it) beside a log of the turn
    events, to hear what cut penrose off."""

    def __init__(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        self.wav = wave.open(str(folder / "call.wav"), "wb")
        self.wav.setnchannels(2)
        self.wav.setsampwidth(2)
        self.wav.setframerate(RATE)
        self.events = open(folder / "events.log", "w", encoding="utf-8")
        self.voice = bytearray()  # penrose's voice not yet played, paced by the phone's own stream
        self.started = time.monotonic()

    @classmethod
    def open(cls, run_id, step_id):
        root = settings.user_dir / "call-recordings"
        if not root.is_dir():
            return None
        return cls(root / f"{time.strftime('%Y%m%d-%H%M%S')}-{run_id[:8]}-{step_id}")

    def heard(self, pcm):
        them = array.array("h")
        them.frombytes(pcm[: len(pcm) // 2 * 2])
        size = len(them) * 2
        ours = array.array("h")
        ours.frombytes(bytes(self.voice[:size]).ljust(size, b"\0"))
        del self.voice[:size]
        both = array.array("h", bytes(size * 2))
        both[0::2], both[1::2] = them, ours
        self.wav.writeframes(both.tobytes())

    def played(self, pcm):
        self.voice += pcm

    def flushed(self):
        self.voice.clear()

    def note(self, text):
        self.events.write(f"{time.monotonic() - self.started:8.2f}  {text}\n")
        self.events.flush()

    def close(self):
        with contextlib.suppress(Exception):
            self.wav.close()
        self.events.close()


class Kept:
    """The call as the user plays it back in the chat: both sides as one 16 kHz mono WAV, written as the phone plays
    (penrose's voice is paced by the phone's own stream, as in Recording). Until they pick up (`begin`) only the last
    KEPT_LEAD_SECONDS are held, so the recording does not open with the whole ringing."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(KEPT_RATE)
        self.voice = bytearray()  # penrose's voice not yet played
        self.rest = array.array("h", [0])  # the last sample used, then those waiting for a full three
        self.lead = bytearray()  # before they picked up
        self.live = False
        self.frames = 0

    def heard(self, pcm):
        them = array.array("h")
        them.frombytes(pcm[: len(pcm) // 2 * 2])
        size = len(them) * 2
        ours = array.array("h")
        ours.frombytes(bytes(self.voice[:size]).ljust(size, b"\0"))
        del self.voice[:size]
        data = self.rest + array.array("h", (max(-32768, min(32767, a + b)) for a, b in zip(them, ours)))
        # 24 kHz to 16 kHz: of every three samples two are made, each a mean of its neighbours (which also takes
        # out what 16 kHz cannot hold).
        n = (len(data) - 1) // 3
        if not n:
            self.rest = data
            return
        out = array.array("h", bytes(4 * n))
        out[0::2] = array.array("h", ((p + 2 * a + b) >> 2 for p, a, b in
                                      zip(data[0:3 * n:3], data[1:3 * n:3], data[2:3 * n:3])))
        out[1::2] = array.array("h", ((b + c) >> 1 for b, c in zip(data[2:3 * n:3], data[3:3 * n + 1:3])))
        self.rest = data[3 * n:]
        if self.live:
            self.wav.writeframes(out.tobytes())
            self.frames += len(out)
        else:
            self.lead += out.tobytes()
            del self.lead[: max(0, len(self.lead) - KEPT_LEAD_SECONDS * KEPT_RATE * 2)]

    def played(self, pcm):
        self.voice += pcm

    def flushed(self):
        self.voice.clear()

    def begin(self):
        """They picked up: what is heard from here on is kept, after the last moments before it."""
        if not self.live:
            self.live = True
            self.wav.writeframes(bytes(self.lead))
            self.frames += len(self.lead) // 2
            self.lead.clear()

    def close(self):
        """Returns the seconds kept."""
        with contextlib.suppress(Exception):
            self.wav.close()
        return self.frames / KEPT_RATE


def clock_text(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


class Call:
    """One call: the phone's audio, the model, the clock. `run()` returns the step's Report."""

    def __init__(self, runner, state, step):
        self.runner, self.state, self.step = runner, state, step
        brief = dict(step["call"])
        brief["max_minutes"] = min(int(brief.get("max_minutes") or settings.call_default_minutes), settings.call_max_minutes)
        self.brief = brief
        self.language = (brief.get("language") or runner.store.user_locale().get("language") or "en")[:2].lower()
        self.transcript = []  # [{"who": "them" | "penrose", "text"}]
        self.outcome = None  # end_call's arguments
        self.answered_at = None
        self.answered = asyncio.Event()  # they spoke: the subagent's dialling is done
        self.dialled_at = None  # the subagent reported the call placed: the ring clock runs from here
        self.started = time.monotonic()
        self.last_voice = time.monotonic()
        self.speaking = {"item": None, "start": 0.0, "sent": 0.0}  # the reply being played and how much was sent
        self.cut_items = set()  # replies cut off: their late audio is dropped
        self.responding = False  # a response is in flight (asked for, not done)
        self.cancelling = False
        self.reply_next = False  # reply once the cancelled response is done
        self.barge = None  # cuts penrose off once call_barge_in_ms of them talking over it is heard
        self.blip = False  # their sound stopped before that: not a turn
        self.loud = []  # (monotonic time, seconds) of the phone's loud blocks in the last few seconds
        self.ignored = set()  # those turns' items: no reply, not in the transcript
        self.wrapping = False
        self.ending_at = None  # hang up once the goodbye has played
        self.voiced = set()  # responses that said something
        self.goodbye_for = None  # the response that hung up without a word: the next one says goodbye
        self.asked_at = 0.0  # when the last reply was asked for, to log how long the voice took
        self.recording = None  # the debugging copy (Recording)
        self.kept = None  # the user's copy (Kept)
        self.connected_at = None  # the phone says they picked up
        self.sound_since = False  # the phone has played something since then
        self.unprompted = False  # penrose greeted without hearing a word
        self.file = None  # the kept recording as the agents name it: file:N

    def dialled(self):
        """The subagent placed the call: from now on, no words from them within call_ring_seconds is no answer."""
        self.dialled_at = time.monotonic()

    @property
    def tapes(self):
        return [tape for tape in (self.recording, self.kept) if tape]

    def trace(self, text):
        log.debug("call %s: %s", self.step["id"], text)
        if self.recording:
            self.recording.note(text)

    def note(self, who, text):
        text = (text or "").strip()
        if not text:
            return
        self.trace(f"{who}: {text}")
        self.transcript.append({"who": who, "text": text})
        self.runner.store.event(self.state["id"], "call_transcript", {"node_id": self.step["id"], "who": who, "text": text})

    async def run(self):
        if not settings.openai_api_key:
            return Report(status="failed", summary="Calls need an OpenAI key: add one in penrose's settings.")
        slot, owner = self.state["slot"], self.state["id"]
        try:
            self.recording = Recording.open(owner, self.step["id"])
            if self.state.get("chat_id"):
                self.kept = Kept(attachments.folder(self.state["chat_id"])
                                 / f"call-{owner[:8]}-{self.step['id']}-{int(time.time())}.wav")
            async with self.runner.devices.call_audio(slot, owner) as phone:
                ready = await asyncio.wait_for(phone.recv(), 40)
                if ready != "ready":
                    return Report(status="failed", summary=f"The call's audio could not start: {str(ready)[7:300]}")
                async with connect(realtime_url(settings.call_model), max_size=None, open_timeout=20,
                                   additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"}) as model:
                    await self.converse(phone, model)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the socket, the model, the phone: say what and keep what was heard
            log.warning("call %s failed: %r", self.step["id"], exc)
            if not self.transcript:
                return Report(status="failed", summary=f"The call failed: {safe_error(exc)}")
            self.outcome = self.outcome or {"outcome": "other", "summary": f"The call broke off: {safe_error(exc)}"}
        finally:
            if self.recording:
                self.recording.close()
            try:
                self.show()
            except Exception:  # the call's report matters more than its card
                log.exception("call %s: could not show the call in the chat", self.step["id"])
        return self.report()

    def show(self):
        """The call in the chat, once it is over: a card with what was said and the recording to play. Nothing for
        a call nobody picked up."""
        kept, self.kept = self.kept, None
        if not kept:
            return
        seconds = kept.close()
        said = [line for line in self.transcript if has_words(line["text"])]
        if self.answered_at is None or (seconds < 1 and not said):
            kept.path.unlink(missing_ok=True)
            return
        store, chat_id, contact = self.runner.store, self.state["chat_id"], self.brief["contact"]
        ids = []
        if seconds >= 1:
            row = attachments.keep(store, chat_id, kept.path, f"Call with {contact}.wav", "audio/wav", "audio", "call",
                                   note=clock_text(seconds))
            self.file, ids = attachments.ref(row), [row["id"]]
        else:
            kept.path.unlink(missing_ok=True)
        length = seconds if ids else time.monotonic() - self.answered_at
        lines = [f"Call with {contact} · {clock_text(length)}"]
        lines += [f"{'penrose' if line['who'] == 'penrose' else contact}: {' '.join(line['text'].split())}" for line in said]
        message = store.message(chat_id, "assistant", "\n".join(lines), source=SOURCE, attachments=ids)
        store.event(self.state["id"], "call_recorded", {"node_id": self.step["id"], "message": message})

    async def converse(self, phone, model):
        await model.send(json.dumps({"type": "session.update", "session": {
            "type": "realtime", "model": settings.call_model, "output_modalities": ["audio"],
            "instructions": instructions(self.brief, self.language),
            "tools": [END_CALL], "tool_choice": "auto",
            "audio": {
                "input": audio_input(self.language),
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": settings.call_voice},
            },
        }}))
        tasks = [asyncio.create_task(self.listen(phone, model)), asyncio.create_task(self.hear(phone, model)),
                 asyncio.create_task(self.clock(phone, model))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception():
                    raise task.exception()
        finally:
            for task in tasks:
                task.cancel()
            if self.barge:
                self.barge.cancel()
            with contextlib.suppress(Exception):
                # No call placed or answered (the dial failed): the line closes and the app is left alone.
                await phone.send("hangup" if self.dialled_at or self.answered_at else "release")
            # The gateway closes the line once it has hung up: wait for that, so the step does not report (and
            # the next one start on the phone) while the call is still up.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(HANG_UP_SECONDS):
                    async for _ in phone:
                        pass

    async def listen(self, phone, model):
        """What the phone plays, to the model."""
        async for pcm in phone:
            if pcm == "connected":  # Android counts the call as connected: they picked up (gateway/call_audio.py)
                if self.connected_at is None:
                    self.connected_at = self.last_voice = time.monotonic()
                    self.trace("the phone says they picked up")
            elif isinstance(pcm, bytes):
                if self.connected_at is not None:
                    self.sound_since = True
                for tape in self.tapes:
                    tape.heard(pcm)
                self.meter(pcm)
                await model.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))

    def meter(self, pcm):
        """Note a block of the phone's audio that is loud enough to be someone talking (see LOUD)."""
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if not samples:
            return
        now = time.monotonic()
        if (sum(s * s for s in samples) / len(samples)) ** 0.5 >= LOUD:
            self.loud.append((now, len(samples) / RATE))
        while self.loud and now - self.loud[0][0] > 5:
            self.loud.pop(0)

    def heard_since(self, since):
        """Seconds of loud sound from the phone since `since` (monotonic)."""
        return sum(seconds for at, seconds in self.loud if at >= since)

    def playing(self):
        """Whether penrose's voice is still playing on the phone (audio is sent faster than it plays)."""
        return bool(self.speaking["item"]) and time.monotonic() - self.speaking["start"] < self.speaking["sent"]

    async def reply(self, model, cancel=True):
        """Their turn ended (or the clock has news): the model answers, after any response still in flight (cut
        short unless `cancel` is false)."""
        if self.responding:
            self.reply_next = True
            if cancel:
                await self.cancel(model)
            return
        self.responding = True
        self.asked_at = time.monotonic()
        self.trace("reply")
        await model.send(json.dumps({"type": "response.create"}))

    def end_after_playback(self):
        left = self.speaking["sent"] - (time.monotonic() - self.speaking["start"]) if self.speaking["item"] else 0
        self.ending_at = time.monotonic() + max(0.0, left) + 1.0

    async def cancel(self, model):
        if self.responding and not self.cancelling:
            self.cancelling = True
            await model.send(json.dumps({"type": "response.cancel"}))

    async def cut(self, phone, model):
        """They are talking: stop penrose's reply where they heard it end."""
        await self.cancel(model)
        item = self.speaking["item"]
        if item and self.playing():
            played = time.monotonic() - self.speaking["start"]
            self.trace(f"cut off after {played:.1f} s")
            await phone.send("flush")
            for tape in self.tapes:
                tape.flushed()
            await model.send(json.dumps({"type": "conversation.item.truncate", "item_id": item,
                                         "content_index": 0, "audio_end_ms": int(played * 1000)}))
        if item:
            self.cut_items.add(item)
        self.speaking["item"] = None

    async def cut_in(self, phone, model):
        """They started talking over penrose: cut it off once call_barge_in_ms of their sound is heard. Cancelled
        when the detector says they stopped first (a blip)."""
        started = time.monotonic()
        wanted = settings.call_barge_in_ms / 1000
        while (self.heard_since(started - BARGE_LOOKBACK) < wanted
               and time.monotonic() - started < wanted + BARGE_FALLBACK):
            await asyncio.sleep(0.02)
        self.trace(f"{self.heard_since(started - BARGE_LOOKBACK):.2f} s of them heard over penrose")
        await self.cut(phone, model)

    async def answer(self, model):
        """They spoke (or picked up and said nothing): the call is answered. From now on the model replies when they
        pause; it greets first, once: the greeting is asked for here, not in its instructions, where it was said
        again on every reply."""
        self.answered_at = time.monotonic()
        self.answered.set()
        if self.kept:
            self.kept.begin()
        # Who it calls for is said only when the brief names them: a call to the user themself names nobody.
        caller = (self.brief.get("caller") or "").strip()
        greeting = f"Hi {self.brief['contact']}, this is penrose" + (f", calling for {caller}." if caller else ".")
        await model.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": (
                f'They picked up. Say exactly "{greeting}" and then your first question, once, as one turn.')}]}}))
        await self.reply(model)

    async def hear(self, phone, model):
        """The model's events: its voice to the phone, their turns, the transcript, end_call."""
        async for message in model:
            event = json.loads(message)
            kind = event.get("type", "")
            if kind == "response.output_audio.delta":
                item = event.get("item_id")
                if item in self.cut_items:
                    continue
                pcm = base64.b64decode(event["delta"])
                if self.speaking["item"] != item:
                    self.speaking = {"item": item, "start": time.monotonic(), "sent": 0.0}
                    self.trace(f"voice starts, {time.monotonic() - self.asked_at:.2f} s after the reply was asked for")
                self.voiced.add(event.get("response_id"))
                await phone.send(pcm)
                for tape in self.tapes:
                    tape.played(pcm)
                self.speaking["sent"] += len(pcm) / 2 / RATE
                self.last_voice = time.monotonic()
            elif kind == "response.created":
                self.responding = True
            elif kind == "response.done":
                self.responding = self.cancelling = False
                done_id = (event.get("response") or {}).get("id")
                if self.goodbye_for and done_id != self.goodbye_for and self.ending_at is None:
                    self.end_after_playback()  # the goodbye was said, with or without a second end_call
                if self.reply_next:
                    self.reply_next = False
                    await self.reply(model)
            elif kind == "input_audio_buffer.speech_started":
                self.last_voice = time.monotonic()
                self.blip = False
                if self.playing():  # over penrose's voice: a cut only if it lasts
                    self.trace("they started talking over penrose")
                    self.barge = asyncio.create_task(self.cut_in(phone, model))
                else:
                    self.trace("they started talking")
                    await self.cut(phone, model)  # a reply still being thought of is dropped
            elif kind == "input_audio_buffer.speech_stopped":
                if self.barge and not self.barge.done():
                    self.barge.cancel()
                    self.blip = True
                    self.trace("too short to be them talking: let go")
                self.barge = None
            elif kind == "input_audio_buffer.committed":
                if self.blip:
                    self.ignored.add(event.get("item_id"))
                    self.blip = False
                elif self.answered_at is not None:
                    await self.reply(model)
            elif kind == "conversation.item.input_audio_transcription.completed":
                text = event.get("transcript", "")
                if event.get("item_id") in self.ignored:
                    self.trace(f"(let go) {text}")
                    continue
                self.note("them", text)
                if self.answered_at is None and has_words(text):
                    await self.answer(model)
            elif kind == "response.output_audio_transcript.done":
                self.note("penrose", event.get("transcript", ""))
            elif kind == "response.function_call_arguments.done" and event.get("name") == "end_call":
                with contextlib.suppress(ValueError):
                    self.outcome = json.loads(event.get("arguments") or "{}")
                said = event.get("response_id") in self.voiced
                self.trace(f"end_call ({(self.outcome or {}).get('outcome')}){'' if said else ', nothing said'}")
                if self.answered_at is not None and not said and not self.goodbye_for:
                    # Hanging up without a word leaves them on a dead line: it says goodbye first.
                    self.goodbye_for = event.get("response_id") or "?"
                    self.trace("end_call without a goodbye: asked for one")
                    await model.send(json.dumps({"type": "conversation.item.create", "item": {
                        "type": "function_call_output", "call_id": event.get("call_id"),
                        "output": json.dumps({"hung_up": False, "first": "Thank them and say goodbye in one short "
                                                                         "sentence, then call end_call."})}}))
                    await self.reply(model, cancel=False)
                else:
                    self.end_after_playback()
            elif kind == "error":
                log.warning("call model error: %s", json.dumps(event.get("error", event))[:300])

    async def clock(self, phone, model):
        """The limits, once a second; also how Stop on the run reaches the call."""
        limit = self.brief["max_minutes"] * 60
        while True:
            await asyncio.sleep(1)
            self.runner.cancelled(self.state)  # Stop: raises, the sockets close, the gateway hangs up
            now = time.monotonic()
            if self.ending_at and now >= self.ending_at:
                return
            if self.answered_at is None:
                if self.connected_at is not None and now - max(self.connected_at, self.last_voice) > PICKED_UP_SECONDS:
                    # They picked up and no word of theirs has come: someone waiting in silence, or a phone whose
                    # sound has failed. Either way they hear a greeting, not a dead line.
                    self.unprompted = True
                    self.trace("picked up, nothing heard" + ("" if self.sound_since else " (the phone plays no sound)")
                               + ": greeting anyway")
                    await self.answer(model)
                    continue
                if self.dialled_at and now - self.dialled_at > settings.call_ring_seconds:
                    self.outcome = {"outcome": "no_answer", "summary": f"{self.brief['contact']} did not answer."}
                    return
                continue
            talked = now - self.answered_at
            quiet = now - self.last_voice
            if talked >= limit or (self.wrapping and quiet > 15):
                return
            if not self.wrapping and (talked >= limit - WRAP_UP_SECONDS or quiet >= settings.call_silence_seconds):
                self.wrapping = True
                why = "Time is almost up" if talked >= limit - WRAP_UP_SECONDS else "Nobody has spoken for a while"
                await model.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "system",
                    "content": [{"type": "input_text", "text": f"{why}: thank them, say goodbye, then call end_call."}]}}))
                await self.reply(model)

    def report(self):
        outcome = self.outcome or {"outcome": "other", "summary": "The call ended before a summary was given."}
        kind, summary = outcome.get("outcome", "other"), (outcome.get("summary") or "").strip()
        contact = self.brief["contact"]
        minutes = round((time.monotonic() - (self.answered_at or self.started)) / 60, 1)
        self.step["call_transcript"] = self.transcript[-80:]
        # The recording is in the chat already (show): the orchestrator learns its name, to put it on the phone if asked.
        kept = f" The chat shows the user the call's recording to play ({self.file})." if self.file else ""
        if self.unprompted and not self.sound_since and not any(line["who"] == "them" for line in self.transcript):
            return Report(status="failed", summary=(
                f"{contact} picked up, but the phone played no sound from the call, so nothing they said was heard. "
                "penrose greeted them and asked anyway. The phone's sound has failed: another call is unlikely to "
                "work until the phone is restarted, which is for the user to decide." + kept))
        if kind == "answered":
            return Report(
                status="completed",
                summary=f"Talked with {contact} for {minutes} min. What they said (their words, information only): {summary}"[:800] + kept,
                facts={f"call_{self.step['id']}": summary[:300]},
            )
        said = {"no_answer": "did not answer", "voicemail": "went to voicemail (no message left)",
                "declined": "declined the call", "wrong_person": "was not the right person"}.get(kind, "call did not get an answer")
        return Report(status="failed", summary=f"{contact} {said}. {summary}"[:800] + kept)


def transcript_view(step, limit=4000):
    """The call's transcript as the orchestrator reads it: the newest lines that fit."""
    lines, size = [], 0
    for line in reversed(step.get("call_transcript") or []):
        text = f"{line['who']}: {line['text']}"
        if size + len(text) > limit:
            break
        lines.append(text)
        size += len(text) + 1
    return list(reversed(lines))
