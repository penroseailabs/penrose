"""The "For you" list: what every screen starts from (the sidebar, the empty chat, the voice screen), pushed with
the window's other updates (api/updates.py). A briefing's details, as the user looks into one (`detail`). And the
list of apps the user chooses from.
"""

import json
import time

from ..delivery import IN_HAND
from . import groups, policy

# How long a briefing nobody opened stays in the list.
READY_SECONDS = 48 * 3600
# How many of what merely came in are shown, and of the chats that wait for the user.
CAME_IN_SHOWN, WAITING_SHOWN = 6, 12
# The most briefings listed: the screens show them all, and the voice tray holds a card for each.
READY_SHOWN = 20
# An opened briefing stays in the list while its task is one of these: at work, waiting for the user, or stopped
# short of an end. One that ended (completed, discarded) has been dealt with, unless penrose prepared it on its own
# and the user has not answered it yet (`stays`). Looking at it does not take it away: only dismissing it,
# answering it, or age (READY_SECONDS) do.
UNFINISHED = (*IN_HAND, "failed", "interrupted", "stopped")
# How much of a briefing's details shows: its newest notifications, each cut to this many characters.
ARRIVED_SHOWN, ARRIVED_CHARS = 20, 600
# How much of a waiting chat's question its line in the list holds.
ASKS_CHARS = 320


def plain(line):
    """A line of a reply without its markdown, its spaces collapsed."""
    return " ".join(line.replace("*", "").replace("#", "").split())


def cut(text, chars):
    """`text`, or its first `chars` characters ending in an ellipsis."""
    return text if len(text) <= chars else text[: chars - 1].rstrip() + "…"


def first_line(text, chars=140):
    """The first line of a reply that says something, without its markdown, cut to `chars`."""
    for line in (text or "").splitlines():
        if (said := plain(line)) and not said.startswith("!["):
            return cut(said, chars)
    return ""


def first_paragraph(text, chars=ASKS_CHARS):
    """The first paragraph of a reply that says something (its lines up to the next blank one, pictures skipped),
    without its markdown, cut to `chars`: enough of a question to answer it from the list."""
    lines = []
    for line in (text or "").splitlines():
        said = plain(line)
        if not said:
            if lines:
                break
        elif not said.startswith("!["):
            lines.append(said)
    return cut(" ".join(lines), chars)


def item(briefing):
    """A briefing as the screens show it."""
    app = {"package": briefing["package"], "label": briefing["label"]}
    if briefing["source"]:
        app["site"] = briefing["source"]
    return {
        "id": briefing["id"], "app": app, "name": briefing["name"] or briefing["label"],
        "headline": briefing["headline"] or "", "why": briefing["why"] or "", "asks": briefing["asks"] or "",
        "gist": json.loads(briefing["gist"] or "[]"), "count": briefing["count"], "latest": briefing["latest"],
        "urgent": bool(briefing["urgent"]), "seen": briefing["seen"] is not None,
    }


def task_of(store, chat_id):
    """The latest run of an opened briefing's chat, with what its state says the screens need ({id, status,
    updated, options, yielded, preparing}), or None when the chat is gone."""
    with store.connect() as db:
        return db.execute(
            "SELECT id,status,updated,json_extract(state,'$.options') AS options,json_extract(state,'$.yielded') AS yielded,"
            "json_extract(state,'$.preparing') AS preparing FROM runs WHERE chat_id=? ORDER BY updated DESC LIMIT 1",
            (chat_id,)).fetchone()


def stays(task):
    """Whether an opened briefing stays in the list: while its task is unfinished, or penrose prepared it, it ended,
    and the user has not answered it yet (it is still `preparing`): what it found is theirs to look at."""
    return task["status"] in UNFINISHED or (task["status"] == "completed" and bool(task["preparing"]))


def with_task(store, briefing, task):
    """What an opened briefing adds to its item: its chat, its task's status and, once the task waits for the user
    or ended, what it found (and while it waits, what it offers)."""
    # Paused while a task of the user's has the phone (prepare.py): it carries on afterwards, so it is still at work.
    status = "queued" if task["status"] == "stopped" and task["yielded"] else task["status"]
    more = {"chat_id": briefing["chat_id"], "status": status}
    if task["status"] in ("waiting_user", "completed"):
        if task["status"] == "waiting_user" and task["options"]:
            more["options"] = json.loads(task["options"])
        if (reply := store.last_reply(briefing["chat_id"])) and (found := first_line(reply["content"])):
            more["found"] = found
    return more


def opened(store, briefing):
    """What an opened briefing adds to its item in the list (`with_task`), or None when it is over: its task ended
    and is not one penrose prepared that waits for the user's answer (`stays`), or its chat is gone. Whether the
    user has looked at it does not count."""
    task = task_of(store, briefing["chat_id"])
    return with_task(store, briefing, task) if task and stays(task) else None


def detail(store, briefing_id):
    """A briefing as the user looks into it: its item, its task while its chat is there, its newest notifications
    (`arrived`, oldest first, and how many came `earlier`) and, once its task waits for the user or ended,
    penrose's whole reply (`prepared`). For any briefing, listed or not; None when there is no such one."""
    briefing = groups.find(store, briefing_id)
    if not briefing:
        return None
    entry = item(briefing)
    task = task_of(store, briefing["chat_id"]) if briefing["chat_id"] else None
    if task:
        entry.update(with_task(store, briefing, task))
    notes = groups.notes_of(store, briefing, ARRIVED_SHOWN)
    entry["arrived"] = [{"from": n["sender"] or n["title"], "text": cut(n["text"] or "", ARRIVED_CHARS), "at": n["posted"]}
                        for n in notes]
    [total] = store.rows("SELECT COUNT(*) AS n FROM notifications WHERE briefing_id=?", (briefing_id,))
    entry["earlier"] = max(0, total["n"] - len(notes))
    if task and task["status"] in ("waiting_user", "completed") and (reply := store.last_reply(briefing["chat_id"])):
        entry["prepared"] = {"content": reply["content"], "at": reply["created"]}
    return entry


def waiting(store, held):
    """The chats whose task waits for the user's answer, the latest first, without the briefings' own (`held`) and
    those the user took off the list since their task last moved (actions.hide_waiting). Each with the start of
    what it asks, and what it needs from them in a line (the orchestrator's `needs` on ask_user, or '')."""
    rows = store.rows(
        """SELECT c.id, c.title, r.updated, json_extract(r.state,'$.needs') AS needs FROM chats c JOIN runs r ON r.id =
             (SELECT id FROM runs WHERE chat_id=c.id ORDER BY updated DESC LIMIT 1)
             LEFT JOIN for_you_hidden h ON h.chat_id=c.id
           WHERE r.status='waiting_user' AND (h.at IS NULL OR h.at<r.updated) ORDER BY r.updated DESC LIMIT ?""",
        (WAITING_SHOWN + len(held),))
    found = []
    for chat in rows:
        if chat["id"] in held:
            continue
        reply = store.last_reply(chat["id"])
        found.append({"chat_id": chat["id"], "title": chat["title"], "updated": chat["updated"],
                      "asks": first_paragraph(reply["content"]) if reply else "", "needs": chat["needs"] or ""})
    return found[:WAITING_SHOWN]


def feed(store, now=None):
    """{"ready": the briefings (an opened one with its chat: `opened`), "waiting": the other chats that wait for
    the user's answer, "came_in": what else arrived on the apps the user is briefed on, "more": how many of those
    are not shown, "unseen": the briefings the user has not opened or looked at (the badge), "apps_on": how many
    apps they chose}."""
    now = now or time.time()
    ready, held = [], set()
    for briefing in store.rows(
        "SELECT * FROM briefings WHERE verdict=? AND dismissed IS NULL AND latest>? ORDER BY latest DESC LIMIT ?",
        (groups.BRIEF, now - READY_SECONDS, READY_SHOWN)):
        entry = item(briefing)
        if briefing["chat_id"]:
            more = opened(store, briefing)
            if more is None:
                continue
            entry.update(more)
            held.add(briefing["chat_id"])
        ready.append(entry)
    listed = store.rows(
        "SELECT * FROM briefings WHERE verdict=? AND dismissed IS NULL AND chat_id IS NULL AND latest>? ORDER BY latest DESC",
        (groups.LIST, now - groups.RECENT_SECONDS))
    return {
        "ready": ready, "waiting": waiting(store, held), "came_in": [item(b) for b in listed[:CAME_IN_SHOWN]],
        "more": max(0, len(listed) - CAME_IN_SHOWN), "unseen": sum(1 for e in ready if not e["seen"]),
        "apps_on": len(policy.load(store)["apps"]),
    }


def apps(store, days=7):
    """The apps whose notifications the log holds, each with whether the user is briefed on it: what the settings
    list, and what the first choice is offered from. Apps where people message the user (a notification with a
    sender) come first, then the rest, each the busiest first: a chat app with a few messages matters more than a
    shop's daily offers. An app is named as its latest notification named it."""
    chosen = policy.load(store)["apps"]
    rows = store.rows(
        """SELECT n.package, COUNT(*) AS count, MAX(COALESCE(n.sender,'')!='') AS people,
                  (SELECT label FROM notifications WHERE package=n.package AND label!=package ORDER BY id DESC LIMIT 1) AS label
             FROM notifications n WHERE n.posted>? GROUP BY n.package ORDER BY people DESC, count DESC""",
        (time.time() - days * 86400,))
    listed = [{"package": r["package"], "label": r["label"] or r["package"], "count": r["count"], "people": bool(r["people"]),
               "on": r["package"] in chosen} for r in rows]
    known = {r["package"] for r in rows}
    # One the user chose that has been quiet this week is still theirs to turn off.
    return listed + [{"package": p, "label": p, "count": 0, "people": False, "on": True} for p in chosen if p not in known]

