"""Notifications from the phone, kept as events and passed to the chats they belong to.

A notification is an event, not a document: it says that something happened, in which app, from whom and when.
The message, the email or the order itself is in the app. So nothing here stands in for an app's content:

- Every notification is kept in a log (`notifications`): the app, who or what it is from, when it came and the
  start of what it said. The orchestrator's look_up reads the log as leads (`search`): where to read, never the
  whole story, and only since the day penrose began keeping them.
- One a chat waits for is also passed to that chat (`deliver`). It joins the chat as a `notification` entry,
  which the agents read as someone else's words: a working task reads it at its next decision, a chat with no
  task working just keeps it. It is never a request; only the user's own messages are.

Which chat is never a guess of what a notification is about. Two things decide it, both set by the chat's own
work:
- A watch (app/watches.py): the words the chat's task said will be in what comes back, whichever app brings it
  (an order's number in the store's email, in its app's alert, in its website's). A watch comes first, and one
  that says what to do then can wake its chat (`watches.wake`).
- Where the chat's steps worked (`route`): a chat counts for an app while its task is still open (working, or
  waiting for the user's answer) and one of its steps worked there in the last AFFINITY_DAYS (`worked_in`), and a
  notification from that app reaches the chat when the conversation it is from (the person, group or order) is
  named in what those steps were about, or is one whose notifications the chat was already passed (the app's own
  id for the conversation). A chat whose task is over receives nothing more this way: it once read a group, and
  would otherwise collect everything the group says for weeks. What it did is read instead, by the briefing
  that the notification joins.
- A briefing (app/briefings): a notification no chat took, from an app the user chose to be briefed on,
  joins its conversation's briefing, which the user finds under "For you". One the user opened and has not
  answered yet takes what its conversation shows next.
Anything else stays in the log.

The worker's `watch` is told of each phone's new notifications by the gateway, which holds a read open on the
bridge module's listener (gateway/notices.py): nothing asks on a timer, no lease is needed, so it hears them
while a task uses the phone, and a sleeping phone is not woken for it.
"""

import asyncio
import contextlib
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone

from . import watches
from .briefings import groups
from .config import settings
from .delivery import IN_HAND, NOTICE, WORKING, latest_run
from .store import uid
from .watches import words

log = logging.getLogger(__name__)

# A chat keeps receiving an app's notifications this long after one of its steps last worked in the app.
AFFINITY_DAYS = 14
# The log keeps a notification this long.
KEEP_DAYS = 90
# A chat is passed at most this many notifications after its last own message (the user's or penrose's): a busy
# group must not bury a chat nobody is looking at. The rest stay in the log.
NOTE_CAP = 20
# Notifications from these packages are not kept: penrose's own, and the system's.
IGNORED_PREFIXES = ("app.relay.", "android", "com.android.systemui")
# Apps whose content look_up reads at the source (Gmail's own search): their notifications would only repeat it.
SEARCHED_AT_SOURCE = ("com.google.android.gm",)
# How a passed notification is kept in the chat: a role of its own, so no agent reads it as the user's words.
ROLE, SOURCE = "notification", "Notification"
# What a chat's steps in one app were about, the newest kept.
ABOUT_CHARS = 6000
# Profile keys: where each phone's listener was last read, and when the log began.
CURSORS, SINCE = "notification_cursors", "notifications_since"
# Past any event: a read from here returns none, with the listener's current number.
NOWHERE = 2**62
# One read of a phone's notifications is held at the gateway this long, waiting for the next one.
HOLD_SECONDS = 25
# A chat remembers this many of an app's conversations whose notifications it was passed (their ids).
THREADS_KEPT = 12
# What a notification's line under the app's name is when it is a website: a host name (Chrome shows the site).
SITE = re.compile(r"(?i)^(?:https?://)?((?:[a-z0-9-]+\.)+[a-z]{2,})(?::\d+)?/?$")
# The columns the log has gained since it began: (table, column, declaration).
ADDED = (
    ("notifications", "source", "TEXT DEFAULT ''"), ("notifications", "thread", "TEXT DEFAULT ''"),
    ("notifications", "conversation", "TEXT DEFAULT ''"), ("notifications", "sender", "TEXT DEFAULT ''"),
    ("notifications", "watch_id", "INTEGER"), ("chat_apps", "threads", "TEXT DEFAULT ''"),
)


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,slot TEXT,key TEXT,package TEXT,
            label TEXT,title TEXT,text TEXT,posted REAL,chat_id TEXT,message_id TEXT);
        CREATE INDEX IF NOT EXISTS notifications_key ON notifications(slot,key,id);
        CREATE INDEX IF NOT EXISTS notifications_posted ON notifications(posted);
        CREATE TABLE IF NOT EXISTS chat_apps(chat_id TEXT,package TEXT,about TEXT,used REAL,PRIMARY KEY(chat_id,package));
    """)
    for table, column, declaration in ADDED:
        if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    watches.schema(db)
    groups.schema(db)
    try:  # words by their stem, as the messages' index (store.index_messages); without FTS5 the search uses LIKE
        indexed = {row["name"] for row in db.execute("PRAGMA table_info(notifications_fts)")}
        if indexed and "source" not in indexed:  # the index from before a notification said which site it is from
            db.executescript("DROP TRIGGER IF EXISTS notifications_ai; DROP TRIGGER IF EXISTS notifications_ad; "
                             "DROP TABLE notifications_fts;")
            indexed = set()
        db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS notifications_fts USING fts5(label, source, title, text,
              content='notifications', content_rowid='id', tokenize='porter unicode61 remove_diacritics 2');
            CREATE TRIGGER IF NOT EXISTS notifications_ai AFTER INSERT ON notifications BEGIN
              INSERT INTO notifications_fts(rowid, label, source, title, text)
                VALUES (new.id, new.label, new.source, new.title, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS notifications_ad AFTER DELETE ON notifications BEGIN
              INSERT INTO notifications_fts(notifications_fts, rowid, label, source, title, text)
                VALUES ('delete', old.id, old.label, old.source, old.title, old.text);
            END;
        """)
        if not indexed:
            db.execute("INSERT INTO notifications_fts(notifications_fts) VALUES ('rebuild')")
    except sqlite3.OperationalError:
        pass


# --- which chat a notification belongs to -----------------------------------------------------------------


def worked_in(store, chat_id, package, about):
    """A step of the chat works in `package`: `about` are the texts that say what the step is for (its goal, its
    instructions, what it reported), where the people, groups and orders it deals with are named."""
    if not package:
        return
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT about FROM chat_apps WHERE chat_id=? AND package=?", (chat_id, package)).fetchone()
        known = row["about"] if row else ""
        for text in about:
            text = " ".join((text or "").split())
            if text and text not in known:
                known = f"{known}\n{text}"[-ABOUT_CHARS:]
        db.execute("INSERT INTO chat_apps(chat_id,package,about,used) VALUES (?,?,?,?) "
                   "ON CONFLICT(chat_id,package) DO UPDATE SET about=excluded.about,used=excluded.used",
                   (chat_id, package, known, time.time()))


def named(about, title, label=""):
    """Whether `about` names what the notification is from: its title as a whole phrase ("Jordan Lee", "Home"),
    without a count an app adds ("Jordan Lee (2 messages)"). Android titles a message in a named conversation
    "<conversation>: <sender>" ("Home: Dad"), so the part before the colon counts too: the conversation, never
    the sender alone (Dad writing in the Home group is not the chat with Dad). A title that is only the app's own
    name names nothing."""
    title = re.sub(r"\s*\(\d[^)]*\)\s*$", "", (title or "").strip())
    for name in dict.fromkeys((title, title.split(": ")[0].strip())):
        if len(name) < 3 or name.lower() == (label or "").lower():
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", about or ""):
            return True
    return False


def room(db, chat_id):
    """Whether the chat can be passed another notification: True, False when it has been passed NOTE_CAP since its
    last own message, None when the chat is gone."""
    own = db.execute("SELECT MAX(created) FROM messages WHERE chat_id=? AND role!=?", (chat_id, ROLE)).fetchone()[0]
    if own is None:
        return None
    passed = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND role=? AND created>?",
                        (chat_id, ROLE, own)).fetchone()[0]
    return passed < NOTE_CAP


def route(db, note, now):
    """The chat a notification is passed to for where it comes from, or None: the one whose steps worked in its
    app most recently and named the conversation it is from (the conversation's own title when the app gives one,
    else the notification's), or were already passed that conversation's notifications (its id in the app);
    unless that chat has been passed NOTE_CAP since its last own message. Only a chat whose task is still open
    (delivery.IN_HAND) counts: one that is over is read by briefings, and receives nothing."""
    fresh = db.execute(
        "SELECT chat_id,about,threads FROM chat_apps WHERE package=? AND used>? ORDER BY used DESC",
        (note["package"], now - AFFINITY_DAYS * 86400),
    ).fetchall()
    thread = note.get("thread") or ""
    for row in fresh:
        known = thread and thread in (row["threads"] or "").split("\n")
        if not known and not named(row["about"], note.get("conversation") or note["title"], note["label"]):
            continue
        fits = room(db, row["chat_id"])
        if fits is None:
            continue  # the chat was deleted
        latest = latest_run(db, row["chat_id"])
        if not latest or latest["status"] not in IN_HAND:
            continue  # its task is over
        if fits and thread and not known:
            threads = [thread, *(t for t in (row["threads"] or "").split("\n") if t)][:THREADS_KEPT]
            db.execute("UPDATE chat_apps SET threads=? WHERE chat_id=? AND package=?",
                       ("\n".join(threads), row["chat_id"], note["package"]))
        return row["chat_id"] if fits else None
    return None


def line(note):
    """A notification in one line, as the chat keeps it and the agents read it."""
    title, text = (note.get("title") or "").strip(), (note.get("text") or "").strip()
    said = f"{title}: {text}" if title and text else title or text
    app = f"{note['label']} ({note['source']})" if note.get("source") else note["label"]
    return f"{app} · {said}"


def deliver(db, chat_id, note, now):
    """Pass the notification to the chat: an entry in its conversation and, while its task works, a notice in the
    task's inbox (runner.hear). Returns (the message's id, the chat's latest run's id or None)."""
    message_id, content = uid(), line(note)
    db.execute("INSERT INTO messages(id,chat_id,role,content,created,source) VALUES (?,?,?,?,?,?)",
               (message_id, chat_id, ROLE, content, now, SOURCE))
    latest = latest_run(db, chat_id)
    if latest and latest["status"] in WORKING:
        db.execute("INSERT INTO inbox(run_id,chat_id,mode,content,provider,message_id,created) VALUES (?,?,?,?,?,?,?)",
                   (latest["id"], chat_id, NOTICE, content, None, message_id, now))
    return message_id, latest["id"] if latest else None


def site(event):
    """The website a notification is from, or '': a browser shows the site under its own name."""
    found = SITE.match((event.get("sub_text") or "").strip())
    return found[1].lower().removeprefix("www.") if found else ""


def keep(store, slot, event, now=None):
    """Log one notification the phone's listener reported and pass it to the chat that waits for it: the one with
    a watch its words match (app/watches.py), else the one whose open task worked in the conversation it is from,
    else the open briefing on that conversation. One no chat takes joins a briefing when the user is briefed on
    its app (`briefing_id`). Returns the kept row (with `chat_id` when it was passed on, and `watch`, the watch's row, when one matched
    that says what to do then), or None when it is not kept: an ongoing one, the system's or penrose's own, one
    with nothing to read, or the one already kept under its key said again."""
    if event.get("type") != "notification" or event.get("ongoing"):
        return None
    package = event.get("app") or ""
    title, text = (event.get("title") or "").strip(), (event.get("text") or "").strip()
    big = (event.get("big_text") or "").strip()
    if len(big) > len(text):  # the expanded text, when the app gives one: an email's subject and its first lines
        text = big
    if not package or package.startswith(IGNORED_PREFIXES) or not (title or text):
        return None
    read = now or time.time()
    # When the phone posted it, when the listener says (a notification read after the worker was down is not
    # dated to the reading), unless the phone's clock is off.
    posted = (event.get("when") or 0) / 1000
    now = now or (posted if read - 86400 < posted <= read else read)
    key = event.get("key") or ""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if key:
            last = db.execute("SELECT title,text FROM notifications WHERE slot=? AND key=? ORDER BY id DESC LIMIT 1",
                              (slot, key)).fetchone()
            if last and (last["title"], last["text"]) == (title, text):
                return None
        label = event.get("label") or ""
        if not label or label == package:  # the gateway names an app from the shade: one already gone has no name
            known = db.execute("SELECT label FROM notifications WHERE package=? AND label!=package ORDER BY id DESC LIMIT 1",
                               (package,)).fetchone()
            label = known["label"] if known else package
        note = {"slot": slot, "key": key, "package": package, "label": label, "title": title, "text": text,
                "posted": now, "chat_id": None, "message_id": None, "source": site(event),
                "thread": event.get("thread") or "", "conversation": (event.get("conversation") or "").strip(),
                "sender": (event.get("sender") or "").strip()}
        note["id"] = db.execute(
            "INSERT INTO notifications(slot,key,package,label,title,text,posted,source,thread,conversation,sender) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (slot, key, package, label, title, text, now, note["source"], note["thread"], note["conversation"],
             note["sender"]),
        ).lastrowid
        run_id = None
        chat_id = None
        if watch := watches.matching(db, note, read):
            # One that may wake its chat is always passed on (until it has matched too much to be what it waits
            # for); one that only passes on gives way once nobody has looked at the chat for NOTE_CAP of them.
            waits = bool(watch["todo"])
            if watches.counted(db, watch) and (waits or room(db, watch["chat_id"])):
                chat_id = watch["chat_id"]
                note["watch_id"] = watch["id"]
                if waits:
                    note["watch"] = dict(watch)
        chat_id = chat_id or route(db, note, read)
        if not chat_id and (opened := groups.claimed(db, note)) and room(db, opened):
            chat_id = opened  # the user opened a briefing on this conversation and has not answered it yet
        if chat_id:
            note["chat_id"] = chat_id
            note["delivered"] = read
            note["message_id"], run_id = deliver(db, chat_id, note, read)
            db.execute("UPDATE notifications SET chat_id=?,message_id=?,watch_id=? WHERE id=?",
                       (chat_id, note["message_id"], note.get("watch_id"), note["id"]))
        elif briefing_id := groups.gather(db, note, read):
            note["briefing_id"] = briefing_id
    if run_id:  # the window follows the chat's latest run: this is how it learns the chat has a new entry
        store.event(run_id, "notification", {"message_id": note["message_id"], "app": label})
    elif note.get("briefing_id"):  # the "For you" list shows it: its screens read the list again
        store.changed()
    return note


# --- the log as look_up reads it ---------------------------------------------------------------------------


def began(store):
    """When the log's oldest possible entry is from: the day penrose began keeping notifications, or KEEP_DAYS
    ago. None when it has not begun."""
    since = store.profile_get(SINCE)
    return max(since, time.time() - KEEP_DAYS * 86400) if since else None


def search(store, query, limit=6, exclude_chat=None):
    """The kept notifications that have every word of `query`, newest first, as look_up's lines: one per
    notification, then a line saying how many there are and since when the log reaches. '' when none match.
    Left out: the apps look_up reads at the source, and what was passed to `exclude_chat` (its agents read
    that in the conversation)."""
    found, since = words(query), began(store)
    if not found or not since:
        return ""
    where = (f"n.package NOT IN ({','.join('?' for _ in SEARCHED_AT_SOURCE)}) AND (n.chat_id IS NULL OR n.chat_id!=?) "
             "AND n.posted>=?")
    args = (*SEARCHED_AT_SOURCE, exclude_chat or "", since)
    try:
        match = " AND ".join(f'"{w}"' for w in found)
        source = f"notifications_fts f JOIN notifications n ON n.id=f.rowid WHERE notifications_fts MATCH ? AND {where}"
        total = store.rows(f"SELECT COUNT(*) AS n FROM {source}", (match, *args))[0]["n"]
        rows = store.rows(f"SELECT n.* FROM {source} ORDER BY n.posted DESC LIMIT ?", (match, *args, limit)) if total else []
    except sqlite3.OperationalError:  # no FTS5: plain substring matches
        every = " AND ".join("(n.label || ' ' || n.source || ' ' || n.title || ' ' || n.text) LIKE ?" for _ in found)
        likes = tuple(f"%{w}%" for w in found)
        total = store.rows(f"SELECT COUNT(*) AS n FROM notifications n WHERE {every} AND {where}", (*likes, *args))[0]["n"]
        rows = store.rows(f"SELECT n.* FROM notifications n WHERE {every} AND {where} ORDER BY n.posted DESC LIMIT ?",
                          (*likes, *args, limit)) if total else []
    if not rows:
        return ""
    zone = store.zone() or timezone.utc
    lines = []
    for row in rows:
        when = datetime.fromtimestamp(row["posted"], zone).strftime("%a %d %b %Y, %H:%M")
        said = row["text"] if len(row["text"]) <= 160 else row["text"][:160] + "…"
        app = f"{row['label']} ({row['source']})" if row["source"] else row["label"]
        lines.append(" · ".join(part for part in (f"Notification {app}", row["title"], when, said) if part))
    count = f"{total}, the newest {len(rows)} shown" if total > len(rows) else str(total)
    lines.append(f'Notifications "{query}" · {count} · kept since {datetime.fromtimestamp(since, zone).strftime("%d %b %Y")} '
                 "only; each is the start of a message that is in its app")
    return "\n".join(lines)


def prune(store):
    """Drop the notifications and briefings older than KEEP_DAYS, the apps of chats that are gone or that old,
    and the watches that ended that long ago or whose chat is gone."""
    now = time.time()
    with store.connect() as db:
        db.execute("DELETE FROM notifications WHERE posted<?", (now - KEEP_DAYS * 86400,))
        # A chat's apps outlive its claim on their notifications (AFFINITY_DAYS, in route): briefings read them.
        db.execute("DELETE FROM chat_apps WHERE used<? OR chat_id NOT IN (SELECT id FROM chats)", (now - KEEP_DAYS * 86400,))
        db.execute("DELETE FROM briefings WHERE latest<?", (now - KEEP_DAYS * 86400,))
        watches.open_rows(db, now=now)  # closes the ones past their date
        db.execute("DELETE FROM watches WHERE ended<? OR chat_id NOT IN (SELECT id FROM chats)", (now - KEEP_DAYS * 86400,))


# --- reading the phone ---------------------------------------------------------------------------------------


class Intake:
    """Keeps each phone's new notifications as the gateway tells of them. Where each phone's listener was last
    read is saved, so a restart of the worker misses nothing the listener still holds; the first read of a phone
    only marks where its news starts, so what was in its shade before penrose began keeping notifications is
    never kept. `runner` and `ring` are for the watches that wake a chat (watches.wake): the model call that
    checks one, and telling the worker its task is queued."""

    def __init__(self, store, devices, runner=None, ring=None, on_briefing=None):
        self.store, self.devices, self.runner, self.ring = store, devices, runner, ring
        self.seen = dict(store.profile_get(CURSORS, {}) or {})  # slot -> the last event read
        self.pruned = 0.0
        self.waking = set()  # the checks of watches under way
        self.on_briefing = on_briefing  # called when a notification joined a briefing (briefings/triage.py Sorter.poke)

    async def follow(self, slot):
        """One phone, for good: each read is held at the gateway until the phone has a new notification."""
        while True:
            if time.monotonic() - self.pruned > 3600:
                self.pruned = time.monotonic()
                with contextlib.suppress(Exception):
                    prune(self.store)
            asked = time.monotonic()
            try:
                heard = await self.read(slot, wait=HOLD_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                heard = False
                log.debug("notification intake: %s", exc)
            if not heard and time.monotonic() - asked < 1:
                await asyncio.sleep(2)  # the gateway is down or restarting: it could not hold the read

    async def read(self, slot, wait=0):
        """Keep what the phone's listener has after the last event read; with `wait`, the gateway answers when
        there is something, or after that many seconds. False when the gateway did not answer."""
        known = self.seen.get(slot)
        found = await self.devices.notification_events(slot, known if known is not None else NOWHERE, wait=wait)
        if found is None:
            return False
        if found.get("seq") is None:
            return True  # the phone is not up: nothing to read yet
        seq, events = found.get("seq", 0), found.get("events", [])
        if known is None:
            events = []
            if not self.store.profile_get(SINCE):
                self.store.profile_set(SINCE, time.time())
        elif seq < known:  # the listener restarted (the phone did, or its module was updated): what it has is all new
            found = await self.devices.notification_events(slot, 0) or {}
            events, seq = found.get("events", []), found.get("seq", 0)
        for event in events:
            try:
                note = keep(self.store, slot, event)
            except Exception:
                log.exception("could not keep a notification from %s", event.get("app"))
                continue
            if note and note.get("briefing_id") and self.on_briefing:
                self.on_briefing()
            if note and note.get("watch") and self.runner:
                task = asyncio.create_task(self.wake(note))  # beside the intake: its check is a model call
                self.waking.add(task)
                task.add_done_callback(self.waking.discard)
        if seq != known:
            self.seen[slot] = seq
            self.store.profile_set(CURSORS, self.seen)
        return True

    async def wake(self, note):
        try:
            await watches.wake(self.runner, note["watch"], note, ring=self.ring)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("could not check watch %s", note["watch"].get("id"))


async def watch(store, devices, runner=None, ring=None, on_briefing=None):
    """The worker's notification intake, one task per phone: never fails the worker. Off with
    notification_seconds 0."""
    if settings.notification_seconds <= 0:
        return
    intake = Intake(store, devices, runner, ring, on_briefing)
    while not (slots := [phone["id"] for phone in await devices.phones()]):
        await asyncio.sleep(2)  # the gateway is starting: its phones are the same for as long as it runs
    await asyncio.gather(*(intake.follow(slot) for slot in slots))
