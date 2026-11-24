"""Conversation compaction: a long chat, kept short to read.

Every agent that talks to the user reads the chat's messages (store.conversation_context). Once they are over
the budget, the older ones are folded into one summary (the chat's `chat_summaries` row) that the agents read
in their place; the newest messages stay word for word. This runs after a run, while the agents are idle, and
never delays a reply. The messages themselves are untouched: the window shows them all, and a rewind that
reaches into the summarised part drops the summary (api/chats.py), which is then written again from them.

The summary folds the summary before it, so a chat is summarised as many times as it grows past the budget
and the agents always read: one summary, then the recent messages.
"""

import asyncio
import logging

from ..agents.registry import COMPACTOR
from ..config import settings
from ..errors import safe_error
from ..models import PRIVATE_MARK
from .memory import PRIVATE_ANSWER, day, provider_of, today

log = logging.getLogger(__name__)

# Characters per token, near enough for a budget (no tokenizer: the providers differ).
CHARS_PER_TOKEN = 4


def tokens(messages):
    """The size of `messages` in a prompt, estimated."""
    return sum(len(m["content"]) // CHARS_PER_TOKEN + 4 for m in messages)


def due(store, chat_id):
    """(the chat's summary row or None, the messages to fold into it) when the chat is over budget, else None.
    The newest conversation_keep_messages stay word for word."""
    if not settings.conversation_budget_tokens:
        return None
    summary = store.summary(chat_id)
    messages = store.public_messages(chat_id, summary["covers_until"] if summary else 0.0)
    if tokens(messages) <= settings.conversation_budget_tokens:
        return None
    keep = settings.conversation_keep_messages
    cut = len(messages) - keep if keep else len(messages)
    # The kept messages are the ones after `covers_until`: a kept message written in the same clock tick as the
    # last folded one (Windows clocks tick every few ms) would be lost, so the cut falls where the time changes.
    while 0 < cut < len(messages) and messages[cut]["created"] == messages[cut - 1]["created"]:
        cut += 1
    fold = messages[:cut]
    return (summary, fold) if fold else None


async def compact(runner, state):
    """Fold the chat's older messages into its summary when it is over budget. Best effort: a failure is logged
    and shown in the trace, and the chat is tried again after the next run."""
    store = runner.store
    try:
        found = due(store, state["chat_id"])
        if not found:
            return
        summary, fold = found
        context = {
            "today": today(),
            "summary_so_far": summary["summary"] if summary else "",
            "messages": [
                {"role": m["role"], "date": day(m["created"]), "content": m["content"]}
                for m in fold if m["content"] not in (PRIVATE_ANSWER, PRIVATE_MARK)
            ],
        }
        usage = runner.on_usage(state, {"stage": COMPACTOR.name})
        turn = await runner.model.ask(COMPACTOR, context, on_usage=usage, **provider_of(state, COMPACTOR))
        text = turn.actions[0].summary.strip()
        folded = (summary["folded"] if summary else 0) + len(fold)
        store.save_summary(state["chat_id"], text, fold[-1]["created"], folded)
        store.event(state["id"], "compacted", {
            "messages": len(fold), "folded": folded,
            "description": f"{len(fold)} older messages were folded into the conversation's summary ({folded} in all).",
        })
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # best effort; the chat is over budget still and is tried again after the next run
        log.warning("compaction failed: %s", safe_error(exc))
        store.event(state["id"], "compaction_failed", {"description": safe_error(exc)})
