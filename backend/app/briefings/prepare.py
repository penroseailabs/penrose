"""Preparing briefings before the user looks: what a briefing worth their attention becomes without a tap.

A briefing sorted as one the user will want (triage.py, verdict brief) is begun by penrose on its own, as the user's
tap would begin it (actions.start with `background`): its chat, and a task that reads what it is about and ends
with what it found and what it could do next. So the card is ready when the user opens penrose.

Such a task is a background one (its state's `background`) and gives way to the user's own:
- the worker starts one only when it has nothing else to do, and one at a time (`begin_next`);
- it takes the user's own tasks first (store.claim);
- when one of the user's tasks is queued while a background task works, the background task is paused (`pause`):
  stopped as the user's Stop stops a task, its phone released; once the user's task has the worker, it is queued
  again (`resume`) and carries on from what it had found.

Nothing here limits how many are prepared: what is worth preparing is the sort's to say.
"""

import time

from .. import delivery
from ..store import BACKGROUND, halt, put_state, read_state
from . import groups, triage

# A briefing older than this is not begun on penrose's own: by then the user has seen the notification or it is over.
FRESH_SECONDS = 6 * 3600


def busy(store):
    """Whether a background task is queued or working: they go one at a time."""
    return bool(store.rows(
        f"SELECT 1 FROM jobs j JOIN runs r ON r.id=j.run_id WHERE j.status IN ('queued','running') AND {BACKGROUND} IS 1 LIMIT 1"))


def next_up(store, now=None):
    """The briefing to prepare next: one worth the user's attention, not begun or dismissed, the urgent first, then
    the latest. None when there is none."""
    rows = store.rows(
        "SELECT id FROM briefings WHERE verdict=? AND chat_id IS NULL AND dismissed IS NULL AND latest>? "
        "ORDER BY urgent DESC, latest DESC LIMIT 1", (groups.BRIEF, (now or time.time()) - FRESH_SECONDS))
    return rows[0]["id"] if rows else None


def begin_next(store):
    """Begin preparing the next briefing, when no other is being prepared. Returns the run's state, or None. The
    caller makes sure the worker has nothing of the user's to do."""
    # Imported here: actions imports this module, to stop a preparing task when its briefing is dismissed (stop).
    from .actions import start

    if busy(store):
        return None
    if again := carry_on(store):
        return again
    if (briefing_id := next_up(store)) is None:
        return None
    picks = triage.picks(store)
    opened = start(store, briefing_id, picks.get("provider") or "deepseek", picks.get("models"), background=True)
    return opened[1] if opened else None


def carry_on(store):
    """Queue again a preparing task the worker lost (it stopped or restarted while the task worked: the task is
    `interrupted`). A task of the user's is left for them to continue, since an action may have happened; one that
    only reads may simply carry on. Returns its state, or None."""
    rows = store.rows(
        f"SELECT r.id FROM runs r WHERE r.status='interrupted' AND {BACKGROUND} IS 1 "
        "AND json_extract(r.state,'$.preparing') IS 1 ORDER BY r.updated DESC LIMIT 1")
    if not rows:
        return None
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = read_state(db, rows[0]["id"])
        if state["status"] != "interrupted":
            return None
        halt(state)
        state["trigger"] = "resumed"
        try:
            delivery.requeue(db, state)
        except delivery.Busy:
            return None
    return state


def pause(store, run_id):
    """Pause a background task so the user's own can have the worker and the phone: stopped as Stop stops a task,
    and marked to carry on afterwards (`resume`). The worker cancels it on seeing it stopped. True when it was."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = read_state(db, run_id)
        if not state or not state.get("background") or state["status"] not in delivery.WORKING:
            return False
        state["status"] = "stopped"
        state["yielded"] = True
        halt(state)
        put_state(db, state)
        db.execute("UPDATE jobs SET status='done' WHERE run_id=? AND status='queued'", (run_id,))
    return True


def resume(store, run_id):
    """Queue a paused background task again, after the user's own: it carries on from what it found (its
    orchestrator is told it was paused). Returns its state, or None when it was not paused for the user."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        state = read_state(db, run_id)
        if not state or not state.pop("yielded", None) or state["status"] != "stopped":
            return None
        state["trigger"] = "resumed"
        try:
            delivery.requeue(db, state)
        except delivery.Busy:
            state["yielded"] = True  # its job is still ending: the worker asks again when it has
            put_state(db, state)
            return None
    return state


def stop(db, chat_id):
    """Stop penrose's own preparing in this chat, inside the caller's transaction: its briefing was dismissed.
    A task the user has answered is theirs, and is left alone."""
    row = delivery.latest_run(db, chat_id)
    if not row or row["status"] not in (*delivery.WORKING, "stopped"):
        return
    state = read_state(db, row["id"])
    if not state.get("background") or not state.get("preparing"):
        return
    state.pop("yielded", None)
    if state["status"] in delivery.WORKING:
        state["status"] = "stopped"
        halt(state)
    put_state(db, state)
    db.execute("UPDATE jobs SET status='done' WHERE run_id=? AND status='queued'", (row["id"],))
