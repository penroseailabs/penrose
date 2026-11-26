"""The reception desk: the voice the user talks to on the phone, which only routes (api/desk.py, prompt
agent_prompts/desk.md).

The desk is an OpenAI Realtime session (speech in, speech out) with session-control tools and nothing else: the
user's words go to a session (a chat and its task) or start one; what the sessions report comes back to the desk
as events, which it tells the user in a sentence. The tasks themselves run as any other, on the user's picked
provider.

The desk keeps no account of its own of the errands. Each time it is about to speak, the phone asks for penrose as
it is at that moment (runtime/deskstate.py `state`: every errand's status, what it is doing, what is on the phone,
its notes, what the user has not been told) and puts that last in the desk's conversation in place of the one
before. So a reply is always made from the present: a deleted errand is not there, a result that changed is the
new one, and nothing is replayed. Beside the state the conversation holds only the last lines said (`items()`, on
connect: the dialogue of the last minutes), so the desk can follow "the second one" and what it was cut off in.

Everything said and done is still written here (`log`): what the user said, what the desk said, each tool call and
its result, and each event. That log is what the tools read for the user's own words (`their_words`), where the
state reads what the desk last acted on, and where an event the user has not heard waits (a card on their screen,
"not told yet" in the state) until it is told. It keeps itself short (`prune`, on every write): the newest KEEP
entries and nothing older than KEEP_DAYS, except an event the user has not heard yet. It is a working memory, not
a record: the chats are the record.

Tool calls return at once; what a session does afterwards shows in the state. `watch` follows the runner's own
event rows and writes a desk event as each is written (progress, for a conversation held by voice; a question, the
result or the failure): to the phone an event is a signal that the state moved and a card for the screen, never
words to play (desk.ts).
"""

import asyncio
import json
import logging
import re
import time

from .. import bell, delivery
from ..agents import prompts
from ..config import settings
from ..store import uid
from . import frontdesk

# The API's routers are imported where they are used: they import this module's neighbours.

log = logging.getLogger(__name__)

SHORT = 6  # characters of an id the desk sees ("s:ab12cd", "m:34ef56")
TURNS = 24  # log entries read at a time (`entries`)
DIALOGUE_LINES = 6  # the last lines said that a connecting session gets back
DIALOGUE_SECONDS = 300  # ...and none older: what was said before that is read from the state, not remembered
KEEP = 200  # log entries kept at all: headroom for a phone catching up on events
KEEP_DAYS = 7  # and nothing older: what the microphone heard a week ago is not worth keeping
READ_CHARS = 1500  # of a reply read aloud (api/voice.py SPEAK_CHARS)
EVENT_CHARS = 600  # of an event; the agents' spoken forms are far shorter, this bounds a written reply in their place
PICTURES = 6  # a reply's pictures the voice screen shows with an event
# A picture in a reply, as the runner writes it (runtime/pictures.py resolve): the run's image URL and its caption.
PICTURE = re.compile(r"!\[([^\]]*)\]\((/api/runs/[0-9a-f]+/images/\d+(?:#\d+x\d+)?)\)")

WORKING = delivery.WORKING

# The tools' first names (before 2026-10-31), so a logged call replays under today's.
RENAMED = {"start": "start_errand", "tell": "pass_on", "stop": "stop_errand", "resume": "resume_errand",
           "rewind": "rewind_errand", "read_more": "read_reply", "search": "find_errand"}


def _arg(session, description, *props):
    properties = {"session": {"type": "string", "description": description}} if session else {}
    for name, text in props:
        properties[name] = {"type": "string", "description": text}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


SESSION = "the session's id from the board or an event, like s:ab12cd"
TOOLS = [
    {"type": "function", "name": "start_errand",
     "description": "Start a session for anything the user wants done, found out or answered that no session in the "
                    "state is about.",
     "parameters": _arg(False, "", ("request", "what the user asked for, in their words"))},
    {"type": "function", "name": "pass_on",
     "description": "Pass the user's words on to a session: an answer to its question, a correction, a follow-up, "
                    "something they tell you about it or where to find something, or anything they want done, "
                    "checked, used or shown. A working session is steered by them; a "
                    "stopped or finished one goes on with them. Everything the user says for an errand goes here: "
                    "you keep nothing yourself.",
     "parameters": _arg(True, SESSION, ("message", "the user's words, as they said them"))},
    {"type": "function", "name": "stop_errand", "description": "Stop a working session.", "parameters": _arg(True, SESSION)},
    {"type": "function", "name": "resume_errand", "description": "Continue a stopped session where it left off.",
     "parameters": _arg(True, SESSION)},
    {"type": "function", "name": "rewind_errand",
     "description": "Take a session back to one of its rewind points on the board, dropping what came after, and send "
                    "new words in place of that message.",
     "parameters": _arg(True, SESSION, ("to", "the rewind point's id, like m:34ef56"),
                        ("message", "what the user wants instead, in their words"))},
    {"type": "function", "name": "read_reply", "description": "The session's latest reply in full, to read to the user.",
     "parameters": _arg(True, SESSION)},
    {"type": "function", "name": "find_errand", "description": "Find earlier sessions by what they were about.",
     "parameters": _arg(False, "", ("query", "a few words of what the user means"))},
]


def instructions():
    return prompts.load("desk")


def session_config(language=""):
    """The Realtime session the phone opens with the desk's client secret (api/desk.py)."""
    return {
        "type": "realtime",
        "model": settings.voice_desk_model,
        "output_modalities": ["audio"],
        "instructions": instructions(),
        "tools": TOOLS,
        "tool_choice": "auto",
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "noise_reduction": {"type": "near_field"},
                "transcription": {"model": settings.voice_transcribe_model, **({"language": language} if language else {})},
                # The words decide when a turn ends, not a timer: a pause in the middle of a thought keeps the
                # turn open. The page sets this again when the line opens (desk.ts TURNS), and falls back to a
                # silence timer if the model refuses it.
                "turn_detection": {
                    "type": "semantic_vad", "eagerness": "low", "create_response": True, "interrupt_response": True,
                },
            },
            "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": settings.voice_tts_voice},
        },
    }


# --- the log ------------------------------------------------------------------------------------------------------

def write(store, kind, payload, announced=1):
    """One entry: kind "user", "assistant", "call", "output" or "event". Returns its id. Each write also prunes."""
    with store.connect() as db:
        cursor = db.execute(
            "INSERT INTO desk_log(kind,payload,created,announced) VALUES (?,?,?,?)",
            (kind, json.dumps(payload, ensure_ascii=False), time.time(), announced),
        )
        prune(db)
        return cursor.lastrowid


def prune(db, now=None):
    """Drop what the desk no longer needs: entries beyond the newest KEEP, and anything older than KEEP_DAYS. An
    event the user has not heard yet stays, whatever its age, until it is told."""
    db.execute(
        "DELETE FROM desk_log WHERE NOT (kind='event' AND announced=0)"
        " AND (created<? OR id<=COALESCE((SELECT id FROM desk_log ORDER BY id DESC LIMIT 1 OFFSET ?),0))",
        ((now or time.time()) - KEEP_DAYS * 86400, KEEP),
    )


def forget(db, chat_id):
    """A deleted chat's events leave the log with it. The turns around them are the user's and the desk's own
    words, which `prune` takes in their time."""
    db.execute("DELETE FROM desk_log WHERE kind='event' AND json_extract(payload,'$.chat_id')=?", (chat_id,))


def entries(store, limit=TURNS):
    rows = store.rows("SELECT * FROM desk_log ORDER BY id DESC LIMIT ?", (limit,))
    return [{**r, "payload": json.loads(r["payload"])} for r in reversed(rows)]


def events_after(store, after):
    rows = store.rows("SELECT * FROM desk_log WHERE kind='event' AND id>? ORDER BY id LIMIT ?", (after, KEEP))
    return [{**r, "payload": json.loads(r["payload"])} for r in rows]


def unannounced(store):
    rows = store.rows("SELECT * FROM desk_log WHERE kind='event' AND announced=0 ORDER BY id")
    return [{**r, "payload": json.loads(r["payload"])} for r in rows]


def latest_event_id(store):
    rows = store.rows("SELECT MAX(id) AS id FROM desk_log WHERE kind='event'")
    return rows[0]["id"] or 0


def announced(store, upto=0, ids=(), chat_id=None):
    """Mark events as heard by the user: those up to `upto`, the ones in `ids`, and all of `chat_id`'s. What the user has
    not heard stays a card on their screen (and a line on the board) until they open it, dismiss it or talk about it."""
    store.execute("UPDATE desk_log SET announced=1 WHERE kind='event' AND id<=?", (upto,))
    for id_ in ids:
        store.execute("UPDATE desk_log SET announced=1 WHERE kind='event' AND id=?", (id_,))
    if chat_id:
        store.execute("UPDATE desk_log SET announced=1 WHERE kind='event' AND json_extract(payload,'$.chat_id')=?", (chat_id,))


# --- ids as the desk sees them -------------------------------------------------------------------------------------

def short(id_):
    return id_[:SHORT]


def resolve(store, session):
    """The chat a desk id (`s:ab12cd`, or the bare prefix) names, among the recent ones; None when it names none."""
    prefix = (session or "").strip().lower().removeprefix("s:")
    if not re.fullmatch(r"[0-9a-f]{3,32}", prefix):
        return None
    rows = store.rows("SELECT id FROM chats WHERE id LIKE ? ORDER BY created DESC LIMIT 2", (prefix + "%",))
    return rows[0]["id"] if len(rows) == 1 else None


# --- the errands' statuses, as the desk reads them --------------------------------------------------------------

# A run's status as the desk reads it, in the state and in a tool's result. A run that is "queued" has only just
# started: read as a word of its own, the desk once told the user an errand was "queued for Monday. Done" (2026-11-07).
STATUS = {"queued": "starting", "running": "working", "completed": "finished", "failed": "failed",
          "stopped": "stopped", "interrupted": "interrupted", "cancelled": "cancelled"}


# --- what a connecting session gets back -----------------------------------------------------------------------------

def _message(role, text):
    kind = "output_text" if role == "assistant" else "input_text"
    return {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}


def pictures_in(text):
    """The pictures in a reply (its image URLs and captions), in order, for the voice screen to show."""
    out, seen = [], set()
    for caption, url in PICTURE.findall(text or ""):
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "caption": " ".join(caption.split())})
    return out[:PICTURES]


def on_screen(pictures):
    """What the screen shows, as the desk reads it: the captions only (it never sees the pictures)."""
    if not pictures:
        return ""
    captions = "; ".join(f'"{p["caption"]}"' for p in pictures if p.get("caption"))
    count = f"{len(pictures)} picture{'s' if len(pictures) > 1 else ''}"
    return f"[on screen: {count}{': ' + captions if captions else ''}]"


def _kind(event):
    return event.get("kind") or {"completed": "finished", "waiting_user": "asks"}.get(event.get("status"), event.get("status"))


# What a session's news is, as a headline: all the desk knows of it until the user opens that session.
HEADLINE = {"finished": "is finished", "asks": "has a question for the user", "failed": "failed"}


def news_text(event):
    """An event the user has not heard, as the desk reads it before they open it: a headline, never the result, so
    the desk cannot read out a session the user did not ask about. Opening it (the phone's card, or the user's
    words) brings the event itself (`event_text`)."""
    kind = _kind(event)
    return (f'[news] s:{short(event["chat_id"])} "{event["title"]}" {HEADLINE.get(kind, kind)}; '
            "the user has not heard it and has not opened it")


def event_json(entry):
    """A logged event as the phone gets it: its words, its headline (`news`) and the session's id for the desk."""
    payload = entry["payload"]
    return {**payload, "id": entry["id"], "text": event_text(payload), "news": news_text(payload),
            "session": f's:{short(payload["chat_id"])}'}


def event_text(event):
    """An event as the desk reads it."""
    kind = _kind(event)
    what = "working" if kind == "progress" else kind
    line = f'[event] s:{short(event["chat_id"])} "{event["title"]}" {what}: {event["text"]}'
    if event.get("private"):
        line += (" (private: a form for it is on the user's screen now; they fill it in there, never aloud. Tell them "
                 "it is on their screen)")
    if event.get("pictures"):
        line += " " + on_screen(event["pictures"])
    return line


def items(store, now=None):
    """The last lines said, as Realtime conversation items, oldest first: what a connecting session starts with.
    Only the dialogue of the last minutes (DIALOGUE_LINES, DIALOGUE_SECONDS), so the desk can follow on from what
    was just said; no tool calls, results or events: what the errands are and did is read from the state
    (runtime/deskstate.py), which the phone adds before each reply. Opening is silent."""
    rows = store.rows(
        "SELECT * FROM desk_log WHERE kind IN ('user','assistant') AND created>? ORDER BY id DESC LIMIT ?",
        ((now or time.time()) - DIALOGUE_SECONDS, DIALOGUE_LINES),
    )
    out = []
    for row in reversed(rows):
        payload = json.loads(row["payload"])
        # A reply that was cut off is remembered as the user heard it, not as it was written.
        text = payload.get("heard", "") if row["kind"] == "assistant" and payload.get("cut") else payload["text"]
        if text:
            out.append(_message(row["kind"], text))
    return out


# --- the tools --------------------------------------------------------------------------------------------------

class ToolError(Exception):
    pass


def _title(store, chat_id):
    rows = store.rows("SELECT title FROM chats WHERE id=?", (chat_id,))
    return rows[0]["title"] if rows else ""


def _session(store, chat_id, **extra):
    latest = store.rows("SELECT status FROM runs WHERE chat_id=? ORDER BY updated DESC LIMIT 1", (chat_id,))
    status = latest[0]["status"] if latest else "new"
    out = {"session": f"s:{short(chat_id)}", "title": _title(store, chat_id), "status": STATUS.get(status, status), **extra}
    if status in WORKING and "note" not in out:
        out["note"] = "Nothing is done yet: what it finds or does comes as events. Never say it is done."
    return out


def _need(store, args):
    chat_id = resolve(store, args.get("session"))
    if not chat_id:
        raise ToolError("No errand has that id: it was deleted, or the id is wrong. The state lists the errands there "
                        "are; if it is not among them, there is nothing to act on, and nothing to look for.")
    return chat_id


def _deliver(store, chat_id, text, provider, models, mode="steer"):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        working = (delivery.latest_run(db, chat_id) or {"status": None})["status"] in WORKING
        try:
            state = delivery.deliver(db, chat_id, text, provider, mode, models, channel="voice")
        except delivery.Busy as exc:
            raise ToolError(str(exc)) from None
    store.event(state["id"], "message_steer" if working else "queued", {})
    return state, working


HEARD_WINDOW = 120  # seconds: what the user said this long ago is not behind the request the desk makes now


def _norm(text):
    return "".join(c for c in text.lower() if c.isalnum())


def heard(store):
    """The user's latest turn as transcribed: what they said since the desk last spoke, behind the tool call
    being made now. A sentence they started over ("Find us a movie on Netflix." then "Find us a movie on Netflix,
    Hulu or Prime Video.") counts once. "" when it is not in the log (yet): then nothing is guessed."""
    rows = store.rows(
        "SELECT kind, payload FROM desk_log WHERE created>? ORDER BY id DESC LIMIT 30", (time.time() - HEARD_WINDOW,)
    )
    lines, skipped = [], 0
    for row in rows:  # newest first
        if row["kind"] == "user":
            lines.append(json.loads(row["payload"]).get("text", ""))
        elif lines and row["kind"] in ("assistant", "output"):
            break  # the desk spoke or acted before their turn: the turn starts after it
        elif row["kind"] == "assistant":
            skipped += 1  # this response's own words, before its call; a second one means their turn is not logged
            if skipped > 1:
                return ""
        elif row["kind"] == "output":
            return ""  # an earlier tool's result comes before any words of theirs: they are not logged yet
    lines.reverse()
    kept = [line for i, line in enumerate(lines) if not any(_norm(line) in _norm(later) for later in lines[i + 1:])]
    return " ".join(kept).strip()


def cut(store, heard, by="user", how=None):
    """The desk's latest reply was stopped before its end, by the user speaking over it or for news (desk.ts
    `interrupt`). Its entry keeps `heard`, the words the user heard: the desk's memory (`items`) and the sessions
    (`their_words`) read that, not the whole reply. False when no reply is logged."""
    rows = store.rows("SELECT id, payload FROM desk_log WHERE kind='assistant' ORDER BY id DESC LIMIT 1")
    if not rows:
        return False
    payload = {**json.loads(rows[0]["payload"]), "heard": heard, "cut": by, **({"how": how} if how else {})}
    store.execute("UPDATE desk_log SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), rows[0]["id"]))
    return True


def interrupted(store):
    """What the user heard of the reply they spoke over, when their latest turn cut one off: "" when the reply
    before their turn played to its end, was cut for news rather than by them, or the desk has acted since. As in
    `heard`, the desk's own word before its call ("Okay.") is not the reply they spoke over, and their turn may
    not be logged yet (a long one transcribes late): a reply the user cut is followed by their words whether
    or not those are in the log."""
    rows = store.rows(
        "SELECT kind, payload FROM desk_log WHERE created>? ORDER BY id DESC LIMIT 30", (time.time() - HEARD_WINDOW,)
    )
    theirs = False
    for row in rows:  # newest first
        if row["kind"] == "output":
            return ""  # the desk acted after that reply: the words it was cut by have been passed on
        if row["kind"] == "user":
            theirs = True
        elif row["kind"] == "assistant":
            payload = json.loads(row["payload"])
            if payload.get("cut") == "user":
                return payload.get("heard", "")
            if theirs:
                return ""  # the reply before their words played whole (or was cut for news)
    return ""


INTERRUPTING = '[interrupting your reply after hearing: "{heard}"]'


def their_words(store, text):
    """What a session is sent for the user: their own words as transcribed, not the desk's retelling of them (a
    voice model slips on long numbers and names when it retells: it passed on 5125550198 as "1 2 5 5 5 0 1 9 8").
    The desk's words only when theirs are not in the log. Words spoken over the desk's reply carry what the user
    had heard of it when they spoke, so "that" and "the second one" mean what they heard, not what the written
    reply goes on to say."""
    words = heard(store) or text
    if cut_at := interrupted(store):
        words += "\n\n" + INTERRUPTING.format(heard=cut_at)
    return words


ACTS = ("start_errand", "pass_on", "stop_errand", "resume_errand", "rewind_errand")  # what only the user's words may do
NOT_THEIRS = ("Not done: no words of the user's ask for this. You are telling news from your own work; an event is "
              "never something to pass on or act on. Tell the user the news, then act only on what they say.")


async def perform(store, name, arguments, provider="deepseek", models=None, for_user=None):
    """Run the desk's tool `name` with `arguments` (JSON text). Returns what the desk is told, as a dict; a problem
    is {"error": ...}: the desk tells the user in a few words. `for_user` False: the reply that made the call only
    tells news (desk.ts), so it may read and search, but not start, steer or stop an errand."""
    if for_user is False and RENAMED.get(name, name) in ACTS:
        log.info("desk: refused %s from a reply that only tells news: %s", name, (arguments or "")[:200])
        return {"error": NOT_THEIRS}
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            raise ToolError("Bad arguments.")
        return await _perform(store, name, args, provider, models)
    except ToolError as exc:
        return {"error": str(exc)}
    except json.JSONDecodeError:
        return {"error": "Bad arguments."}


async def _perform(store, name, args, provider, models):
    from ..api import chats as chats_api
    from ..api import runs as runs_api

    text = " ".join(str(args.get("message") or args.get("request") or "").split())
    if name == "start_errand":
        if not text:
            raise ToolError("Say what the errand is.")
        chat_id = uid()
        store.execute("INSERT INTO chats VALUES (?,?,?)", (chat_id, "New conversation", time.time()))
        _deliver(store, chat_id, their_words(store, text), provider, models)
        return _session(store, chat_id, started=True)
    if name == "find_errand":
        found = store.search_history([str(args.get("query") or "")], limit=5)
        return {"sessions": [
            {"session": f"s:{short(c['chat_id'])}", "title": c["title"], "date": c["date"],
             "matches": [m["excerpt"][:160] for m in c["matches"][:3]]}
            for c in found
        ]}
    if name not in ("pass_on", "stop_errand", "resume_errand", "rewind_errand", "read_reply"):
        raise ToolError(f"No tool called {name}.")
    chat_id = _need(store, args)
    announced(store, chat_id=chat_id)  # the user is talking about it now: its news is heard
    if name == "pass_on":
        if not text:
            raise ToolError("Say what to pass on.")
        waiting = frontdesk.waiting_code(store, chat_id)
        code = (frontdesk.digits(heard(store)) or frontdesk.digits(text)) if waiting else ""  # as heard, first
        if waiting and code:
            run_id, node_id = waiting
            await runs_api.reply(run_id, runs_api.ReplyInput(content=code, node_id=node_id, sensitive=True))
            return _session(store, chat_id, did="sent the code privately")
        _, working = _deliver(store, chat_id, their_words(store, text), provider, models)
        return _session(store, chat_id, did="steered" if working else "continued with it")
    if name == "stop_errand":
        latest = store.rows("SELECT id,status FROM runs WHERE chat_id=? ORDER BY updated DESC LIMIT 1", (chat_id,))
        if not latest or latest[0]["status"] not in WORKING:
            raise ToolError("It is not working right now.")
        runs_api.stop(latest[0]["id"])
        return _session(store, chat_id, did="stopped")
    if name == "resume_errand":
        latest = store.rows("SELECT id,status FROM runs WHERE chat_id=? ORDER BY updated DESC LIMIT 1", (chat_id,))
        if not latest or latest[0]["status"] in WORKING:
            raise ToolError("It is already working." if latest else "It never started.")
        _deliver(store, chat_id, "Go on where you left off.", provider, models)
        return _session(store, chat_id, did="resumed")
    if name == "rewind_errand":
        prefix = str(args.get("to") or "").strip().lower().removeprefix("m:")
        with store.connect() as db:
            points = [m for m in chats_api.rewindable(db, chat_id) if prefix and m.startswith(prefix)]
        if len(points) != 1:
            raise ToolError("No rewind point has that id. Use one from the state.")
        if not text:
            raise ToolError("Say what to do instead.")
        try:
            await chats_api.rewind(chat_id, points[0], chats_api.MessageInput(content=their_words(store, text), provider=provider, models=models))
        except Exception as exc:  # noqa: BLE001 - an HTTPException's detail says why (a working task, no such point)
            raise ToolError(str(getattr(exc, "detail", exc))) from None
        return _session(store, chat_id, did="rewound and sent the new words")
    if name == "read_reply":
        from ..api.voice import plain, shorten

        rows = store.rows(
            "SELECT content, spoken, created FROM messages WHERE chat_id=? AND role='assistant' AND content<>'' ORDER BY created DESC LIMIT 1",
            (chat_id,),
        )
        if not rows:
            raise ToolError("It has not replied yet.")
        shown = pictures_in(rows[0]["content"])
        extra = {"pictures": shown, "on_screen": on_screen(shown)} if shown else {}
        newer = store.rows("SELECT 1 FROM messages WHERE chat_id=? AND role='user' AND created>? LIMIT 1",
                           (chat_id, rows[0]["created"]))
        if newer:  # it has not answered the user's latest words yet: this is not its answer to them
            extra["note"] = ("This reply came before the user's latest message, which the session is still working "
                             "on. Do not read it as the answer: say it is on it, and its news will follow.")
        return _session(store, chat_id, reply=shorten(plain(rows[0]["content"]), READ_CHARS), **extra)
    raise ToolError(f"No tool called {name}.")  # not reached


# --- events: what the sessions report ------------------------------------------------------------------------------
#
# The runner writes a row in `events` for each thing a task does (store.event); the worker rings the API after
# each write (bell.py), and the ring reaches `news`. `watch` follows those rows and turns the ones meant for the
# user into desk events, as they are written, not when the task's status happens to change:
#
#   agent_message  what an agent said in the chat     -> "progress" (while the task works), or the text of what
#                                                        follows it within a moment:
#   questions      the task waits on the user         -> "asks"
#   completed      the task is done                   -> "finished"
#   error          the task failed                    -> "failed"
#
# A message is held for GRACE before it becomes progress, because the orchestrator writes its question or answer
# as a message a moment before the run's status follows: held, it becomes the text of that asks or finished event
# rather than a second one. Progress is told only for a conversation held by voice (the run's `channel`), only
# while the line is open (written announced: it never rings the phone, and it is not news on the next connect).

news = bell.Signal()  # a ring: runs have new event rows
GRACE = 0.8  # seconds a message is held for the status event that may follow it
SLOW_CHECK = 15  # seconds between reads when no ring comes (a ring that was missed)
REPORTED = ("agent_message", "questions", "completed", "error")
KIND = {"questions": "asks", "completed": "finished", "error": "failed"}


def _spoken(message):
    from ..api.voice import plain, shorten

    text = message.get("spoken") or shorten(plain(message.get("content") or ""), EVENT_CHARS)
    return " ".join(text.split())


def event_for(row, held, held_pictures=()):
    """The desk event a runner event row makes, with `held`, the agent's latest message for the run when one is
    being held (its text is what the run then asks or answers, and its pictures are shown with it); None when it
    makes none."""
    from ..api.voice import plain, shorten

    payload = json.loads(row["payload"] or "{}")
    kind, text, private, ask = KIND.get(row["kind"], "progress"), "", False, None
    shown = list(held_pictures)
    if row["kind"] == "agent_message":
        text = _spoken(payload.get("message") or {})
        shown = pictures_in((payload.get("message") or {}).get("content"))
    elif row["kind"] == "questions":
        questions = [q for q in payload.get("questions") or [] if isinstance(q, dict)]
        private = any(q.get("sensitive") or q.get("kind") == "login" for q in questions) or bool(payload.get("private"))
        ask = private_ask(payload, questions)
        text = held or (" ".join(shorten(plain(questions[0].get("question") or ""), EVENT_CHARS).split()) if questions else "")
    elif row["kind"] == "completed":
        if not (payload.get("reply") or payload.get("spoken")):
            return None  # nothing was said
        text = payload.get("spoken") or held or " ".join(shorten(plain(payload["reply"]), EVENT_CHARS).split())
        shown = pictures_in(payload.get("reply")) or shown
    elif row["kind"] == "error":
        text = " ".join(str(payload.get("message") or "").split()) or held or "Something went wrong."
    if not text:
        return None
    event = {"chat_id": row["chat_id"], "title": row["title"] or "", "kind": kind, "text": text, "private": private,
             **({"ask": ask} if ask else {})}
    return {**event, "pictures": shown} if shown and row["kind"] != "error" else event


def private_ask(payload, questions):
    """The form the voice screen opens for a question that wants something private, as it is told: the orchestrator's
    ask ({"kind": "card"...}), or a code or password a phone step waits for. None for a plain question."""
    if isinstance(payload.get("private"), dict) and payload["private"].get("kind"):
        return {"kind": payload["private"]["kind"], "label": str(payload["private"].get("label") or "")[:60]}
    step = next((q for q in questions if q.get("sensitive")), None)
    if not step:
        return None
    question = " ".join(str(step.get("question") or "").split())
    return {"kind": "password" if re.search(r"password|passcode", question, re.IGNORECASE) else "code", "label": question[:80]}


def reported(store, after):
    """The runner events since id `after` that the desk reports, with their run's chat, title and channel."""
    return store.rows(
        """SELECT e.id, e.kind, e.payload, r.id AS run_id, r.chat_id, c.title,
                  json_extract(r.state, '$.channel') AS channel
             FROM events e JOIN runs r ON r.id = e.run_id JOIN chats c ON c.id = r.chat_id
            WHERE e.id > ? AND e.kind IN (?,?,?,?) ORDER BY e.id""",
        (after, *REPORTED),
    )


def report(store, cursor, held, now):
    """One pass: log the desk events the runner's rows since `cursor` make. `held` ({run id: (deadline, event,
    voice)}) carries messages waiting for the status event that may follow; a held one past its deadline becomes
    progress when the conversation is held by voice. Returns (the new cursor, whether anything was logged)."""
    logged = False
    for row in reported(store, cursor):
        cursor = row["id"]
        earlier = held.pop(row["run_id"], None)
        event = event_for(row, earlier[1]["text"] if earlier else "", earlier[1].get("pictures", ()) if earlier else ())
        if event is None:
            continue
        if event["kind"] == "progress":
            held[row["run_id"]] = (now + GRACE, event, row["channel"] == "voice")
            continue
        write(store, "event", event, announced=0)
        logged = True
    for run_id, (deadline, event, voice) in list(held.items()):
        if deadline <= now:
            del held[run_id]
            if voice:
                write(store, "event", event, announced=1)
                logged = True
    return cursor, logged


async def watch(store, changed, listen=None):
    """Follow the runner's events (woken by `listen`, default the rings on `news`) and log a desk event for each
    message, question, result or failure; `changed` tells the streams the desk has news."""
    listen = listen or news.listen
    held = {}
    cursor = (store.rows("SELECT COALESCE(MAX(id), 0) AS id FROM events") or [{"id": 0}])[0]["id"]
    while True:
        rung = listen()
        try:
            cursor, logged = await asyncio.to_thread(report, store, cursor, held, time.monotonic())
            if logged:
                changed()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("desk: reporting the runner's events")
        due = min((deadline for deadline, _, _ in held.values()), default=None)
        wait = SLOW_CHECK if due is None else max(0.0, min(SLOW_CHECK, due - time.monotonic()))
        await bell.wait(rung, wait)
