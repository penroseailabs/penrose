"""Where a user's message goes: the chat's open task, a new task, or, while a task works, the task's inbox.

A message sent while a task works waits in the inbox, in one of two modes:
- "steer" (Enter): it joins the conversation at once, and the task reads it at its next step boundary (the
  phone agent's next turn, the orchestrator's next decision) and carries on with it in mind.
- "next" (Ctrl+Enter): it stays out of the conversation until the task ends, then goes out as the next message.
Whatever the task has not read when its job ends (`settle`) goes out as the next message too.

A notification passed to a working task (app/notifications.py) waits there as well, as a "notice": the task reads
it at its next decision as someone else's words, never as a message of the user's, and one it has not read when
its job ends stays what it already is, an entry in the conversation.
"""

import json
import time

from . import attachments as files
from .plan import node_views
from .store import dump_state, put_state, read_state, uid

# A run in one of these states is an open task: the next message continues it.
OPEN = ("waiting_user", "failed", "interrupted", "stopped")
WORKING = ("queued", "running")
# A run in one of these states is in hand: it works, or it waits for the user's answer. Its chat still follows the
# conversations its steps worked with (notifications.route), and a briefing it was opened from is not over.
IN_HAND = (*WORKING, "waiting_user")
# A run in one of these states does not need the phone: another taker may release its lease
# (runtime/runner.py take_phone, api/phone.py take_phone).
YIELDS_PHONE = (*OPEN, "completed", "cancelled")
MODES = ("steer", "next")
# A notification waiting for a working task: not a message of the user's (app/notifications.py NOTICE).
NOTICE = "notice"


class Busy(Exception):
    """The run's previous job is still unwinding."""


def enqueue(db, run_id):
    db.execute("INSERT INTO jobs VALUES (?,?,'queued',NULL,?)", (uid(), run_id, time.time()))


def requeue(db, state):
    """Hand the run back to the worker, unless its previous job is still unwinding."""
    if db.execute(
        "SELECT id FROM jobs WHERE run_id=? AND status IN ('queued','running')", (state["id"],)
    ).fetchone():
        raise Busy("The worker is finishing this run. Try again in a moment.")
    state["status"] = "queued"
    state["questions"] = []
    state.pop("error", None)
    state["nodes"] = node_views(state)  # the window's view of the state, as the caller returns it
    put_state(db, state)
    enqueue(db, state["id"])


def record(db, chat_id, content, run_id=None, before=None, source=None, attached=None):
    """The user's side of the conversation. `before` is the open run's whole state (dump_state) just before this
    message reached it: a checkpoint, so the user can later go back to this message and write it again. `source`:
    "voice" when the user spoke it (api/voice.py); None when they wrote it. `attached`: the ids of the files it
    carries (app/attachments.py)."""
    message_id = uid()
    now = time.time()
    db.execute(
        "INSERT INTO messages(id,chat_id,role,content,created,source,attachments) VALUES (?,?,?,?,?,?,?)",
        (message_id, chat_id, "user", content, now, source, json.dumps(attached) if attached else None),
    )
    if before is not None and run_id:
        db.execute("INSERT INTO checkpoints VALUES (?,?,?,?)", (message_id, run_id, before, now))
    return message_id


def new_run(db, chat_id, content, provider, created=None, models=None, channel=None, attached=None, title=None):
    """A new task. `models` are the user's picks for the agent and the subagent (agents/catalog.py), or None.
    `channel` "voice": the user spoke the request and will hear the reply, so the agents also write what to say.
    `attached`: the ids of the files the request carries, which the first decisions are shown (app/attachments.py);
    `title`: the chat's first title, when the request is not words (it is then the note on the files)."""
    state = {
        "id": uid(),
        "chat_id": chat_id,
        "request": content,
        "provider": provider,
        **({"models": models} if models else {}),
        **({"channel": channel} if channel else {}),
        **({"attached": attached} if attached else {}),
        "status": "queued",
        "created": created if created is not None else time.time(),
        "plan": [],
        "nodes": {},
        "questions": [],
    }
    put_state(db, state, insert=True)
    db.execute("UPDATE chats SET title=? WHERE id=? AND title='New conversation'", ((title or content)[:55], chat_id))
    enqueue(db, state["id"])
    return state


def latest_run(db, chat_id):
    """The chat's latest run: {"id", "status"}, or None."""
    return db.execute(
        "SELECT id,status FROM runs WHERE chat_id=? ORDER BY updated DESC LIMIT 1", (chat_id,)
    ).fetchone()


def told(db, content, attached):
    """What a task reads of a message: the user's words, then a line on each file it carries."""
    rows = files.rows_of(db, attached)
    return files.with_note(content, rows) if rows else content


def deliver(db, chat_id, content, provider, mode="steer", models=None, channel=None, attachments=None):
    """Give the user's message to the chat's task: into the inbox of a working one, to an open one (which
    continues), or to a new one. Returns the run's state. `models` are the user's picks, which a message carries
    to the task as `provider` does: the task goes on with the latest. `channel` "voice": the user spoke it.
    `attachments`: the rows of the files it carries (attachments.claim): the task reads a line on each beside the
    words, and the photos are shown to its models."""
    ids = [r["id"] for r in attachments or []]
    files.mark_sent(db, ids)
    seen = files.with_note(content, attachments) if attachments else content
    latest = latest_run(db, chat_id)
    if latest and latest["status"] in WORKING:
        current = read_state(db, latest["id"])
        message_id = record(db, chat_id, content, source=channel, attached=ids) if mode == "steer" else None
        db.execute(
            "INSERT INTO inbox(run_id,chat_id,mode,content,provider,message_id,created,models,attachments) VALUES (?,?,?,?,?,?,?,?,?)",
            (current["id"], chat_id, mode, content, provider, message_id, time.time(),
             json.dumps(models) if models else None, json.dumps(ids) if ids else None),
        )
        return current
    if latest and latest["status"] in OPEN:
        # The open task continues: the orchestrator reads the message with everything the task has done so far.
        current = read_state(db, latest["id"])
        before = dump_state(current)
        current.setdefault("inbox", []).append(seen)
        if ids:
            current.setdefault("attached", []).extend(ids)
        # A task penrose began on its own (briefings/prepare.py) is the user's once they answer it: it no longer
        # gives way to their other tasks. (One still working hears them at its next decision, which does the same.)
        current.pop("background", None)
        current.pop("yielded", None)
        current["provider"] = provider
        if models:
            current["models"] = models
        if channel:
            current["channel"] = channel
        requeue(db, current)
        record(db, chat_id, content, current["id"], before, source=channel, attached=ids)
        return current
    begun = time.time()  # before the message is recorded, so the message belongs to the new run
    record(db, chat_id, content, source=channel, attached=ids)
    return new_run(db, chat_id, seen, provider, created=begun, models=models, channel=channel,
                   attached=ids, title=files.title_of(content, attachments or []))


def take(db, run_id, mode=None):
    """The run's waiting messages (all, or one mode), oldest first, taken out of the inbox."""
    where, args = ("run_id=?", (run_id,)) if mode is None else ("run_id=? AND mode=?", (run_id, mode))
    rows = [dict(r) for r in db.execute(f"SELECT * FROM inbox WHERE {where} ORDER BY id", args)]
    if rows:
        db.execute(f"DELETE FROM inbox WHERE id IN ({','.join('?' for _ in rows)})", [r["id"] for r in rows])
    return rows


def settle(db, run_id):
    """After the run's job: what the run did not read goes out as the next message, to the run itself when it is
    still open, else to a new run. Returns that run's state, or None when nothing went out. A stopped or
    discarded run sends nothing: `stop` and `cancel` deal with its inbox."""
    row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row or row["status"] in (*WORKING, "stopped", "cancelled"):
        return None
    rows = [r for r in take(db, run_id) if r["mode"] != NOTICE]  # a notification is in the conversation already
    if not rows:
        return None
    state = read_state(db, run_id)
    before = dump_state(state)
    chat_id, provider = state["chat_id"], rows[-1]["provider"] or state.get("provider")
    models = json.loads(rows[-1]["models"]) if rows[-1].get("models") else state.get("models")
    contents = [told(db, r["content"], r.get("attachments")) for r in rows]
    attached = [i for r in rows for i in json.loads(r.get("attachments") or "[]")]
    if row["status"] in OPEN:
        for r in rows:  # a queued message joins the conversation now; a steering one already has
            if r["mode"] == "next":
                record(db, chat_id, r["content"], run_id, before, attached=json.loads(r.get("attachments") or "null"))
        state.setdefault("inbox", []).extend(contents)
        if attached:
            state.setdefault("attached", []).extend(attached)
        state["provider"] = provider
        if models:
            state["models"] = models
        requeue(db, state)
        return state
    # Done: a new task, begun when the first of these messages was written, so they all belong to it.
    begun = min(r["created"] for r in rows) - 0.001
    for r in rows:
        if r["mode"] == "next":
            record(db, chat_id, r["content"], attached=json.loads(r.get("attachments") or "null"))
    return new_run(db, chat_id, "\n\n".join(contents), provider, created=begun, models=models, attached=attached)


def pending(db, chat_id):
    """The chat's waiting messages, for the window: steering ones not read yet, and queued ones."""
    return [dict(r) for r in db.execute(
        "SELECT id,run_id,mode,content,message_id,created,attachments FROM inbox WHERE chat_id=? AND mode!=? ORDER BY id",
        (chat_id, NOTICE),
    )]
