import json
import math
import re
import sqlite3
import threading
import time
import uuid
import weakref
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from . import attachments
from .config import settings
from .plan import ACTIVE, node_views
from .pricing import add_up, tokens

# Chat messages that speak to the user: the gate and the orchestrator (and, in chats saved before
# the orchestrator, the final response). NULL is a runtime notice. Everything else stays in the
# activity panel and never re-enters a model's conversation.
PUBLIC_SOURCES = ("Fast response", "Orchestrator", "Final response")
# A notification passed to the chat (app/notifications.py): part of the conversation, in a role of its own.
NOTIFICATION = "notification"
# The most messages an agent reads word for word; compaction normally keeps a chat far below it.
CONVERSATION_CAP = 1000
# How much of a query's word weight an earlier conversation must match to be found (Store.covering): the idea of
# Lucene's minimum_should_match, weighed by rarity.
COVERAGE = 0.5


def public(prefix=""):
    """SQL: the message is part of the conversation as the agents read it (the user's, a public reply, or a
    notification passed to the chat). Bind PUBLIC_SOURCES after the parameters before it."""
    marks = ",".join("?" for _ in PUBLIC_SOURCES)
    return f"({prefix}role IN ('user','{NOTIFICATION}') OR {prefix}source IS NULL OR {prefix}source IN ({marks}))"


PUBLIC = public()
# A user message this long after the one before starts a new request in the conversation as the agents read it
# (Store.conversation_context), as does one that opened a new task: what was seen before then may have changed.
REQUEST_GAP = 10 * 60


def request_break(when, gap, zone=None):
    """The line the agents read where the user came back to the chat: when, and how long after the message before.
    Both are fixed once written, so the conversation's prompt prefix stays the same from call to call."""
    local = datetime.fromtimestamp(when, zone or timezone.utc)
    minutes = int(gap // 60)
    if minutes < 2:
        since = "moments"
    elif minutes < 120:
        since = f"{minutes} minutes"
    elif minutes < 48 * 60:
        since = f"{minutes // 60} hours"
    else:
        since = f"{minutes // (24 * 60)} days"
    stamp = local.strftime("%A %d %B %Y, %H:%M") + ("" if zone else " UTC")
    return f"New request on {stamp}, {since} after the message before."
# A stopped run is paused, not over: the next user message resumes it through the orchestrator.
HALTED = ("cancelled", "stopped")
# In SQL over `runs r`: whether the run is one penrose began on its own (briefings/prepare.py), 1 when it is.
BACKGROUND = "json_extract(r.state,'$.background')"


def uid():
    return uuid.uuid4().hex


def excerpt(text, words, width=220):
    """The part of `text` around the first of `words` (or of its stem: "booked" finds "booking"), trimmed to
    `width` characters."""
    lower = text.lower()
    stems = [form for w in words for form in (w.lower(), w.lower()[:max(4, len(w) - 3)])]
    hit = min((i for i in (lower.find(w) for w in stems) if i >= 0), default=0)
    start = max(0, hit - width // 3)
    piece = text[start:start + width].strip()
    return ("…" if start else "") + piece + ("…" if start + width < len(text) else "")


# --- a run's saved state ---------------------------------------------------------------------------------
# A run is saved in two parts: its state without the steps' turns (`runs.state`), and the turns, one row each
# (`turns`). The turns are most of a long run and only ever grow, so a save writes the new ones and the last
# few (the only ones that still change: an action's turn gets its result when it ends) instead of the whole
# history. `nodes` is a view of the plan (plan.node_views) and is made when a state is read, never saved.

# Turns this close to the end of a step may still change; older ones are final.
TURN_TAIL = 8


def split(state):
    """(the state to keep in `runs.state`, {step id: its turns}). A state without a plan (saved before there was
    one) is kept whole."""
    if "plan" not in state:
        return state, {}
    head = {k: v for k, v in state.items() if k not in ("plan", "nodes")}
    head["plan"], turns = [], {}
    for step in state["plan"]:
        turns[step["id"]] = step.get("turns", [])
        head["plan"].append({k: v for k, v in step.items() if k != "turns"})
    return head, turns


def assemble(db, run_id, text):
    """A run's whole state from its saved head (`runs.state`) and its turns, with the view the window draws."""
    state = json.loads(text)
    if "plan" not in state:
        return state
    if any("turns" not in step for step in state["plan"]):
        saved = defaultdict(list)
        for row in db.execute("SELECT step_id,turn FROM turns WHERE run_id=? ORDER BY step_id,idx", (run_id,)):
            saved[row[0]].append(json.loads(row[1]))
        for step in state["plan"]:
            step.setdefault("turns", saved.get(step["id"], []))
    state["nodes"] = node_views(state)
    return state


def read_state(db, run_id):
    """The run's whole saved state, or None."""
    row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
    return assemble(db, run_id, row[0]) if row else None


def run_states(db, chat_id, order="updated"):
    """The chat's runs, whole, in `order` (a runs column)."""
    rows = db.execute(f"SELECT id,state FROM runs WHERE chat_id=? ORDER BY {order}", (chat_id,)).fetchall()
    return [assemble(db, row[0], row[1]) for row in rows]


# A task that ends forgets the private entries the user gave its chat (Store.add_private).
ENDED = ("completed", "failed", "cancelled")


def forget_private(db, state):
    if state.get("status") in ENDED and state.get("chat_id"):
        db.execute("DELETE FROM private_entries WHERE chat_id=?", (state["chat_id"],))


def put_state(db, state, insert=False):
    """Replace the run's saved state and every turn (a new run when `insert`). For the rare whole-state writes:
    a reply, a stop, a rewind; the worker's saves go through Store.save."""
    head, turns = split(state)
    forget_private(db, state)
    if insert:
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?)",
                   (state["id"], state["chat_id"], state["status"], json.dumps(head), time.time()))
    else:
        db.execute("UPDATE runs SET status=?,state=?,updated=? WHERE id=?",
                   (state["status"], json.dumps(head), time.time(), state["id"]))
    db.execute("DELETE FROM turns WHERE run_id=?", (state["id"],))
    db.executemany("INSERT INTO turns VALUES (?,?,?,?)", [
        (state["id"], step_id, i, json.dumps(turn)) for step_id, items in turns.items() for i, turn in enumerate(items)
    ])


def dump_state(state):
    """The whole state as one JSON text, for a checkpoint."""
    return json.dumps({k: v for k, v in state.items() if k != "nodes"})


def halt(state):
    """Mark the steps that were working as stopped. An action that was started but never reported back
    may have happened, so it becomes uncertain and is never replayed blindly."""
    for step in state.get("plan", []):
        if step["status"] in ACTIVE:
            step["status"] = "stopped"
        for turn in step.get("turns", []):
            if turn.get("action") and turn.get("status") == "started":
                turn["status"] = "uncertain"
    state["nodes"] = node_views(state)


class Connection(sqlite3.Connection):
    """A connection kept for the next use on its thread (Store.connect). Its cursors are closed after each use: an
    unfinished query would otherwise hold its snapshot, and the next use would read the database as it was."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cursors = weakref.WeakSet()

    def cursor(self, *args, **kwargs):
        cursor = super().cursor(*args, **kwargs)
        self.cursors.add(cursor)
        return cursor

    def execute(self, sql, parameters=()):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, script):
        return self.cursor().executescript(script)

    def finish(self):
        for cursor in list(self.cursors):
            cursor.close()
        if self.in_transaction:
            self.rollback()


def upgraded(total):
    """A chat's aggregate usage as it is kept now: chats priced before OpenAI and Claude were (DeepSeek only)
    summed the usage JSON without naming the provider."""
    usage = total.setdefault("usage", {})
    if usage and not set(usage) <= {"deepseek", "openai", "anthropic"}:
        total["usage"] = {"deepseek": usage}
    return total


class Store:
    def __init__(self, directory=None):
        self.directory = directory or settings.user_dir
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "agent.sqlite3"
        # What this process last wrote of each run's turns: {run id: {step id: [the turn's JSON, or None once it
        # is older than TURN_TAIL]}}, so a save skips what is already there. Forgotten when the run is read.
        self.turns_written = {}
        # One idle connection per thread, reused: opening one costs milliseconds (and on Windows, a file scan).
        self.idle = threading.local()
        # Told the run id after new events are committed: the doorbell (app/bell.py) that wakes the event streams.
        self.on_event = None
        with self.connect() as db:
            # Memories became named, typed notes; the old flat sentences are not carried over.
            if "name" not in {row["name"] for row in db.execute("PRAGMA table_info(memories)")}:
                db.execute("DROP TABLE IF EXISTS memories")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,title TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,chat_id TEXT,status TEXT,state TEXT,updated REAL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,kind TEXT,payload TEXT,created REAL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS ui_calls(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,node_id TEXT,created REAL,seconds REAL,request TEXT,response TEXT,error TEXT);
                CREATE INDEX IF NOT EXISTS ui_calls_run ON ui_calls(run_id,id);
                CREATE TABLE IF NOT EXISTS images(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,node_id TEXT,created REAL,jpeg BLOB);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,run_id TEXT,status TEXT,owner TEXT,heartbeat REAL);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(run_id) WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,name TEXT UNIQUE,type TEXT,description TEXT,content TEXT,app TEXT,source TEXT,chat_id TEXT,created REAL,updated REAL,expires TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS memory_scans(chat_id TEXT PRIMARY KEY,scanned REAL);
                CREATE TABLE IF NOT EXISTS chat_summaries(chat_id TEXT PRIMARY KEY,summary TEXT,covers_until REAL,folded INTEGER,updated REAL);
                CREATE TABLE IF NOT EXISTS leases(slot TEXT PRIMARY KEY,owner TEXT UNIQUE,created REAL);
                CREATE TABLE IF NOT EXISTS secrets(run_id TEXT,node_id TEXT,value TEXT,PRIMARY KEY(run_id,node_id));
                CREATE TABLE IF NOT EXISTS private_entries(chat_id TEXT,n INTEGER,label TEXT,value TEXT,created REAL,PRIMARY KEY(chat_id,n));
                CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY,heartbeat REAL);
                CREATE TABLE IF NOT EXISTS checkpoints(message_id TEXT PRIMARY KEY,run_id TEXT,state TEXT,created REAL);
                CREATE INDEX IF NOT EXISTS checkpoints_run ON checkpoints(run_id);
                CREATE TABLE IF NOT EXISTS profile(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,chat_id TEXT,mode TEXT,content TEXT,provider TEXT,message_id TEXT,created REAL);
                CREATE INDEX IF NOT EXISTS inbox_run ON inbox(run_id,id);
                CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,run_id TEXT,call_id TEXT,stage TEXT,provider TEXT,model TEXT,created REAL,peak INTEGER,credits REAL,usage TEXT);
                CREATE INDEX IF NOT EXISTS usage_chat ON usage(chat_id,id);
                CREATE TABLE IF NOT EXISTS chat_usage(chat_id TEXT PRIMARY KEY,usage TEXT);
                CREATE TABLE IF NOT EXISTS turns(run_id TEXT,step_id TEXT,idx INTEGER,turn TEXT,PRIMARY KEY(run_id,step_id,idx));
                CREATE TABLE IF NOT EXISTS attachments(id INTEGER PRIMARY KEY AUTOINCREMENT,chat_id TEXT,name TEXT,mime TEXT,kind TEXT,size INTEGER,purpose TEXT,path TEXT,phone_path TEXT,w INTEGER DEFAULT 0,h INTEGER DEFAULT 0,text TEXT,note TEXT,created REAL,done INTEGER DEFAULT 0,sent INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS attachments_chat ON attachments(chat_id);
                CREATE TABLE IF NOT EXISTS desk_log(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,payload TEXT,created REAL,announced INTEGER DEFAULT 1);
                CREATE INDEX IF NOT EXISTS images_run ON images(run_id);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS runs_chat ON runs(chat_id,updated,status);
                CREATE INDEX IF NOT EXISTS messages_chat ON messages(chat_id,created);
            """)
            db.execute("BEGIN IMMEDIATE")
            if "source" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN source TEXT")
            # The reasoning behind an orchestrator or gate reply, shown collapsed above it.
            if "thinking" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN thinking TEXT")
            # What Relay says aloud for a reply when the user is on voice (api/voice.py).
            if "spoken" not in {row["name"] for row in db.execute("PRAGMA table_info(messages)")}:
                db.execute("ALTER TABLE messages ADD COLUMN spoken TEXT")
            # The files a message carries (attachments table), as a JSON list of ids; None for most messages.
            for table in ("messages", "inbox"):
                if "attachments" not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN attachments TEXT")
            # A file is `sent` once a message carries it (attachments.py); tables made before it lack the column.
            if "sent" not in {row["name"] for row in db.execute("PRAGMA table_info(attachments)")}:
                db.execute("ALTER TABLE attachments ADD COLUMN sent INTEGER DEFAULT 0")
            # The agent and subagent models a waiting message was sent with (agents/catalog.py).
            if "models" not in {row["name"] for row in db.execute("PRAGMA table_info(inbox)")}:
                db.execute("ALTER TABLE inbox ADD COLUMN models TEXT")
            # A memory with a validity (a trip, a stay) stops being recalled after its date.
            if "expires" not in {row["name"] for row in db.execute("PRAGMA table_info(memories)")}:
                db.execute("ALTER TABLE memories ADD COLUMN expires TEXT DEFAULT ''")
            # The package IDs a lease is limited to (gateway/scope.py); NULL: any app.
            if "apps" not in {row["name"] for row in db.execute("PRAGMA table_info(leases)")}:
                db.execute("ALTER TABLE leases ADD COLUMN apps TEXT")
            # Pictures a phone agent clipped for the user's answer, beside the screenshots models were shown.
            if "kind" not in {row["name"] for row in db.execute("PRAGMA table_info(images)")}:
                db.execute("ALTER TABLE images ADD COLUMN kind TEXT DEFAULT 'shown'")
                db.execute("ALTER TABLE images ADD COLUMN caption TEXT DEFAULT ''")
                db.execute("ALTER TABLE images ADD COLUMN place TEXT DEFAULT ''")
                db.execute("ALTER TABLE images ADD COLUMN w INTEGER DEFAULT 0")
                db.execute("ALTER TABLE images ADD COLUMN h INTEGER DEFAULT 0")
            db.commit()
            self.index_messages(db)
            from .notifications import schema as notification_schema  # the notification log, and the chats' apps

            notification_schema(db)

    @staticmethod
    def index_messages(db):
        """A full-text index of the messages, kept by triggers, so earlier conversations can be searched
        (`search_history`). Words are indexed by their stem (porter), so "booked" finds "booking" and "hotels"
        finds "hotel". Messages from before the index, or an index from before stems, are indexed once. Without
        FTS5 there is no index and the search falls back to LIKE."""
        try:
            old = db.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts'").fetchone()
            fresh = not old or "porter" not in (old[0] or "")
            if old and fresh:
                db.execute("DROP TABLE messages_fts")
            db.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content, content='messages', content_rowid='rowid',
                  tokenize='porter unicode61 remove_diacritics 2');
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                  INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
                END;
            """)
            if fresh and db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
                db.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
        except sqlite3.OperationalError:
            pass

    @contextmanager
    def connect(self):
        """A connection for one transaction: this thread's idle one, or a new one when it is in use (a nested
        connect). Committed at the end, rolled back on an error."""
        db, self.idle.db = getattr(self.idle, "db", None), None
        if db is None:
            db = sqlite3.connect(self.path, timeout=15, factory=Connection)
            db.row_factory = sqlite3.Row
            # With WAL, NORMAL syncs at checkpoints rather than at every commit: an app crash loses nothing, a power
            # cut at most the last writes.
            db.execute("PRAGMA synchronous=NORMAL")
        try:
            with db:
                yield db
        finally:
            db.finish()
            if getattr(self.idle, "db", None) is None:
                self.idle.db = db
            else:
                db.close()

    def close(self):
        """Close this thread's idle connection."""
        if db := getattr(self.idle, "db", None):
            self.idle.db = None
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as db:
            db.execute(sql, args)

    def message(self, chat_id, role, content, source=None, message_id=None, thinking=None, spoken=None, attachments=None):
        """`attachments`: the ids of the chat's files the message carries (app/attachments.py), e.g. a call's
        recording; the message comes back with them as the window reads them."""
        message = {
            "id": message_id or uid(),
            "role": role,
            "content": content,
            "created": time.time(),
            "source": source,
            "thinking": thinking or None,
            "spoken": spoken or None,
        }
        self.execute(
            "INSERT INTO messages(id,chat_id,role,content,created,source,thinking,spoken,attachments) VALUES (?,?,?,?,?,?,?,?,?)",
            (message["id"], chat_id, role, content, message["created"], source, message["thinking"], message["spoken"],
             json.dumps(list(attachments)) if attachments else None),
        )
        if attachments:
            from . import attachments as files

            message["attachments"] = files.views(self, list(attachments))
        return message

    def conversation(self, chat_id, limit=30, before=None):
        """The user's messages and Relay's public replies, oldest first."""
        return self.rows(
            f"SELECT role,content FROM (SELECT role,content,created FROM messages WHERE chat_id=? AND created<=? AND {PUBLIC} "
            "ORDER BY created DESC LIMIT ?) ORDER BY created",
            (chat_id, before if before is not None else float("inf"), *PUBLIC_SOURCES, limit),
        )

    def last_reply(self, chat_id):
        """Relay's latest public reply in the chat ({content, created}), or None."""
        rows = self.rows(
            f"SELECT content,created FROM messages WHERE chat_id=? AND role='assistant' AND {PUBLIC} ORDER BY created DESC LIMIT 1",
            (chat_id, *PUBLIC_SOURCES),
        )
        return rows[0] if rows else None

    def public_messages(self, chat_id, after=0.0):
        """The user's messages and Relay's public replies written after `after`, oldest first, with their times."""
        return self.rows(
            f"SELECT role,content,created,attachments FROM messages WHERE chat_id=? AND created>? AND {PUBLIC} ORDER BY created",
            (chat_id, after, *PUBLIC_SOURCES),
        )

    # --- the conversation as the agents read it: a summary of its older part, the rest word for word ----

    def summary(self, chat_id):
        """The chat's summary row ({summary, covers_until, folded, updated}), or None. `covers_until` is the
        time of the last message it stands in for (runtime/compaction.py writes it)."""
        rows = self.rows("SELECT * FROM chat_summaries WHERE chat_id=?", (chat_id,))
        return rows[0] if rows else None

    def save_summary(self, chat_id, summary, covers_until, folded):
        self.execute("INSERT OR REPLACE INTO chat_summaries VALUES (?,?,?,?,?)", (chat_id, summary, covers_until, folded, time.time()))

    def conversation_context(self, chat_id):
        """What an agent that talks to the user reads of the chat: `conversation`, the messages since the summary
        (every one, word for word), and `conversation_summary` when older messages were folded into one. The
        messages are capped so a chat whose compaction keeps failing still fits a prompt. A notification passed
        to the chat is an entry of its own role, with when it came (`at`)."""
        summary = self.summary(chat_id)
        messages = self.public_messages(chat_id, summary["covers_until"] if summary else 0.0)[-CONVERSATION_CAP:]
        previous = summary["covers_until"] if summary else None
        if messages and previous is None:
            earlier = self.rows(
                f"SELECT MAX(created) AS t FROM messages WHERE chat_id=? AND created<? AND {PUBLIC}",
                (chat_id, messages[0]["created"], *PUBLIC_SOURCES),
            )
            previous = earlier[0]["t"] if earlier else None
        starts = {
            row["t"] for row in self.rows("SELECT json_extract(state,'$.created') AS t FROM runs WHERE chat_id=?", (chat_id,))
            if row["t"] is not None
        }
        zone, conversation = self.zone(), []
        for m in messages:
            if m["role"] == "user" and previous is not None and (
                m["created"] - previous >= REQUEST_GAP or any(previous < t <= m["created"] for t in starts)
            ):
                conversation.append({"role": "break", "content": request_break(m["created"], m["created"] - previous, zone)})
            if m["role"] == NOTIFICATION:
                at = datetime.fromtimestamp(m["created"], zone or timezone.utc).strftime("%A %d %B %Y, %H:%M")
                conversation.append({"role": m["role"], "at": at + ("" if zone else " UTC"), "content": m["content"]})
                continue  # not part of the user's and penrose's exchange: the next request's gap is counted without it
            entry = {"role": m["role"], "content": m["content"]}
            if m.get("attachments"):  # the files a message carried, by the names the agents are told them by
                entry["attachments"] = attachments.brief(self, m["attachments"])
            conversation.append(entry)
            previous = m["created"]
        context = {"conversation": conversation}
        if summary:
            context["conversation_summary"] = summary["summary"]
        return context

    # --- earlier conversations: what `recall` and `look_up` search ----------------------------------------

    def zone(self):
        """The user's time zone, or None when it is unknown or this Python has no time zone data (UTC then: callers
        say so). Unknown is None, not UTC: with tzdata installed, UTC would pass for a zone the user has."""
        if not (name := self.user_locale().get("timezone")):
            return None
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            return None

    def local_day(self, timestamp, zone=None):
        """The ISO day of `timestamp` where the user is."""
        return datetime.fromtimestamp(timestamp, zone or timezone.utc).date().isoformat()

    def day_bounds(self, first=None, last=None):
        """(start, end) timestamps of the days `first` to `last` (ISO, both included) where the user is; either
        may be None for open. Without the user's time zone, UTC widened by half a day each way, so a day is
        never cut short."""
        zone = self.zone()
        pad = 0 if zone else 12 * 3600

        def start(day):
            return datetime.combine(date.fromisoformat(day), datetime.min.time(), zone or timezone.utc).timestamp()

        return (
            start(first) - pad if first else None,
            start((date.fromisoformat(last) + timedelta(days=1)).isoformat()) + pad if last else None,
        )

    def search_history(self, query, limit=8, exclude_chat=None, after=None, before=None, coverage=COVERAGE, excerpts=2):
        """Earlier conversations that mention `query` (a string, or several wordings of the same thing): the
        user's messages and Relay's public replies, grouped by conversation, best match first, as
        [{chat_id, title, date, matches: [{role, date, excerpt}]}] (the `excerpts` best matches of each). Words match by their stem and any of them
        counts; rarer words weigh more (bm25). `after` / `before` (timestamps) keep it to a stretch of time. The
        conversation the agent is in (`exclude_chat`) is left out: the agent reads that one anyway. `coverage`: how
        much of one wording's word weight a conversation must hold (1: every word of it that is known anywhere)."""
        queries = [query] if isinstance(query, str) else list(query)
        wordings = [list(dict.fromkeys(w.lower() for w in re.findall(r"[^\W_]+", q, re.UNICODE) if len(w) > 1))
                    for q in queries]
        words = list(dict.fromkeys(w for ws in wordings for w in ws))[:24]
        if not words:
            return []
        where = f"WHERE {public('m.')} AND m.chat_id IS NOT ? AND m.created>=? AND m.created<? AND "
        args = (*PUBLIC_SOURCES, exclude_chat, after or 0, before if before is not None else float("inf"))
        try:
            self.rows("SELECT 1 FROM messages_fts LIMIT 0")
            fts = True
        except sqlite3.OperationalError:  # no FTS5: plain substring matches, newest first
            fts = False
        kept = self.covering(wordings, words, where, args, fts, coverage)
        if not kept:
            return []
        among = f"m.chat_id IN ({','.join('?' for _ in kept)}) AND "
        if fts:
            rows = self.rows(
                "SELECT m.chat_id,c.title,m.role,m.content,m.created FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid "
                f"JOIN chats c ON c.id=m.chat_id {where}{among}messages_fts MATCH ? ORDER BY bm25(messages_fts) LIMIT ?",
                (*args, *kept, " OR ".join(f'"{w}"' for w in words), limit * 6),
            )
        else:
            rows = self.rows(
                f"SELECT m.chat_id,c.title,m.role,m.content,m.created FROM messages m JOIN chats c ON c.id=m.chat_id {where}{among}("
                + " OR ".join("m.content LIKE ?" for _ in words) + ") ORDER BY m.created DESC LIMIT ?",
                (*args, *kept, *(f"%{w}%" for w in words), limit * 6),
            )
        zone, chats = self.zone(), {}
        for row in rows:
            if row["chat_id"] not in chats and len(chats) >= limit:
                continue
            chat = chats.setdefault(row["chat_id"], {
                "chat_id": row["chat_id"], "title": row["title"], "date": self.local_day(row["created"], zone), "matches": [],
            })
            if len(chat["matches"]) < excerpts:
                # The best-matching conversation is most likely the one meant: it is quoted at three times the
                # length, so a reply of several lines is read whole and not finished from imagination.
                best = next(iter(chats)) == row["chat_id"]
                chat["matches"].append({
                    "role": row["role"], "date": self.local_day(row["created"], zone),
                    "excerpt": excerpt(row["content"], words, width=660 if best else 220),
                })
        return list(chats.values())

    def covering(self, wordings, words, where, args, fts, coverage=COVERAGE):
        """The conversations whose matching words carry at least `coverage` of one wording's weight, the weight of a
        word being its BM25 IDF across all messages (the rarer, the heavier). Any single word matching is not
        enough: "temperature in New York now" must not find chats that only say "in", "new" and "now". A word no
        message contains weighs nothing: it can match nowhere (and recall's other wordings are meant to miss)."""
        test = "messages_fts MATCH ?" if fts else "content LIKE ?"
        source = "messages_fts" if fts else "messages"
        found = (lambda w: f'"{w}"') if fts else (lambda w: f"%{w}%")
        total = self.rows(f"SELECT count(*) AS n FROM {source}")[0]["n"]
        weight, holders = {}, {}
        for w in words:
            if not (n := self.rows(f"SELECT count(*) AS n FROM {source} WHERE {test}", (found(w),))[0]["n"]):
                continue
            weight[w] = max(math.log((total - n + 0.5) / (n + 0.5)), 1e-6)  # as FTS5's bm25 weighs it
            join = "messages_fts JOIN messages m ON m.rowid=messages_fts.rowid" if fts else "messages m"
            holders[w] = {r["chat_id"] for r in self.rows(
                f"SELECT DISTINCT m.chat_id FROM {join} {where}{'messages_fts MATCH ?' if fts else 'm.content LIKE ?'}",
                (*args, found(w)),
            )}

        def covers(chat):
            for ws in wordings:
                known = [w for w in ws if w in weight]
                if known and sum(weight[w] for w in known if chat in holders[w]) >= coverage * sum(weight[w] for w in known) - 1e-9:
                    return True
            return False

        return sorted(chat for chat in set().union(*holders.values()) if covers(chat)) if holders else []

    def conversations_between(self, after=None, before=None, limit=12, exclude_chat=None):
        """The conversations active in a stretch of time (timestamps; None for open), newest first, as
        [{chat_id, title, date, summary | matches}]: the chat's summary when it has one, else its first request
        and its last reply in that time. What "what did I do last week" is answered from."""
        lo, hi = after or 0, before if before is not None else float("inf")
        chats = self.rows(
            f"SELECT m.chat_id,c.title,MAX(m.created) AS last FROM messages m JOIN chats c ON c.id=m.chat_id WHERE {public('m.')} "
            "AND m.chat_id IS NOT ? AND m.created>=? AND m.created<? GROUP BY m.chat_id ORDER BY last DESC LIMIT ?",
            (*PUBLIC_SOURCES, exclude_chat, lo, hi, limit),
        )
        zone, found = self.zone(), []
        for chat in chats:
            entry = {"chat_id": chat["chat_id"], "title": chat["title"], "date": self.local_day(chat["last"], zone)}
            if (summary := self.summary(chat["chat_id"])) and summary["summary"]:
                entry["summary"] = summary["summary"][:800]
            else:
                said = self.rows(
                    f"SELECT role,content,created FROM messages WHERE chat_id=? AND {PUBLIC} AND created>=? AND created<? ORDER BY created",
                    (chat["chat_id"], *PUBLIC_SOURCES, lo, hi),
                )
                ends = [m for m in said if m["role"] == "user"][:1] + [m for m in said if m["role"] == "assistant"][-1:]
                entry["matches"] = [
                    {"role": m["role"], "date": self.local_day(m["created"], zone), "excerpt": m["content"][:300]} for m in ends
                ]
            found.append(entry)
        return found

    # --- memory: one named note per fact (runtime.memory decides what is kept and who reads it) ------

    def memories(self):
        """Every memory, most recently updated first."""
        return self.rows("SELECT * FROM memories ORDER BY updated DESC")

    def save_memory(self, name, type, description, content, app="", source="user", chat_id=None, expires=""):
        """Create the memory `name`, or replace what it says. Returns the saved row."""
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO memories(id,name,type,description,content,app,source,chat_id,created,updated,expires) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET type=excluded.type,"
                "description=excluded.description,content=excluded.content,app=excluded.app,"
                "source=excluded.source,chat_id=excluded.chat_id,updated=excluded.updated,expires=excluded.expires",
                (uid(), name, type, description, content, app or "", source, chat_id, now, now, expires or ""),
            )
            return dict(db.execute("SELECT * FROM memories WHERE name=?", (name,)).fetchone())

    def update_memory(self, memory_id, content, description=None, expires=None):
        """What the user edited in the panel: the memory's words, kept under its name. Returns the row, or None."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE memories SET content=?,description=?,expires=?,updated=? WHERE id=?",
                (content, description if description is not None else row["description"],
                 expires if expires is not None else (row["expires"] or ""), time.time(), memory_id),
            )
            return dict(db.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone())

    def forget_memory(self, name):
        """Delete the memory `name`. Returns the deleted row, or None."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE name=?", (name,)).fetchone()
            db.execute("DELETE FROM memories WHERE name=?", (name,))
            return dict(row) if row else None

    def event(self, run_id, kind, payload):
        self.execute(
            "INSERT INTO events(run_id,kind,payload,created) VALUES (?,?,?,?)",
            (run_id, kind, json.dumps(payload), time.time()),
        )
        self.ring(run_id)

    def ring(self, run_id):
        if self.on_event:
            self.on_event(run_id)

    def changed(self):
        """Something the window lists changed without an event of a run (a briefing was sorted): whoever follows
        the store's writes hears, as after an event, and reads its lists again."""
        self.ring("")

    def record_usage(self, chat_id, run_id, call_id, stage, provider, model, usage, created, credits, peak):
        """One model request's usage, kept with its cost in credits (None: no known price), and added to the
        chat's aggregate: {"requests", "credits", "peak_requests", "tokens": the same terms for every provider,
        summed, "usage": {provider: every field of its usage JSON summed}}. Kept when a rewind drops the run: the
        request was paid for. Returns the request's row."""
        row = {"chat_id": chat_id, "run_id": run_id, "call_id": call_id, "stage": stage, "provider": provider,
               "model": model, "created": created, "peak": bool(peak), "credits": credits, "usage": usage,
               "tokens": tokens(provider, usage)}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO usage(chat_id,run_id,call_id,stage,provider,model,created,peak,credits,usage) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (chat_id, run_id, call_id, stage, provider, model, created, int(bool(peak)), credits, json.dumps(usage)),
            )
            found = db.execute("SELECT usage FROM chat_usage WHERE chat_id=?", (chat_id,)).fetchone()
            total = upgraded(json.loads(found["usage"]) if found and found["usage"] else {})
            total["requests"] = total.get("requests", 0) + 1
            total["peak_requests"] = total.get("peak_requests", 0) + bool(peak)
            total["credits"] = total.get("credits", 0) + (credits or 0)
            total["tokens"] = add_up(total.get("tokens", {}), row["tokens"])
            total["usage"].setdefault(provider, {})
            add_up(total["usage"][provider], usage)
            db.execute("INSERT OR REPLACE INTO chat_usage VALUES (?,?)", (chat_id, json.dumps(total)))
        return row

    def chat_usage(self, chat_id):
        """(the chat's aggregate usage, its requests oldest first)."""
        found = self.rows("SELECT usage FROM chat_usage WHERE chat_id=?", (chat_id,))
        total = upgraded(json.loads(found[0]["usage"])) if found and found[0]["usage"] else None
        requests = self.rows(
            "SELECT id,run_id,call_id,stage,provider,model,created,peak,credits,usage FROM usage WHERE chat_id=? ORDER BY id",
            (chat_id,),
        )
        for r in requests:
            r["peak"], r["usage"] = bool(r["peak"]), json.loads(r["usage"] or "{}")
            r["tokens"] = tokens(r["provider"], r["usage"])
        return total, requests

    def image(self, run_id, node_id, jpeg, kind="shown", caption="", place="", size=None):
        """Keep a picture of the run: a screenshot a model was shown (kind "shown", so the trace can show it too),
        or one a phone agent clipped for the user's answer (kind "clip", with its caption and where it was taken)."""
        w, h = size or (0, 0)
        with self.connect() as db:
            return db.execute(
                "INSERT INTO images(run_id,node_id,created,jpeg,kind,caption,place,w,h) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, node_id, time.time(), jpeg, kind, caption, place, w, h),
            ).lastrowid

    def save(self, state, whole=True, event=None):
        """The worker's save of a working run; never over a stop or a discard (the API wrote those). New and
        recent turns always; the rest of the state only when `whole`. `event` ((kind, payload)) is recorded in
        the same transaction."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            head, turns = split(state)
            if whole:
                saved = db.execute(
                    "UPDATE runs SET status=?,state=?,updated=? WHERE id=? AND status NOT IN (?,?)",
                    (state["status"], json.dumps(head), time.time(), state["id"], *HALTED),
                ).rowcount
            else:
                saved = db.execute(
                    "UPDATE runs SET updated=? WHERE id=? AND status NOT IN (?,?)", (time.time(), state["id"], *HALTED)
                ).rowcount
            if saved and "plan" in state:
                self.save_turns(db, state["id"], turns)
            if saved and whole:
                forget_private(db, state)
            if event:
                db.execute("INSERT INTO events(run_id,kind,payload,created) VALUES (?,?,?,?)",
                           (state["id"], event[0], json.dumps(event[1]), time.time()))
        if event:
            self.ring(state["id"])

    def save_turns(self, db, run_id, turns):
        """Write the turns that are new or may have changed since this process last wrote them."""
        known = self.turns_written.get(run_id)
        if known is None:
            known = self.turns_written[run_id] = {
                step_id: [None] * count for step_id, count in db.execute(
                    "SELECT step_id,COUNT(*) FROM turns WHERE run_id=? GROUP BY step_id", (run_id,))
            }
        for step_id in [s for s in known if s not in turns]:  # a step the plan no longer has
            db.execute("DELETE FROM turns WHERE run_id=? AND step_id=?", (run_id, step_id))
            del known[step_id]
        for step_id, items in turns.items():
            have = known.get(step_id, [])
            if len(items) < len(have):  # the step was started over
                db.execute("DELETE FROM turns WHERE run_id=? AND step_id=? AND idx>=?", (run_id, step_id, len(items)))
                have = have[:len(items)]
            start = max(0, len(have) - TURN_TAIL)
            texts = [json.dumps(turn) for turn in items[start:]]
            db.executemany("INSERT OR REPLACE INTO turns VALUES (?,?,?,?)", [
                (run_id, step_id, i, text) for i, text in enumerate(texts, start)
                if i >= len(have) or have[i] != text
            ])
            final = max(0, len(items) - TURN_TAIL)
            known[step_id] = [None] * final + texts[final - start:]

    def status(self, run_id):
        """The run's status, or None: what the worker checks often, without reading the whole state."""
        rows = self.rows("SELECT status FROM runs WHERE id=?", (run_id,))
        return rows[0]["status"] if rows else None

    def set_run_field(self, run_id, key, value):
        """Change one field of a run's saved state, leaving the rest as it is now."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
            if row:
                head = json.loads(row["state"])
                head[key] = value
                db.execute("UPDATE runs SET state=? WHERE id=?", (json.dumps(head), run_id))

    def load(self, run_id):
        """The run's whole saved state, or None. What this process wrote of its turns is checked again."""
        self.turns_written.pop(run_id, None)
        with self.connect() as db:
            return read_state(db, run_id)

    def enqueue(self, run_id, db):
        db.execute("INSERT INTO jobs VALUES (?,?,'queued',NULL,?)", (uid(), run_id, time.time()))

    def take_messages(self, run_id, mode="steer"):
        """The run's waiting messages of `mode`, oldest first, taken out of the inbox (app.delivery)."""
        from .delivery import take

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return take(db, run_id, mode)

    def has_messages(self, run_id, mode="steer"):
        """Whether the run has waiting messages of `mode`, without taking them."""
        with self.connect() as db:
            return db.execute("SELECT 1 FROM inbox WHERE run_id=? AND mode=? LIMIT 1", (run_id, mode)).fetchone() is not None

    def settle(self, run_id):
        """After the run's job: what it did not read goes out as the next message (app.delivery.settle)."""
        from .delivery import settle

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return settle(db, run_id)

    def queued(self):
        """Whether a job waits: a read, so an idle worker can ask often without taking the write lock."""
        return bool(self.rows("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1"))

    def users_waiting(self):
        """Whether a job of the user's own waits (not one penrose began on its own): a background run then pauses."""
        return bool(self.rows(
            f"SELECT 1 FROM jobs j LEFT JOIN runs r ON r.id=j.run_id WHERE j.status='queued' AND {BACKGROUND} IS NOT 1 LIMIT 1"))

    def background(self, run_id):
        """Whether the run is one penrose began on its own and that gives way to the user's (briefings/prepare.py)."""
        return bool(self.rows(f"SELECT 1 FROM runs r WHERE r.id=? AND {BACKGROUND} IS 1", (run_id,)))

    def claim(self, owner):
        """The next job, the oldest first, the user's own before the ones penrose began on its own (a run's
        `background`: briefings/prepare.py)."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                f"SELECT j.* FROM jobs j LEFT JOIN runs r ON r.id=j.run_id WHERE j.status='queued' "
                f"ORDER BY {BACKGROUND} IS 1, j.heartbeat LIMIT 1"
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE jobs SET status='running',owner=?,heartbeat=? WHERE id=?",
                    (owner, time.time(), row["id"]),
                )
                return dict(row)
        return None

    def recover_stale(self):
        """Runs whose worker disappeared are interrupted, never replayed (an external action may already have
        completed). Returns their ids, so the worker frees the phone they held: an interrupted run leases it
        again when the user continues it, and meanwhile other tasks can use it."""
        interrupted = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            stale = db.execute(
                "SELECT * FROM jobs WHERE status='running' AND heartbeat<?", (time.time() - 90,)
            ).fetchall()
            for job in stale:
                state = read_state(db, job["run_id"])
                if state:
                    if state["status"] not in ("completed", "cancelled", "failed", "stopped"):
                        state["status"] = "interrupted"
                        halt(state)
                        state["error"] = (
                            "Worker stopped. Inspect the emulator before resuming; an external action may already have completed."
                        )
                        put_state(db, state)
                        self.turns_written.pop(state["id"], None)
                        interrupted.append(state["id"])
                db.execute("UPDATE jobs SET status='interrupted' WHERE id=?", (job["id"],))
        return interrupted

    def lease(self, slots, owner):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT slot FROM leases WHERE owner=?", (owner,)).fetchone()
            if existing:
                return existing["slot"]
            busy = {r["slot"] for r in db.execute("SELECT slot FROM leases")}
            for slot in slots:
                if slot not in busy:
                    db.execute("INSERT INTO leases(slot,owner,created) VALUES (?,?,?)", (slot, owner, time.time()))
                    return slot
        return None

    def profile_get(self, key, default=None):
        rows = self.rows("SELECT value FROM profile WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def profile_set(self, key, value):
        self.execute("INSERT OR REPLACE INTO profile VALUES (?,?)", (key, json.dumps(value)))

    def google_account(self):
        """The Google account last seen signed in on the phone ({account, at}), or {}. The user signs
        in themselves before the first task; the API records the account whenever it reads the phone."""
        saved = self.profile_get("google_account", {}) or {}
        return {"account": saved["account"]} if saved.get("account") else {}

    def set_google_account(self, account):
        if (self.profile_get("google_account", {}) or {}).get("account") != account:
            self.profile_set("google_account", {"account": account, "at": time.time()})

    def user_locale(self):
        """The user's time zone and language ({"timezone", "language"}) as the browser or phone they last used
        Relay from reports them, or {} until one has. Never the server's own: Relay may run far from its user."""
        saved = self.profile_get("locale", {}) or {}
        return {k: saved[k] for k in ("timezone", "language") if saved.get(k)}

    def set_user_locale(self, timezone, language):
        if self.user_locale() != {"timezone": timezone, "language": language}:
            self.profile_set("locale", {"timezone": timezone, "language": language, "at": time.time()})

    # --- private entries: values the user gave a chat privately (a card number, a password) ----------------
    # The agents see only their number and the user's label; the runtime types a value where an agent writes
    # {private:N} (device/driver.py). They go when the chat's task ends (forget_private) or the chat is deleted.

    def add_private(self, db, chat_id, entries):
        """Keep [(label, value)] for the chat; returns [{"n", "label"}] of the new ones, numbered after the rest."""
        last = db.execute("SELECT MAX(n) FROM private_entries WHERE chat_id=?", (chat_id,)).fetchone()[0] or 0
        added = []
        for i, (label, value) in enumerate(entries, start=last + 1):
            db.execute("INSERT INTO private_entries VALUES (?,?,?,?,?)", (chat_id, i, label, value, time.time()))
            added.append({"n": i, "label": label})
        return added

    def private_labels(self, chat_id):
        """The chat's private entries as an agent may read them: [{"n", "label"}], never the values."""
        return [dict(r) for r in self.rows("SELECT n,label FROM private_entries WHERE chat_id=? ORDER BY n", (chat_id,))]

    def private_value(self, chat_id, n):
        rows = self.rows("SELECT value FROM private_entries WHERE chat_id=? AND n=?", (chat_id, n))
        return rows[0]["value"] if rows else None

    def consume_secret(self, run_id, node_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM secrets WHERE run_id=? AND node_id=?", (run_id, node_id)
            ).fetchone()
            db.execute("DELETE FROM secrets WHERE run_id=? AND node_id=?", (run_id, node_id))
            return row["value"] if row else None
