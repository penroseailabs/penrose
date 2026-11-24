"""Reflection: every few tasks, what the recent tasks show together.

The memory agent (runtime/memory.py) runs after every task and keeps what that task's messages and lessons
hold. Reflection runs every reflect_every_tasks finished tasks and reads them all at once, with the chats'
summaries (runtime/compaction.py) and every memory: it writes the routines and repeated trouble no single
task shows, and replaces or forgets memories the tasks contradict. Its memories are kept with source
"reflection", so the memory panel says where they came from.

The count of tasks since the last reflection is one profile row (REFLECTION_KEY): no clock, no scheduler.
"""

import asyncio
import json
import logging
import time

from ..agents.registry import REFLECTION
from ..config import settings
from ..errors import safe_error
from .memory import apply, context_for, day, decided, provider_of

log = logging.getLogger(__name__)

REFLECTION_KEY = "reflection"  # {"tasks": finished since the last reflection, "last": when that was}
JOURNAL_TAIL = 12


def recent_tasks(store, since, limit):
    """The planned tasks finished since `since`, newest first, as the reflection agent reads them."""
    tasks = []
    for row in store.rows("SELECT state FROM runs WHERE updated>? ORDER BY updated DESC LIMIT ?", (since, limit * 3)):
        state = json.loads(row["state"])  # the saved head: everything but the steps' turns
        if not state.get("plan"):
            continue
        tasks.append({
            "request": state["request"], "status": state["status"], "date": day(state.get("created") or time.time()),
            "facts": state.get("facts", {}), "journal": [j["line"] for j in state.get("journal", [])][-JOURNAL_TAIL:],
        })
        if len(tasks) >= limit:
            break
    return tasks


def recent_conversations(store, since):
    """The chat summaries written since `since`, with their chats' titles."""
    return [
        {"title": r["title"], "date": day(r["updated"]), "summary": r["summary"]}
        for r in store.rows(
            "SELECT c.title,s.summary,s.updated FROM chat_summaries s JOIN chats c ON c.id=s.chat_id WHERE s.updated>? ORDER BY s.updated",
            (since,),
        )
    ]


async def reflect(runner, state):
    """Count the finished task; every reflect_every_tasks of them, run the reflection agent. Best effort: a
    failure is logged and shown in the trace, and reflection is tried again after the next task."""
    store = runner.store
    try:
        every = settings.reflect_every_tasks
        if not every or not store.profile_get("auto_memory", True):
            return
        counter = store.profile_get(REFLECTION_KEY, {}) or {}
        tasks, last = counter.get("tasks", 0) + 1, counter.get("last", 0)
        if tasks < every:
            store.profile_set(REFLECTION_KEY, {**counter, "tasks": tasks})
            return
        context, existing = context_for(
            store, recent_tasks=recent_tasks(store, last, every), recent_conversations=recent_conversations(store, last),
        )
        usage = runner.on_usage(state, {"stage": REFLECTION.name})
        action = await decided(
            lambda c: runner.model.ask(REFLECTION, c, on_usage=usage, **provider_of(state, REFLECTION)), context, store)
        saved, removed = apply(store, action, existing, source="reflection")
        store.profile_set(REFLECTION_KEY, {"tasks": 0, "last": time.time()})
        if saved or removed:
            store.event(state["id"], "memories_updated", {"added": saved, "removed": removed, "reflection": True})
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # best effort; the count stands, so the next task brings reflection again
        log.warning("reflection failed: %s", safe_error(exc))
        store.event(state["id"], "memory_failed", {"description": safe_error(exc), "reflection": True})
