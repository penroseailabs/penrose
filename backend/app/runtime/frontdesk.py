"""Where the user's spoken words go (voice mode, api/voice.py).

In the window the user picks the conversation in the task list; on voice there is no list, so Relay picks. Each
conversation holds one errand's plan, facts and open questions: an answer, a correction or a follow-up must reach
its own conversation (a message to a waiting task answers it; one to a working task steers it), and a new errand
gets a new one. Plain cases are decided here by rule; the rest go to the front desk agent (agents/registry.py
FRONTDESK), a fast-tier call that reads what the user said beside the recent conversations, the pending questions
and the earlier conversations that share words with it, and says where it belongs and what to say back.

Deprecated: voice talks to the reception desk (runtime/desk.py) whenever an OpenAI key is set, and without one
voice does not work at all, so this path runs only with the desk switched off (VOICE_DESK=0). It stays as that
fallback. The helpers (`chats`, `about`, `question_of`, `waiting_code`, `digits`) are the desk's too; only
`decide` and what feeds it are unused while the desk is on.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import delivery
from ..agents.catalog import route as model_route
from ..agents.registry import FRONTDESK
from ..errors import safe_error
from ..store import read_state

log = logging.getLogger(__name__)

RECENT = 12  # conversations the front desk sees
LAST_MESSAGES = 6  # of the focus conversation
ABOUT = 240  # characters of what a conversation was about

# Digits as people say them. "oh" and "o" are read as zero only while a code is waited for.
NUMBER_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
# Words around a code that mean nothing ("the code is 2793", "it's 2 7 9 3").
FILLER = {"the", "code", "is", "it", "its", "my", "otp", "pin", "number", "and", "um", "uh", "okay", "ok", "here", "this", "a", "yes"}
CODE = re.compile(r"^\d{4,10}$")


@dataclass
class Decision:
    chat_id: str  # "" starts a new conversation
    code: bool
    acknowledgment: str
    decided_by: str  # "rule" or "front desk", for the trace


def digits(text):
    """`text` as the code it says ("two seven, nine three" -> "2793"), or "" when it says anything else."""
    out = []
    for word in re.findall(r"[^\W_]+", text.lower().replace("'", "")):
        if word.isdigit():
            out.append(word)
        elif word in NUMBER_WORDS:
            out.append(NUMBER_WORDS[word])
        elif word not in FILLER:
            return ""
    code = "".join(out)
    return code if CODE.match(code) else ""


def question_of(state):
    """The question a run waits on: {node_id, question, sensitive}, or None."""
    questions = state.get("questions") or [] if state else []
    return dict(questions[0]) if questions else None


def chats(store):
    """The latest conversations with their task's status, newest first."""
    return store.rows(
        """SELECT c.id, c.title, c.created,
             (SELECT r.status FROM runs r WHERE r.chat_id = c.id ORDER BY r.updated DESC LIMIT 1) AS status,
             (SELECT r.id FROM runs r WHERE r.chat_id = c.id ORDER BY r.updated DESC LIMIT 1) AS run_id,
             MAX(c.created, COALESCE((SELECT MAX(r.updated) FROM runs r WHERE r.chat_id = c.id), 0)) AS updated
           FROM chats c ORDER BY updated DESC LIMIT ?""",
        (RECENT,),
    )


def about(store, chat_id):
    """What a conversation was about: its summary, else its first message."""
    summary = store.summary(chat_id)
    if summary and summary.get("summary"):
        return summary["summary"][:ABOUT]
    rows = store.rows("SELECT content FROM messages WHERE chat_id=? AND role='user' ORDER BY created LIMIT 1", (chat_id,))
    return rows[0]["content"][:ABOUT] if rows else ""


def card(store, row, zone, state=None):
    card = {
        "id": row["id"], "title": row["title"], "date": store.local_day(row["updated"], zone),
        "status": row["status"] or "new", "about": about(store, row["id"]),
    }
    if row["status"] == "waiting_user" and state is not None:
        card["question"] = question_of(state)
    return card


def context_for(store, text, focus_id):
    """What the front desk reads: the utterance, the focus conversation, the ones waiting on the user, the recent
    ones and the earlier ones that share words with the utterance."""
    zone = store.zone()
    rows = chats(store)
    states = {}
    with store.connect() as db:
        for row in rows:
            if row["status"] == "waiting_user" or row["id"] == focus_id:
                states[row["id"]] = read_state(db, row["run_id"]) if row["run_id"] else None
    focus = None
    focus_row = next((r for r in rows if r["id"] == focus_id), None)
    if focus_row:
        focus = card(store, focus_row, zone, states.get(focus_id))
        focus["conversation"] = [
            {"role": m["role"], "content": m["content"][:600]} for m in store.conversation(focus_id, limit=LAST_MESSAGES)
        ]
        if focus_row["status"] == "waiting_user":
            focus["question"] = question_of(states.get(focus_id))
    waiting = [card(store, r, zone, states.get(r["id"])) for r in rows if r["status"] == "waiting_user" and r["id"] != focus_id]
    recent = [card(store, r, zone) for r in rows if r["id"] != focus_id]
    related = [
        {"id": c["chat_id"], "title": c["title"], "date": c["date"], "matches": [m["excerpt"] for m in c["matches"]]}
        for c in store.search_history([text], limit=5)
    ]
    return {
        "locale": store.user_locale(),
        "utterance": text,
        "focus": focus,
        "waiting": waiting,
        "recent": recent,
        "related": related,
        "current_datetime_utc": datetime.now(timezone.utc).isoformat(),
    }


def by_rule(store, text, focus_id):
    """The plain cases, decided without a model: a code for the focus conversation's waiting step, or nothing to
    choose from. None when the front desk must decide."""
    rows = chats(store)
    focus_row = next((r for r in rows if r["id"] == focus_id), None)
    if focus_row and focus_row["status"] == "waiting_user" and focus_row["run_id"]:
        with store.connect() as db:
            question = question_of(read_state(db, focus_row["run_id"]))
        if question and question.get("sensitive") and digits(text):
            return Decision(focus_id, True, "Sending the code.", "rule")
    if not rows:
        return Decision("", False, "On it.", "rule")
    return None


async def decide(model, store, text, focus_id=None, provider="deepseek", models=None):
    """Where `text` goes: a Decision. The rules first; the front desk agent otherwise, on the user's picked
    agent provider's fast model (`provider`, `models` as a message carries them). When the agent fails, the
    focus conversation continues, or a new one starts."""
    ruled = by_rule(store, text, focus_id)
    if ruled:
        return ruled
    context = context_for(store, text, focus_id)
    known = {c["id"] for c in context["recent"]} | {c["id"] for c in context["related"]}
    if context["focus"]:
        known.add(context["focus"]["id"])
    try:
        turn = await model.ask(FRONTDESK, context, **model_route({"provider": provider, "models": models}, FRONTDESK))
        action = turn.actions[0]
    except Exception as exc:  # noqa: BLE001 - the user is waiting: fall back rather than fail
        log.warning("front desk failed: %s", safe_error(exc))
        return Decision(focus_id if context["focus"] else "", False, "On it.", "fallback")
    chat_id = action.chat_id if action.chat_id in known else ""
    code = bool(action.code and chat_id and digits(text))
    acknowledgment = " ".join(action.acknowledgment.split()) or "On it."
    return Decision(chat_id, code, acknowledgment, "front desk")


def waiting_code(store, chat_id):
    """The run and step in `chat_id` waiting for a private entry: (run_id, node_id), or None."""
    with store.connect() as db:
        latest = delivery.latest_run(db, chat_id)
        if not latest or latest["status"] != "waiting_user":
            return None
        question = question_of(read_state(db, latest["id"]))
    if not question or not question.get("sensitive"):
        return None
    return latest["id"], question.get("node_id")


def trace(decision, text):
    """One line for the log."""
    return json.dumps({"to": decision.chat_id or "new", "code": decision.code, "by": decision.decided_by, "words": len(text.split())})
