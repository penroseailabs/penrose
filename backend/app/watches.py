"""What a chat waits for: watches on the notifications the phone receives.

When a task leaves something that will come back later, on whatever channel (an order's updates by email, from
the store's app or its website; a person's reply; a booking's confirmation), the orchestrator says so as it ends
its turn (tools/orchestrator.py Watch, on finish and ask_user). A watch is a look_up on the notification log that
is kept open: a few short searches (`match`), and a notification that has every word of one of them is passed
to the chat the moment it arrives (notifications.keep), whichever app it comes from. It is matched on the words
the session itself chose while it knew what it had done, never on a guess of what a notification is about.

A watch may also say what to do when it arrives (`then`), which the orchestrator writes from the user's own
request while that request is in front of it. Then the notification can wake the chat: a task starts whose
request is that sentence (`wake`). Waking is held to more than being passed on:

- the notification must come from one of the apps the watch names (`apps`), so a message somewhere else that
  merely repeats the words wakes nothing;
- an agent without tools (WATCH_CHECK) reads the notification and says whether it is what the watch waits for;
- the woken task may use only those apps: its lease is limited to them, and the gateway refuses everything else
  (gateway/scope.py) until the user writes to the task.

A watch with `then` wakes once and is over. One without stays until it expires or the user or the orchestrator
ends it.
"""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

from .delivery import WORKING, Busy, latest_run, new_run, requeue
from .store import put_state, read_state

log = logging.getLogger(__name__)

# A chat keeps at most this many watches open; the oldest gives way.
MAX_OPEN = 8
# A watch with `then` is checked against at most this many notifications, then it is over: its words match too
# much to be what it waits for.
MAX_CHECKS = 40
# Why a watch ended.
WOKE, EXPIRED, CANCELLED, REPLACED, STOPPED, NOISY = "woke", "expired", "cancelled", "replaced", "stopped", "noisy"


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS watches(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,run_id TEXT,what TEXT,
            queries TEXT,todo TEXT,apps TEXT,created REAL,expires REAL,ended REAL,why TEXT,hits INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS watches_open ON watches(ended,chat_id);
    """)


def words(query):
    """The words of a search, as the notification log is searched for them: all of them must be there."""
    return list(dict.fromkeys(w.lower() for w in re.findall(r"[^\W_]+", query or "", re.UNICODE) if len(w) > 1))[:8]


def view(row, zone=None):
    """A watch as the orchestrator and the window read it."""
    apps = json.loads(row["apps"] or "[]")
    until = datetime.fromtimestamp(row["expires"], zone or timezone.utc)
    return {
        "id": row["id"], "what": row["what"], "match": json.loads(row["queries"] or "[]"),
        **({"then": row["todo"], "apps": [a.get("name") or a.get("package") for a in apps]} if row["todo"] else {}),
        "until": until.strftime("%d %b %Y"), "expires": row["expires"], "created": row["created"],
    }


def open_rows(db, chat_id=None, now=None):
    """The watches still open (of one chat, or all), newest first; the ones past their date are closed first."""
    now = now or time.time()
    db.execute("UPDATE watches SET ended=expires,why=? WHERE ended IS NULL AND expires<=?", (EXPIRED, now))
    where, args = ("ended IS NULL AND chat_id=?", (chat_id,)) if chat_id else ("ended IS NULL", ())
    return db.execute(f"SELECT * FROM watches WHERE {where} ORDER BY id DESC", args).fetchall()


def open_for(store, chat_id):
    """The chat's open watches, as views, oldest first."""
    with store.connect() as db:
        rows = open_rows(db, chat_id)
    zone = store.zone()
    return [view(row, zone) for row in reversed(rows)]


def keep(store, state, specs, now=None):
    """Open the watches the orchestrator asked for as it ended its turn (`specs`: tools.orchestrator.Watch as
    dicts). One with the same `what` as an open one of the chat replaces it. Returns the chat's open watches."""
    now = now or time.time()
    chat_id = state["chat_id"]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for spec in specs:
            what = " ".join(spec["what"].split())
            match = [q for q in (" ".join(m.split()) for m in spec["match"]) if words(q)]
            if not match:
                continue
            db.execute("UPDATE watches SET ended=?,why=? WHERE chat_id=? AND ended IS NULL AND lower(what)=lower(?)",
                       (now, REPLACED, chat_id, what))
            then = " ".join((spec.get("then") or "").split())
            apps = [{"name": a.get("name", ""), "package": a["package"]} for a in spec.get("apps") or [] if a.get("package")]
            db.execute(
                "INSERT INTO watches(chat_id,run_id,what,queries,todo,apps,created,expires) VALUES (?,?,?,?,?,?,?,?)",
                (chat_id, state["id"], what, json.dumps(match), then, json.dumps(apps if then else []), now,
                 now + spec.get("days", 14) * 86400),
            )
        rows = open_rows(db, chat_id, now)
        for row in rows[MAX_OPEN:]:
            db.execute("UPDATE watches SET ended=?,why=? WHERE id=?", (now, REPLACED, row["id"]))
    return open_for(store, chat_id)


def end(store, watch_id, why, chat_id=None):
    """Close an open watch (of `chat_id`, when given). True when it was open."""
    where, args = ("id=? AND ended IS NULL", (watch_id,))
    if chat_id:
        where, args = where + " AND chat_id=?", (*args, chat_id)
    with store.connect() as db:
        return db.execute(f"UPDATE watches SET ended=?,why=? WHERE {where}", (time.time(), why, *args)).rowcount > 0


def forget(db, chat_id):
    """The chat is deleted: so are its watches."""
    db.execute("DELETE FROM watches WHERE chat_id=?", (chat_id,))


def has(db, note_id, note, query):
    """Whether the kept notification has every word of `query`: the log's own search (notifications.search), on
    this one row."""
    found = words(query)
    if not found:
        return False
    try:
        match = " AND ".join(f'"{w}"' for w in found)
        return db.execute("SELECT 1 FROM notifications_fts WHERE notifications_fts MATCH ? AND rowid=?",
                          (match, note_id)).fetchone() is not None
    except sqlite3.OperationalError:  # no FTS5: plain substring matches, as the search falls back to
        text = " ".join(str(note.get(k) or "") for k in ("label", "source", "title", "text")).lower()
        return all(w in text for w in found)


def matching(db, note, now):
    """The open watch a notification just kept belongs to, or None: one whose `then` waits for it first (it may
    wake its chat), then the newest."""
    hits = [row for row in open_rows(db, now=now)
            if any(has(db, note["id"], note, query) for query in json.loads(row["queries"] or "[]"))]
    hits.sort(key=lambda row: (not row["todo"], -row["id"]))
    return hits[0] if hits else None


def counted(db, watch):
    """Count a notification against the watch. False when a watch with `then` has now matched too many to be
    what it waits for: it is closed."""
    db.execute("UPDATE watches SET hits=hits+1 WHERE id=?", (watch["id"],))
    if watch["todo"] and watch["hits"] + 1 > MAX_CHECKS:
        db.execute("UPDATE watches SET ended=?,why=? WHERE id=?", (time.time(), NOISY, watch["id"]))
        return False
    return True


def comes_from(watch, note):
    """Whether the notification comes from one of the apps a watch with `then` names."""
    return note["package"] in {a.get("package") for a in json.loads(watch["apps"] or "[]")}


async def wake(runner, watch, note, ring=None):
    """A notification matched a watch that says what to do then: when it comes from one of the watch's apps and
    the check agent finds it is what the watch waits for, start the chat's task on `then`, limited to those apps.
    Returns the task's state, or None when nothing was woken. `ring()` tells the worker a job is queued."""
    from .agents.registry import WATCH_CHECK
    from .runtime.memory import provider_of

    store = runner.store
    if not comes_from(watch, note):
        return None
    origin = store.load(watch["run_id"])
    if not origin:
        return None
    zone, named = store.zone() or timezone.utc, "" if store.zone() else " UTC"
    context = {
        "locale": store.user_locale(),
        "watch": {"waits_for": watch["what"], "then": watch["todo"],
                  "set": datetime.fromtimestamp(watch["created"], zone).strftime("%A %d %B %Y, %H:%M") + named},
        "notification": {k: v for k, v in (
            ("app", note["label"]), ("site", note.get("source")), ("title", note["title"]), ("text", note["text"]),
            ("at", datetime.fromtimestamp(note["posted"], zone).strftime("%A %d %B %Y, %H:%M") + named),
        ) if v},
    }
    usage = runner.on_usage(origin, {"stage": WATCH_CHECK.name})
    verdict = (await runner.model.ask(WATCH_CHECK, context, on_usage=usage, **provider_of(origin, WATCH_CHECK))).actions[0]
    if not verdict.arrived:
        log.info("watch %s: not yet (%s)", watch["id"], verdict.reason[:120])
        return None
    apps = json.loads(watch["apps"] or "[]")
    woken = {
        "waited_for": watch["what"], "then": watch["todo"], "set": context["watch"]["set"],
        "notification": context["notification"],
        "only_apps": [a.get("name") or a["package"] for a in apps],
    }
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM watches WHERE id=? AND ended IS NULL", (watch["id"],)).fetchone():
            return None  # ended meanwhile (the user cancelled it, another notification woke it)
        latest = latest_run(db, watch["chat_id"])
        if latest and latest["status"] in WORKING:
            return None  # a task is working here: it reads the notification at its next decision
        extra = {"trigger": "watch", "woken": woken, "scope": [a["package"] for a in apps]}
        try:
            if latest and latest["status"] == "waiting_user":
                # The chat's task waits for the user: it goes on, with all it has done, on what the watch says.
                state = read_state(db, latest["id"])
                state.update(extra)
                requeue(db, state)
            else:
                began = note.get("delivered") or time.time()
                state = new_run(db, watch["chat_id"], watch["todo"], origin.get("provider"), created=began - 0.001,
                                models=origin.get("models"))
                state.update(extra, route={"orchestrate": True})
                put_state(db, state)
        except Busy:
            return None  # its last job is still unwinding: the next matching notification tries again
        db.execute("UPDATE watches SET ended=?,why=? WHERE id=?", (time.time(), WOKE, watch["id"]))
    store.event(state["id"], "woken", {"watch": watch["what"], "notification": woken["notification"]})
    if ring:
        ring()
    return state
