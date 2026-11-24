"""A task's run: answering it, pausing or cancelling it, and reading what it did (events, the developer trace)."""

import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import bell, delivery
from ..plan import find
from ..store import dump_state, halt, put_state, read_state
from . import common

router = APIRouter(prefix="/api/runs/{run_id}")

# How long an event stream waits for a ring before it reads anyway.
STREAM_CHECK_SECONDS = 10


class ReplyInput(BaseModel):
    """An answer to a run. A plain message goes to the orchestrator; only structured input goes
    straight to the waiting step: a private code, or a note for the step that asked for one (instead)."""

    content: str = Field(min_length=1, max_length=16000)
    node_id: str | None = None
    sensitive: bool = False
    # Instead of the private code, a note for the step that asked ("use email instead"). It reaches
    # that agent as guidance; nothing is typed into the phone.
    instead: bool = False


def locked_state(db, run_id):
    state = read_state(db, run_id)
    if not state:
        raise HTTPException(404, "Run not found")
    return state


@router.get("")
async def run_detail(run_id: str):
    return common.require_run(run_id)


async def settled(run_id, seconds=8.0):
    """The worker shows a run as waiting for the user a few seconds before its job is quite over (it still
    writes the memory). A reply in that window waits for the job instead of failing with 409."""
    state = common.store.load(run_id)
    if not state or state["status"] not in common.OPEN:
        return
    # The worker rings the run's bell when its job is over (worker.py); the second's check is for a ring that never
    # came (an older worker, or one restarting).
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        rung = common.waiters.listen(run_id)
        if not common.store.rows("SELECT id FROM jobs WHERE run_id=? AND status IN ('queued','running')", (run_id,)):
            return
        await bell.wait(rung, min(left, 1.0))


@router.post("/reply", status_code=202)
async def reply(run_id: str, body: ReplyInput):
    await settled(run_id)
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = locked_state(db, run_id)
        if state["status"] not in common.OPEN:
            raise HTTPException(409, "This run is not awaiting a reply")
        selected = find(state, body.node_id) if body.node_id else None
        if body.node_id and (not selected or selected["status"] != "waiting_user"):
            raise HTTPException(422, "Select a step that is waiting for input")
        asked = (selected or {}).get("question", {})
        before = None if body.sensitive or body.instead else dump_state(state)  # a checkpoint to rewind to
        if body.sensitive:
            if not asked.get("sensitive") or len(body.content) > 128 or not body.content.isascii():
                raise HTTPException(
                    422,
                    "Sensitive replies require a step waiting for a private entry and a short ASCII value",
                )
            db.execute("INSERT OR REPLACE INTO secrets VALUES (?,?,?)", (run_id, body.node_id, body.content))
        elif body.instead or asked.get("sensitive"):
            # A note or a plain answer for the step that asked ("OTP is 2793", "use email instead"): it reaches
            # that step's agent, which acts on it.
            if not selected:
                raise HTTPException(422, "Select the step this message is for")
            selected["answers"] = [*selected.get("answers", []), body.content]
        else:
            state.setdefault("inbox", []).append(body.content)
        if selected and (body.sensitive or body.instead or asked.get("sensitive")):
            selected["status"] = "resume"  # its session continues where it waited
            selected.pop("question", None)
        common.requeue(db, state)
        common.record(
            db, state["chat_id"], "[Private code provided]" if body.sensitive else body.content, run_id, before
        )
    common.store.event(run_id, "resumed", {"node_id": body.node_id, "sensitive": body.sensitive})
    return state


@router.post("/stop")
def stop(run_id: str):
    """Pause a working task. It stays open: the next message goes to the orchestrator, which
    corrects the plan or the step concerned and continues."""
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = locked_state(db, run_id)
        if state["status"] not in ("queued", "running"):
            raise HTTPException(409, "Only a working task can be stopped")
        state["status"] = "stopped"
        halt(state)
        # Waiting messages: a steering one is already in the conversation, so the task reads it when the user
        # continues; a queued one never went out, so it goes back to the user's composer.
        waiting = delivery.take(db, run_id)
        state.setdefault("inbox", []).extend(r["content"] for r in waiting if r["mode"] == "steer")
        put_state(db, state)
        db.execute("UPDATE jobs SET status='done' WHERE run_id=? AND status='queued'", (run_id,))
        db.execute("DELETE FROM secrets WHERE run_id=?", (run_id,))
    # A running job releases its devices as it stops; a queued one holds none.
    common.store.event(run_id, "stopped", {})
    return {**state, "unsent": [r["content"] for r in waiting if r["mode"] == "next"]}


@router.post("/cancel")
async def cancel(run_id: str):
    with common.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = locked_state(db, run_id)
        old_status = state["status"]
        if old_status == "completed":
            raise HTTPException(409, "This run has already completed")
        state["status"] = "cancelled"
        put_state(db, state)
        db.execute("UPDATE jobs SET status='done' WHERE run_id=? AND status='queued'", (run_id,))
        db.execute("DELETE FROM secrets WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM inbox WHERE run_id=?", (run_id,))
    if old_status != "running":
        await common.release(common.lease_owners(run_id, state))
    common.store.event(run_id, "cancelled", {})
    return state


@router.get("/events")
def events(run_id: str, after: int = 0):
    common.require_run(run_id)
    return [
        {**r, "payload": json.loads(r["payload"])}
        for r in common.store.rows(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 2000", (run_id, after)
        )
    ]


async def follow(run_id: str, cursor: int):
    """A run's events after `cursor`, as they happen, for as long as the caller reads; None when a while passes
    without one (a keepalive). The window's stream and the companion tunnel both read this."""
    while True:
        rung = common.waiters.listen(run_id)  # before reading, so a ring during the read still wakes us
        batch = common.store.rows(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 200",
            (run_id, cursor),
        )
        for event in batch:
            cursor = event["id"]
            event["payload"] = json.loads(event["payload"])
            yield event
        if len(batch) == 200:
            continue  # more are waiting
        # Woken by the writer's ring; the slow check is for a ring that never came. Until a worker has rung,
        # it may have no doorbell (a bare `relay-server worker`): check often.
        if not await bell.wait(rung, STREAM_CHECK_SECONDS if common.waiters.heard else 0.75):
            yield None


@router.get("/stream")
async def events_stream(run_id: str, request: Request, after: int = 0):
    common.require_run(run_id)
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        cursor = after

    async def generate():
        events = follow(run_id, cursor)
        try:
            async for event in events:
                if await request.is_disconnected():
                    return
                yield ": keepalive\n\n" if event is None else f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"
        finally:
            await events.aclose()

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/ui/{call_id}")
async def ui_call(run_id: str, call_id: int):
    """One UI agent turn exactly as exchanged: the request body sent (a screenshot as its size) and the raw response."""
    common.require_run(run_id)
    rows = common.store.rows("SELECT * FROM ui_calls WHERE run_id=? AND id=?", (run_id, call_id))
    if not rows:
        raise HTTPException(404, "UI agent call not found")
    row = rows[0]
    return {
        **{k: row[k] for k in ("id", "node_id", "created", "seconds", "error")},
        "request": json.loads(row["request"]),
        "response": json.loads(row["response"]) if row["response"] else None,
    }


@router.get("/images/{image_id}")
async def run_image(run_id: str, image_id: int):
    """A screenshot the subagent was shown."""
    common.require_run(run_id)
    rows = common.store.rows("SELECT jpeg FROM images WHERE run_id=? AND id=?", (run_id, image_id))
    if not rows:
        raise HTTPException(404, "Image not found")
    return Response(rows[0]["jpeg"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
