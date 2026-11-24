"""The dossier: what the orchestrator reads when it works on a briefing (`briefing` in its context).

It holds what arrived, what the same conversation showed before, and the earlier conversations with penrose on
the same matter. Those are found two ways, and all that are found are read, never one: every chat whose steps
worked with this conversation in its app (notifications.worked_in), and every chat that speaks of the
conversation or of the matter by the words the sort gave. A briefing lives in a chat of its own; where it lives
and what it draws on are separate things.
"""

import json

from ..notifications import named
from . import groups

# How much of a briefing the orchestrator is given: its newest notifications, earlier ones from the same
# conversation, and the earlier conversations on the matter.
ARRIVED, EARLIER, RELATED = 20, 15, 4
# Of one earlier conversation: the user's latest requests, and the facts its last task established.
ASKED, FACTS = 3, 12


def chat_brief(store, chat_id, matches=None):
    """An earlier conversation as a briefing draws on it: what the user asked there, how it ended, what it
    established, and when. None for a chat that is gone or holds nothing said."""
    chat = store.rows("SELECT title FROM chats WHERE id=?", (chat_id,))
    asked = store.rows("SELECT content,created FROM messages WHERE chat_id=? AND role='user' ORDER BY created DESC LIMIT ?",
                       (chat_id, ASKED))
    reply = store.last_reply(chat_id)
    if not chat or not (asked or reply):
        return None
    brief = {
        "title": chat[0]["title"],
        "asked": [m["content"][:300] for m in reversed(asked)],
        "last_active": groups.stamp(store, max(m["created"] for m in (*asked, *([reply] if reply else [])))),
    }
    if reply:
        brief["ended_with"] = reply["content"][:700]
    facts = store.rows(
        "SELECT json_extract(state,'$.facts') AS facts FROM runs WHERE chat_id=? AND json_extract(state,'$.facts') IS NOT NULL "
        "ORDER BY updated DESC LIMIT 1", (chat_id,))
    if facts and (known := json.loads(facts[0]["facts"])):
        brief["facts"] = {k: str(v)[:160] for k, v in list(known.items())[:FACTS]}
    if matches:
        brief["mentions"] = [m["excerpt"] for m in matches[:2]]
    return brief


def related(store, briefing, exclude_chat=None):
    """The earlier conversations a briefing draws on, as {chat id: brief}: first the chats whose steps worked with
    this conversation in its app, then the chats that speak of it or of the matter."""
    found = {}
    if briefing["name"]:
        for chat in store.rows("SELECT chat_id,about FROM chat_apps WHERE package=? ORDER BY used DESC", (briefing["package"],)):
            if chat["chat_id"] != exclude_chat and named(chat["about"], briefing["name"], briefing["label"]):
                found.setdefault(chat["chat_id"], None)
    wordings = [w for w in (briefing["name"], *json.loads(briefing["search"] or "[]")) if w and w.strip()]
    if wordings:
        # A chat counts when it has every word of one of the wordings: half of "API key" also finds lost keys.
        for hit in store.search_history(wordings, limit=RELATED + 2, exclude_chat=exclude_chat, coverage=1):
            found[hit["chat_id"]] = hit.get("matches")
    briefs = {}
    for chat_id, matches in found.items():
        if len(briefs) == RELATED:
            break
        if brief := chat_brief(store, chat_id, matches):
            briefs[chat_id] = brief
    return briefs


def arrived(store, briefing):
    """What arrived, as the dossier holds it: the briefing's newest notifications, oldest first, and how many
    earlier ones of the same burst are left out."""
    notes = groups.notes_of(store, briefing, ARRIVED)
    found = {"arrived": [groups.as_read(store, n, 600) for n in notes]}
    if briefing["count"] > len(notes):
        found["more_before_these"] = briefing["count"] - len(notes)
    return found


def build(store, briefing, exclude_chat=None):
    """The dossier of a briefing: where it is from, what arrived, what the same conversation showed before, how
    penrose sorted it, and the earlier conversations on the same matter."""
    found = {"from": {**groups.origin(briefing), "package": briefing["package"]}, **arrived(store, briefing)}
    if before := groups.earlier(store, briefing, EARLIER):
        found["earlier_from_it"] = [groups.as_read(store, n, 200) for n in before]
    if briefing["sorted"] and briefing["headline"]:  # the sorting agent's words, never a notification's own
        found["sorted_as"] = {k: briefing[k] for k in ("headline", "why") if briefing[k]}
    if briefs := related(store, briefing, exclude_chat):
        found["related_conversations"] = list(briefs.values())
    return found
