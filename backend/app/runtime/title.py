"""Naming a chat: once Relay starts to answer a new chat's first message, the title agent reads the message and
gives the chat a short name in place of the first 55 characters of it (delivery.new_run), which stand in until
then. It runs beside the run, so it never delays a reply, and the name reaches the task list the way every
other change to it does (an event on the run wakes the chat list's push, api/updates.py).

A chat is named once: only while its title is still the stand-in, so a chat from before this and a chat the
user renames are left alone.
"""

import asyncio
import logging

from ..agents.registry import TITLER
from ..errors import safe_error
from .memory import provider_of

log = logging.getLogger(__name__)

PLACEHOLDER = "New conversation"
STAND_IN = 55  # characters of the first message that stand in for a title (delivery.new_run)


def due(store, state):
    """True when the reply just written is the chat's first and its title is still the stand-in."""
    asked = store.rows("SELECT COUNT(*) AS n FROM messages WHERE chat_id=? AND role='assistant'", (state["chat_id"],))
    if asked[0]["n"] != 1:
        return False
    row = store.rows("SELECT title FROM chats WHERE id=?", (state["chat_id"],))
    return bool(row) and row[0]["title"] in (PLACEHOLDER, state["request"][:STAND_IN])


def begin(runner, state, reply):
    """Name the chat in the background when `reply` is Relay's first message in it."""
    if not due(runner.store, state):
        return
    task = asyncio.get_running_loop().create_task(name(runner, state, reply))
    runner.background.add(task)  # held, so it is not collected before it ends
    task.add_done_callback(runner.background.discard)


async def name(runner, state, reply):
    """Ask the title agent for a name and give it to the chat, unless the title changed meanwhile. Best effort:
    a failure is logged and the stand-in stays."""
    store = runner.store
    try:
        context = {
            "locale": store.user_locale(),
            "request": state["request"],
            "reply": reply[:1000],
        }
        usage = runner.on_usage(state, {"stage": TITLER.name})
        turn = await runner.model.ask(TITLER, context, on_usage=usage, **provider_of(state, TITLER))
        title = " ".join(turn.actions[0].title.split()).strip(" \"'.")
        if not title:
            return
        with store.connect() as db:
            changed = db.execute(
                "UPDATE chats SET title=? WHERE id=? AND title IN (?,?)",
                (title, state["chat_id"], PLACEHOLDER, state["request"][:STAND_IN]),
            ).rowcount
        if changed:
            store.event(state["id"], "chat_titled", {"title": title})
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # best effort; the chat keeps the start of its first message
        log.warning("naming the chat failed: %s", safe_error(exc))
