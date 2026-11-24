"""What the user does with briefings: choose the apps, open one, look into one, dismiss one, mute its conversation,
take a chat that waits for them off the list.

Opening one starts its chat: the notifications as entries in it, the user's question about them, and a task for
the orchestrator with the dossier. That task reads and prepares and changes nothing (`preparing`) until the user
answers it.
"""

import time

from .. import delivery
from ..notifications import ROLE, SOURCE, line
from ..store import put_state, uid
from . import dossier, groups, policy, prepare

# What the user's tap on a briefing says, as their message in its chat.
ASK = "What can you do about this?"
# How many of a briefing's notifications its chat shows as entries; the dossier holds more.
SHOWN = 8


def choose_app(store, package, on):
    """Brief the user on this app's notifications, or stop. Turning one on also takes in what it showed over the
    last day (groups.take_in); turning one off takes what is listed from it away. Returns the policy."""
    now = time.time()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        before = policy.read(db)
        value = policy.with_app(before, package, on)
        policy.write(db, value)
        if on and package not in before["apps"]:
            groups.take_in(db, package, now)
        if not on:
            groups.drop(db, package, now)
    return value


def choose_models(store, picks):
    """The models sorting and preparing run on (policy.models); None: the same as the user's latest task."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        value = policy.with_models(policy.read(db), picks)
        policy.write(db, value)
    return value


def unmute(store, package, key):
    """Brief the user on a conversation they muted again. Returns the policy."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        value = policy.without_muted(policy.read(db), package, key)
        policy.write(db, value)
    return value


def standing(store, briefing):
    """The briefing that stands for this one's conversation now: itself, or the newer one that took its place
    while its card was still on a screen (groups.gather begins a new one after a long silence)."""
    if not briefing["dismissed"] or briefing["chat_id"]:
        return briefing
    newer = store.rows(
        "SELECT * FROM briefings WHERE package=? AND key=? AND id>? AND dismissed IS NULL AND chat_id IS NULL ORDER BY id DESC LIMIT 1",
        (briefing["package"], briefing["key"], briefing["id"]))
    return newer[0] if newer else briefing


def start(store, briefing_id, provider="deepseek", models=None, background=False):
    """The user's tap on a briefing: its chat, and a task on what can be done about what arrived. Returns (the
    chat's id, the run's state), the state None when the briefing was already opened (now marked seen); None when
    there is no such briefing. Two taps at once open one chat, and what arrived while the tap was being answered is
    in it.

    With `background`, penrose begins it on its own before the user looks (prepare.py): the task is one the worker
    puts after the user's own and pauses for them, and the briefing stays unseen until the user opens it. None then
    also when the briefing was dismissed meanwhile.

    Preparing runs on the models the user chose for notifications, when they chose some; else on the window's
    (`provider`, `models`). What they answer then runs on the window's, as any message does."""
    briefing = groups.find(store, briefing_id)
    if not briefing:
        return None
    if chosen := policy.models(policy.load(store)):
        provider, models = chosen["agent"]["provider"], chosen
    if not background:  # a tap on a card that was still on screen opens what took its place
        briefing = standing(store, briefing)
    briefing_id = briefing["id"]
    # Before the write below: it searches every earlier conversation, which takes a moment.
    found = dossier.build(store, briefing)
    now, chat_id = time.time(), uid()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        briefing = db.execute("SELECT * FROM briefings WHERE id=?", (briefing_id,)).fetchone()
        if briefing["chat_id"] and db.execute("SELECT 1 FROM chats WHERE id=?", (briefing["chat_id"],)).fetchone():
            # Begun already: by the tap just before this one, or by penrose on its own (then this is the user's
            # first look at it).
            if not background:
                db.execute("UPDATE briefings SET seen=COALESCE(seen,?) WHERE id=?", (now, briefing_id))
            return briefing["chat_id"], None
        if background and briefing["dismissed"]:
            return None
        # Nothing joins the briefing while this holds the database: what the chat shows and the dossier's
        # `arrived` are read again, so a notification that came during the search above is in both.
        found.update(dossier.arrived(store, briefing))
        title = (briefing["headline"] or briefing["name"] or briefing["label"] or "Briefing")[:55]
        db.execute("INSERT INTO chats VALUES (?,?,?)", (chat_id, title, now))
        for note in groups.notes_of(store, briefing, SHOWN):  # each as of when it came, never after the question
            message_id = uid()
            db.execute("INSERT INTO messages(id,chat_id,role,content,created,source) VALUES (?,?,?,?,?,?)",
                       (message_id, chat_id, ROLE, line(note), min(note["posted"], now - 0.001), SOURCE))
            db.execute("UPDATE notifications SET chat_id=?,message_id=? WHERE id=?", (chat_id, message_id, note["id"]))
        delivery.record(db, chat_id, ASK)
        state = delivery.new_run(db, chat_id, ASK, provider, created=now - 0.0001, models=models, title=title)
        # The gate is for the user's own words; `preparing`: it only reads until the user answers
        # (runtime/orchestrator.py, agent_prompts/orchestrator.md Briefings).
        state.update(route={"orchestrate": True}, trigger="briefing", briefing=found, preparing=True)
        if background:
            state["background"] = True
        put_state(db, state)
        # The user asked about it: whatever the sort made of it, it is a briefing now, and it is on the list.
        db.execute("UPDATE briefings SET chat_id=?,seen=?,verdict=?,dismissed=NULL WHERE id=?",
                   (chat_id, None if background else now, groups.BRIEF, briefing_id))
    return chat_id, state


def dismiss(store, briefing_id, mute=False):
    """Take a briefing off the list; with `mute`, its conversation is not briefed on again. True when there was
    such a briefing."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM briefings WHERE id=?", (briefing_id,)).fetchone()
        if not row:
            return False
        db.execute("UPDATE briefings SET dismissed=COALESCE(dismissed,?) WHERE id=?", (time.time(), briefing_id))
        if row["chat_id"]:  # penrose's own preparing of it is no longer wanted
            prepare.stop(db, row["chat_id"])
        if mute:
            policy.write(db, policy.with_muted(policy.read(db), row["package"], row["key"],
                                               row["name"] or row["label"], row["label"]))
    return True


def look(store, briefing_id):
    """The user looked into a briefing's details: it is seen, and stays in the list for as long as it would have
    (feed.stays). True when there is such a briefing."""
    with store.connect() as db:
        return db.execute("UPDATE briefings SET seen=COALESCE(seen,?) WHERE id=?", (time.time(), briefing_id)).rowcount > 0


def hide_waiting(store, chat_id):
    """Take a chat that waits for the user off the list, as a notification is swiped away: its task is untouched,
    and it is listed again once the task moves (feed.waiting). True when there is such a chat."""
    with store.connect() as db:
        if not db.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone():
            return False
        db.execute("INSERT INTO for_you_hidden(chat_id,at) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET at=excluded.at",
                   (chat_id, time.time()))
    return True


def forget_chat(db, chat_id):
    """The chat is deleted: a briefing it was opened from is over with it, and so is its place off the list."""
    db.execute("UPDATE briefings SET dismissed=COALESCE(dismissed,?) WHERE chat_id=?", (time.time(), chat_id))
    db.execute("DELETE FROM for_you_hidden WHERE chat_id=?", (chat_id,))
