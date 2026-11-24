"""Conversations (the sidebar's tasks) and the messages that start or continue their runs."""

import json
import time
import typing

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import attachments, delivery, watches
from ..agents.catalog import Models
from ..briefings import actions as briefing_actions
from ..plan import log
from ..store import put_state, read_state, run_states, uid
from . import common

router = APIRouter(prefix="/api/chats")


class MessageInput(BaseModel):
    # Empty only when files go with it (attachments): then the files are the message.
    content: str = Field(default="", max_length=16000)
    # The files it carries: the ids of finished uploads of this chat (api/uploads.py).
    attachments: list[int] = Field(default_factory=list, max_length=attachments.MAX_FILES)
    provider: typing.Literal["deepseek", "openai", "anthropic"] = "deepseek"
    # While a task works: "steer" (Enter) reaches it at its next step; "next" (Ctrl+Enter) waits for it to end.
    mode: typing.Literal["steer", "next"] = "steer"
    # The user's picks for the agent and the subagent: model and effort each (agents/catalog.py). Without them the
    # provider's own models run.
    models: Models | None = None


class PrivateEntry(BaseModel):
    label: str = Field(min_length=1, max_length=60)
    # Typed into the phone as is: printable ASCII, as the phone's text entry takes it (gateway/actions.type_text).
    value: str = Field(min_length=1, max_length=128)


class PrivateInput(BaseModel):
    """Values the user gives the chat privately, asked for or not: the agents learn that they exist and their
    labels, never what they are, and the runtime types one where an agent asks (device/driver.py)."""

    entries: list[PrivateEntry] = Field(min_length=1, max_length=6)
    # None (the voice screen): the task goes on with the provider it has.
    provider: typing.Literal["deepseek", "openai", "anthropic"] | None = None
    models: Models | None = None


@router.get("")
def chats():
    # Each chat with its latest run's status and last activity, for the task list.
    return common.store.rows(
        """SELECT c.*,
             (SELECT r.status FROM runs r WHERE r.chat_id = c.id
               ORDER BY r.updated DESC LIMIT 1) AS status,
             MAX(c.created, COALESCE((SELECT MAX(r.updated) FROM runs r WHERE r.chat_id = c.id), 0)) AS updated
           FROM chats c ORDER BY updated DESC"""
    )


@router.post("", status_code=201)
def new_chat():
    chat = {"id": uid(), "title": "New conversation", "created": time.time()}
    common.store.execute("INSERT INTO chats VALUES (?,?,?)", tuple(chat.values()))
    return chat


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str):
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        chat = db.execute("SELECT id FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat:
            raise HTTPException(404, "Conversation not found")
        run_rows = db.execute("SELECT id,state FROM runs WHERE chat_id=?", (chat_id,)).fetchall()
        # The nodes are the plan's steps (lease owners of runs saved before the plan).
        node_owners = [owner for row in run_rows for owner in common.lease_owners(row["id"], json.loads(row["state"]))]
        for row in run_rows:
            for table in RUN_TABLES:
                db.execute(f"DELETE FROM {table} WHERE run_id=?", (row["id"],))
        if node_owners:
            placeholders = ",".join("?" for _ in node_owners)
            db.execute(f"DELETE FROM leases WHERE owner IN ({placeholders})", node_owners)
        db.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        attachments.delete_chat_files(db, chat_id)
        db.execute("DELETE FROM private_entries WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM usage WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM chat_usage WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM runs WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        # The notifications passed to it stay in the log, as ones no chat holds.
        db.execute("DELETE FROM chat_apps WHERE chat_id=?", (chat_id,))
        watches.forget(db, chat_id)
        db.execute("UPDATE notifications SET chat_id=NULL,message_id=NULL WHERE chat_id=?", (chat_id,))
        briefing_actions.forget_chat(db, chat_id)
        # Learned memories outlive the chat; they are managed in the memory panel.
        db.execute("DELETE FROM memory_scans WHERE chat_id=?", (chat_id,))
        db.execute("DELETE FROM chat_summaries WHERE chat_id=?", (chat_id,))
        from ..runtime import (
            desk,  # the desk's news about the chat goes with it (imported here: desk imports this module)
        )

        desk.forget(db, chat_id)
    await common.release(node_owners)
    return {"deleted": True}


# What belongs to a run besides its row: dropped with it.
RUN_TABLES = ("jobs", "secrets", "events", "ui_calls", "images", "checkpoints", "inbox", "turns")


def chat_runs(db, chat_id):
    """The chat's runs (their saved states), in the order they began."""
    return sorted(run_states(db, chat_id), key=lambda s: s.get("created", 0))


def run_of(runs, message):
    """The run a user's message belongs to: the last one that had begun when it was written."""
    return next((s for s in reversed(runs) if s.get("created", 0) <= message["created"]), None)


def rewindable(db, chat_id, runs=None):
    """The ids of the user's messages that can be rewritten: the one that began a task (the task is dropped
    and starts again) and the ones sent to a task that was waiting, which have a checkpoint to go back to.
    `runs`: the chat's runs, when already read."""
    runs = sorted(runs, key=lambda s: s.get("created", 0)) if runs is not None else chat_runs(db, chat_id)
    saved = {r["message_id"] for r in db.execute(
        "SELECT message_id FROM checkpoints WHERE run_id IN (SELECT id FROM runs WHERE chat_id=?)", (chat_id,))}
    firsts, ids = set(), set()
    for message in db.execute("SELECT id,created FROM messages WHERE chat_id=? AND role='user' ORDER BY created", (chat_id,)):
        run = run_of(runs, message)
        if run and run["id"] not in firsts:
            firsts.add(run["id"])
            ids.add(message["id"])
        elif message["id"] in saved:
            ids.add(message["id"])
    return sorted(ids)


def run_view(state):
    """A run as the window reads it: its nodes (the plan's view) and status, without the plan itself, which the
    nodes repeat, and without a briefing's dossier, which only the orchestrator reads."""
    return {k: v for k, v in state.items() if k not in ("plan", "briefing")}


def thoughts(db, chat_id):
    """What the chat keeps of the gate's and the orchestrator's turns besides their messages (Runner.call's
    `turn_thought`), oldest first: the notes a turn kept for itself, and its reasoning when it said nothing to the
    user. `id` is the turn's call, which is also the id of the message it wrote, when it wrote one."""
    rows = db.execute(
        "SELECT payload,created FROM events WHERE kind='turn_thought' AND run_id IN (SELECT id FROM runs WHERE chat_id=?) "
        "ORDER BY id", (chat_id,))
    return [{"id": p.get("call_id"), "created": row["created"], "thinking": p.get("thinking") or "", "notes": p.get("notes") or []}
            for row in rows for p in [json.loads(row["payload"])]]


@router.get("/{chat_id}")
def chat_detail(chat_id: str):
    # A plain function: FastAPI runs it on a worker thread, so reading a long chat never holds up other requests.
    common.require_chat(chat_id)
    with common.store.connect() as db:
        runs = run_states(db, chat_id)
        return {
            "messages": attachments.views_for(
                common.store, [dict(r) for r in db.execute("SELECT * FROM messages WHERE chat_id=? ORDER BY created", (chat_id,))]),
            "thoughts": thoughts(db, chat_id),
            "runs": [run_view(state) for state in runs],
            "rewindable": rewindable(db, chat_id, runs),
            "pending": delivery.pending(db, chat_id),
            # What the chat waits for among the phone's notifications (app/watches.py), oldest first.
            "watches": [watches.view(row, common.store.zone()) for row in reversed(watches.open_rows(db, chat_id))],
            **dict(zip(("usage", "requests"), common.store.chat_usage(chat_id))),
        }


def deliver(db, chat_id, content, provider, mode="steer", models=None, channel=None, attachments=None):
    """Give the user's message to the chat's task (see app.delivery). Returns the run's state."""
    try:
        return delivery.deliver(db, chat_id, content, models.agent.provider if models else provider, mode,
                                models.model_dump() if models else None, channel=channel, attachments=attachments)
    except delivery.Busy as exc:
        raise HTTPException(409, str(exc)) from None


def claimed(db, chat_id, ids):
    """The rows of the files a message carries, checked (422 when one is not this chat's or not finished)."""
    try:
        return attachments.claim(db, chat_id, ids)
    except attachments.Rejected as exc:
        raise HTTPException(422, str(exc)) from None


@router.post("/{chat_id}/messages", status_code=202)
def send_message(chat_id: str, body: MessageInput):
    common.require_chat(chat_id)
    if not body.content.strip() and not body.attachments:
        raise HTTPException(422, "Message cannot be blank")
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        files = claimed(db, chat_id, body.attachments)
        working = (delivery.latest_run(db, chat_id) or {"status": None})["status"] in delivery.WORKING
        state = deliver(db, chat_id, body.content, body.provider, body.mode, body.models, attachments=files)
    # While the task works, the message waits in its inbox; otherwise the task starts or continues.
    common.store.event(state["id"], f"message_{body.mode}" if working else "queued", {})
    return state


@router.post("/{chat_id}/private", status_code=202)
def send_private(chat_id: str, body: PrivateInput):
    common.require_chat(chat_id)
    entries = [(e.label.strip(), e.value) for e in body.entries]
    if any(not label for label, _ in entries):
        raise HTTPException(422, "Each private entry needs a label")
    if any(not value.isascii() or any(ord(c) < 32 for c in value) for _, value in entries):
        raise HTTPException(422, "A private entry can hold letters, digits and symbols, without accents or line breaks")
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        latest = delivery.latest_run(db, chat_id)
        working = (latest or {"status": None})["status"] in delivery.WORKING
        provider = body.provider or (read_state(db, latest["id"]) or {}).get("provider") if latest else body.provider
        added = common.store.add_private(db, chat_id, entries)
        # The conversation says what was given, never the value: the task reads it like any message from the user.
        said = ", ".join(f"{a['label']} (#{a['n']})" for a in added)
        state = deliver(db, chat_id, f"🔒 Sent privately: {said}", provider or "deepseek", "steer", body.models)
    common.store.event(state["id"], "message_steer" if working else "queued", {"private": [a["n"] for a in added]})
    return state


@router.delete("/{chat_id}/queued/{entry_id}")
def unqueue(chat_id: str, entry_id: int):
    """Take back a message queued for after the task (Ctrl+Enter) before it goes out."""
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT content FROM inbox WHERE id=? AND chat_id=? AND mode='next'", (entry_id, chat_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "That message has already gone out")
        db.execute("DELETE FROM inbox WHERE id=?", (entry_id,))
    return {"content": row["content"]}


@router.delete("/{chat_id}/watches/{watch_id}")
def stop_watching(chat_id: str, watch_id: int):
    """The user ends one of the chat's watches: its notifications are no longer passed here, and it wakes nothing."""
    common.require_chat(chat_id)
    if not watches.end(common.store, watch_id, watches.CANCELLED, chat_id=chat_id):
        raise HTTPException(404, "That watch is already over")
    return {"watches": watches.open_for(common.store, chat_id)}


# What the user's rewind takes back, as the orchestrator reads it: the phone keeps what was done on it.
LOST_WORK = (
    "The user went back to an earlier message and rewrote it. Everything below is what the task had done "
    "since then; the plan no longer holds it, but it already happened on the phone, so check before repeating it"
)


def drop_run(db, run_id):
    db.execute("DELETE FROM runs WHERE id=?", (run_id,))
    for table in RUN_TABLES:
        db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))


@router.post("/{chat_id}/messages/{message_id}/rewind", status_code=202)
async def rewind(chat_id: str, message_id: str, body: MessageInput):
    """Go back to the user's message `message_id`, drop it and everything after it, and send `body.content` in
    its place. A message that began a task drops the task. One sent to a waiting task puts the task back as it
    was just before, though the phone keeps whatever the task did on it since."""
    common.require_chat(chat_id)
    if not body.content.strip() and not body.attachments:
        raise HTTPException(422, "Message cannot be blank")
    freed = []
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        message = db.execute(
            "SELECT * FROM messages WHERE id=? AND chat_id=? AND role='user'", (message_id, chat_id)
        ).fetchone()
        if not message:
            raise HTTPException(404, "Message not found")
        if message_id not in rewindable(db, chat_id):
            raise HTTPException(422, "This message can no longer be rewritten")
        runs = chat_runs(db, chat_id)
        if any(s["status"] in ("queued", "running") for s in runs):
            raise HTTPException(409, "Stop the running task first, then rewrite the message")
        if db.execute(
            "SELECT 1 FROM jobs WHERE status IN ('queued','running') AND run_id IN (SELECT id FROM runs WHERE chat_id=?)",
            (chat_id,),
        ).fetchone():
            raise HTTPException(409, "The worker is finishing this run. Try again in a moment.")
        run = run_of(runs, message)
        later = [s for s in runs if s["id"] != run["id"] and s.get("created", 0) > run.get("created", 0)]
        checkpoint = db.execute("SELECT state FROM checkpoints WHERE message_id=?", (message_id,)).fetchone()
        for dropped in later:
            freed += common.lease_owners(dropped["id"], dropped)
            drop_run(db, dropped["id"])
        if checkpoint:
            restored = json.loads(checkpoint["state"])
            since = run.get("journal", [])[len(restored.get("journal", [])):]
            if since:
                log(restored, f"{LOST_WORK}: " + "; ".join(j["line"] for j in since[-8:]))
            for table in ("events", "ui_calls", "images"):
                db.execute(f"DELETE FROM {table} WHERE run_id=? AND created>=?", (run["id"], message["created"]))
            db.execute("DELETE FROM secrets WHERE run_id=?", (run["id"],))
            put_state(db, restored)
        else:
            freed += common.lease_owners(run["id"], run)
            drop_run(db, run["id"])
        db.execute(
            "DELETE FROM checkpoints WHERE message_id IN (SELECT id FROM messages WHERE chat_id=? AND created>=?)",
            (chat_id, message["created"]),
        )
        db.execute("DELETE FROM messages WHERE chat_id=? AND created>=?", (chat_id, message["created"]))
        # A summary that folded a dropped message no longer holds; it is written again from the messages that stay.
        db.execute("DELETE FROM chat_summaries WHERE chat_id=? AND covers_until>=?", (chat_id, message["created"]))
        first = db.execute("SELECT 1 FROM messages WHERE chat_id=? AND role='user'", (chat_id,)).fetchone()
        if not first:
            db.execute("UPDATE chats SET title='New conversation' WHERE id=?", (chat_id,))
        state = deliver(db, chat_id, body.content, body.provider, models=body.models,
                        attachments=claimed(db, chat_id, body.attachments))
        if not checkpoint and run.get("briefing") and state["id"] != run["id"]:
            # The task that replaces one begun from a briefing begins as it did: with the briefing's dossier, straight
            # to the orchestrator. Sent again unchanged, penrose's own question still only prepares (actions.start).
            state.update(route=run.get("route"), trigger=run.get("trigger"), briefing=run["briefing"])
            if body.content.strip() == (run.get("request") or "").strip() and run.get("preparing"):
                state["preparing"] = True
            put_state(db, state)
    await common.release(freed)
    common.store.event(state["id"], "queued", {})
    return state
