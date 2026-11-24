"""Which briefing a notification joins, and a briefing's notifications as the agents read them.

The notifications of one conversation of one app are one briefing while they keep coming (a group chat sends many
in a minute), so each burst is looked at once, not each message. After a longer silence the conversation begins a
new briefing, and the old one gives way to it unless it was worth a briefing the user has yet to open.
"""

from datetime import datetime, timezone

from ..delivery import IN_HAND, latest_run
from . import policy

# A notification joins its conversation's briefing while the last one came within this long.
JOIN_SECONDS = 2 * 3600
# How far back the list reaches: what an app showed longer ago is neither taken in nor sorted.
RECENT_SECONDS = 24 * 3600
# The most characters of a conversation's key (an app's own id for one can be long).
KEY_CHARS = 200
# The most notifications of an app looked through for what a conversation showed before (`earlier`).
LOOKED_THROUGH = 400
# What the sort said of a briefing (tools/triage.py).
IGNORE, LIST, BRIEF = "ignore", "list", "brief"


def schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS briefings(id INTEGER PRIMARY KEY AUTOINCREMENT,slot TEXT,package TEXT,label TEXT,
            source TEXT,key TEXT,name TEXT,began REAL,latest REAL,count INTEGER DEFAULT 0,pending REAL,
            sorted REAL,sorted_count INTEGER DEFAULT 0,verdict TEXT,headline TEXT,why TEXT,search TEXT,
            urgent INTEGER DEFAULT 0,chat_id TEXT,seen REAL,dismissed REAL,asks TEXT,gist TEXT);
        CREATE INDEX IF NOT EXISTS briefings_conversation ON briefings(package,key,id);
        CREATE INDEX IF NOT EXISTS briefings_chat ON briefings(chat_id);
        -- The chats waiting for the user that they took off the list, and when (feed.waiting).
        CREATE TABLE IF NOT EXISTS for_you_hidden(chat_id TEXT PRIMARY KEY, at REAL);
    """)
    # What the sort says is asked of the user, and the things said that matter (a JSON list): kept since 2026-11-12.
    known = {row["name"] for row in db.execute("PRAGMA table_info(briefings)")}
    for column in ("asks", "gist"):
        if column not in known:
            db.execute(f"ALTER TABLE briefings ADD COLUMN {column} TEXT")
    if "briefing_id" not in {row["name"] for row in db.execute("PRAGMA table_info(notifications)")}:
        db.execute("ALTER TABLE notifications ADD COLUMN briefing_id INTEGER")
    db.execute("CREATE INDEX IF NOT EXISTS notifications_briefing ON notifications(briefing_id,id)")
    db.execute("CREATE INDEX IF NOT EXISTS notifications_package ON notifications(package,posted)")


def find(store, briefing_id):
    """The briefing's row, or None."""
    rows = store.rows("SELECT * FROM briefings WHERE id=?", (briefing_id,))
    return rows[0] if rows else None


# --- grouping ---------------------------------------------------------------------------------------------------


def key_of(note):
    """What a notification's conversation is known by within its app: its name (a person, a group), else the
    app's own id for it, else the website it is from (a browser's); '' when there is none of these (a store's
    alerts), and the app as a whole is the group."""
    return (note.get("conversation") or note.get("thread") or note.get("source") or "").strip().lower()[:KEY_CHARS]


def name_of(note):
    """The conversation as the user reads it, or '' when only the app is known."""
    return (note.get("conversation") or (note.get("title") if note.get("thread") else "") or "").strip()


def claimed(db, note):
    """The chat of a briefing on this conversation that the user opened and whose task is still in hand (working,
    or waiting for their answer), or None: what the conversation shows next belongs with what they are being
    asked about it. Only a named conversation is claimed, never an app as a whole, and not one the user
    dismissed or muted since."""
    key = key_of(note)
    if not key:
        return None
    row = db.execute(
        "SELECT chat_id FROM briefings WHERE package=? AND key=? AND chat_id IS NOT NULL AND dismissed IS NULL "
        "ORDER BY id DESC LIMIT 1", (note["package"], key)).fetchone()
    if not row or policy.muted(policy.read(db), note["package"], key):
        return None
    latest = latest_run(db, row["chat_id"])
    return row["chat_id"] if latest and latest["status"] in IN_HAND else None


def gather(db, note, now):
    """Add a notification no chat took to its conversation's briefing, when the user is briefed on its app and
    has not muted the conversation. `note` is its row in the log; `now` is when it came. Returns the briefing's
    id, or None."""
    package, key = note["package"], key_of(note)
    if not policy.briefs_on(policy.read(db), package, key):
        return None
    row = db.execute(
        "SELECT id,latest,verdict FROM briefings WHERE package=? AND key=? AND dismissed IS NULL AND chat_id IS NULL ORDER BY id DESC LIMIT 1",
        (package, key)).fetchone()
    if row and row["latest"] > now - JOIN_SECONDS:
        db.execute("UPDATE briefings SET latest=?,count=count+1,pending=COALESCE(pending,?),label=? WHERE id=?",
                   (now, now, note["label"], row["id"]))
        briefing_id = row["id"]
    else:
        if row and row["verdict"] != BRIEF:
            # An older one that was only listed: the new one stands for the conversation now. One worth a briefing
            # that the user has yet to open stays beside it (feed.READY_SECONDS).
            db.execute("UPDATE briefings SET dismissed=? WHERE id=?", (now, row["id"]))
        briefing_id = db.execute(
            "INSERT INTO briefings(slot,package,label,source,key,name,began,latest,count,pending) VALUES (?,?,?,?,?,?,?,?,1,?)",
            (note["slot"], package, note["label"], note.get("source") or "", key, name_of(note), now, now, now)).lastrowid
    db.execute("UPDATE notifications SET briefing_id=? WHERE id=?", (briefing_id, note["id"]))
    return briefing_id


def take_in(db, package, now):
    """The user just chose this app: what it showed over the last day that no chat took joins briefings, each
    as of when it came, so the list starts from today and not from the next notification. Returns how many."""
    rows = db.execute(
        "SELECT * FROM notifications WHERE package=? AND posted>? AND chat_id IS NULL AND briefing_id IS NULL ORDER BY posted,id",
        (package, now - RECENT_SECONDS)).fetchall()
    return sum(1 for row in rows if gather(db, dict(row), row["posted"]))


def drop(db, package, now):
    """The user no longer wants this app: what is listed from it and was never opened goes with the choice."""
    db.execute("UPDATE briefings SET dismissed=? WHERE package=? AND dismissed IS NULL AND chat_id IS NULL", (now, package))


# --- a briefing's notifications, as the agents read them --------------------------------------------------------


def notes_of(store, briefing, limit):
    """The briefing's own notifications (their rows in the log), oldest first, the newest `limit`."""
    return list(reversed(store.rows("SELECT * FROM notifications WHERE briefing_id=? ORDER BY id DESC LIMIT ?",
                                    (briefing["id"], limit))))


def earlier(store, briefing, limit):
    """What the same conversation of the same app showed before this briefing, oldest first: the log is a
    record of it, as far back as it reaches."""
    rows = store.rows(
        "SELECT * FROM notifications WHERE package=? AND posted<? AND (briefing_id IS NULL OR briefing_id!=?) "
        "ORDER BY posted DESC LIMIT ?", (briefing["package"], briefing["began"], briefing["id"], LOOKED_THROUGH))
    # The same key as grouping, worked out here: SQLite's lower() knows only ASCII, and a name need not be.
    return list(reversed([row for row in rows if key_of(row) == briefing["key"]][:limit]))


def stamp(store, timestamp):
    """A time as the agents read it: in the user's time zone when it is known."""
    zone = store.zone()
    return datetime.fromtimestamp(timestamp, zone or timezone.utc).strftime("%a %d %b %Y, %H:%M") + ("" if zone else " UTC")


def as_read(store, note, chars):
    """One notification as an agent reads it: who or what it is from, the start of what it said, and when."""
    text = note["text"] if len(note["text"]) <= chars else note["text"][:chars] + "…"
    return {"from": note["sender"] or note["title"], "text": text, "at": stamp(store, note["posted"])}


def origin(briefing):
    """Where a briefing is from, for an agent: the app, its website, the conversation."""
    return {k: v for k, v in (("app", briefing["label"]), ("site", briefing["source"]),
                              ("conversation", briefing["name"])) if v}
