"""What the reception desk knows each time it speaks (runtime/desk.py, api/desk.py `/state`): penrose as it is at
that moment, never a history of what happened.

The desk keeps no account of its own of the errands. Before every reply the phone asks for this block and puts it
last in the desk's conversation, in place of the one before (desk.ts): the errands newest first, each numbered and
led by its id, one labelled field per line:

    1. s:4c21e9 "Order the usual pad thai"
       status: working (for 1 min)
       doing: Add 1 × Pad Thai, large
       on the phone now (DoorDash): Cart open: 1 × Pad Thai (Large) $16.50 → $14.03; total $21.40
       not told yet: the question
       notes:
         - DoorDash usual is Thai Basil Kitchen: ordered 15 Nov
       said:
         - user, 1 min ago: "Order the usual pad thai, large."

Whatever was written at some moment says how long ago that was, rounded the way people say it ("a few seconds ago",
"3 min ago", "4 h ago"): a result that says "arriving in about 40 minutes" was read out four hours later as if it
had just been written (2026-11-24), because the block gave only the clock time it finished.

Everything is read from where the errands keep it: the run's status and saved state (its notes, the running step's
own log of the screen and what it is doing now), the chat's last messages, and the desk's log for what the user has
not been told yet. A deleted errand is simply not there; one that stopped says so; nothing an errand did for an
earlier request is shown as current. The first line after the clock says what the desk itself last acted on, as a
hint: it is not a claim about what the user will mean next.
"""

import json
import re
import time
from datetime import datetime, timezone

from ..plan import ACTIVE
from ..store import PUBLIC_SOURCES, read_state
from . import desk, frontdesk

ERRANDS = 8  # errands in the block, the most recently active first
STALL = 240  # seconds a working errand may save nothing before it reads as stalled
ACTED_SECONDS = 3600  # what the desk acted on longer ago than this is no hint of what the user means now
RESULT_CHARS = 400
ASK_CHARS = 300
NOTE_CHARS = 300
SAID_CHARS = 300
SCREEN_CHARS = 320
GOAL_CHARS = 220
NOTES = 3  # the last notes of an errand shown in full
NOTES_BRIEF = 2  # ...and of the others
SAID_LINES = 6  # the last lines said in an errand shown in full
REWIND_ERRANDS = 3  # the latest errands whose rewind points are listed

DID = {"start_errand": "started it", "pass_on": "passed the user's words on to it", "stop_errand": "stopped it",
       "resume_errand": "resumed it", "rewind_errand": "rewound it", "read_reply": "read its reply to the user"}
UNTOLD = {"finished": "the result", "asks": "the question", "failed": "the failure"}


def plain(text, limit):
    """Words an agent wrote for another agent, on one line and without their machine parts: package names,
    element numbers, picture refs, links, markdown."""
    text = " ".join(str(text or "").split())
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\((?:https?://|/)[^)]*\)", r"\1", text)
    text = re.sub(r"\s*\((?:[a-z]+\.)+[A-Za-z.]+(?:,\s*\w+)?\)", "", text)  # (com.android.settings, SomeActivity)
    text = re.sub(r"\s*\[\d+\]", "", text)  # [20]
    text = re.sub(r"\(?image:\d+\)?", "a picture", text)
    text = re.sub(r"https?://\S+", "(a link)", text)
    text = re.sub(r"[*_#|`]+", "", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def hour(at, now, zone):
    """A time as people say it ("6:41 pm"), with the day when it is not today."""
    then, today = datetime.fromtimestamp(at, zone or timezone.utc), datetime.fromtimestamp(now, zone or timezone.utc)
    text = then.strftime("%I:%M %p").lstrip("0").lower()
    return text if then.date() == today.date() else f"{then.strftime('%d %b').lstrip('0')}, {text}"


def span(seconds):
    """How long something has lasted: "40 s", "3 min", "2 h", "3 days"."""
    seconds = max(0, seconds)
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} days"


def ago(seconds):
    """How long ago something was written or done, rounded the way people say it: "a few seconds ago", "3 min ago",
    "1 h 20 min ago", "4 h ago", "2 days ago"."""
    seconds = max(0, seconds)
    if seconds < 45:
        return "a few seconds ago"
    if seconds < 90:
        return "1 min ago"
    if seconds < 3300:
        return f"{round(seconds / 60)} min ago"
    if seconds < 3 * 3600:  # to ten minutes: "in half an hour", said two hours ago, is worked out from this
        hours, minutes = divmod(round(seconds / 600) * 10, 60)
        return f"{hours} h {minutes} min ago" if minutes else f"{hours} h ago"
    if seconds < 23.5 * 3600:
        return f"{round(seconds / 3600)} h ago"
    days = max(1, round(seconds / 86400))
    return f"{days} day{'' if days == 1 else 's'} ago"


def acted(store, now):
    """The errand the desk last acted on for the user, within the hour: (chat id, what it did, when), or None."""
    rows = store.rows(
        "SELECT kind, payload, created FROM desk_log WHERE kind IN ('call','output') AND created>? ORDER BY id DESC LIMIT 40",
        (now - ACTED_SECONDS,),
    )
    entries = [(r["kind"], json.loads(r["payload"]), r["created"]) for r in rows]
    names = {p.get("call_id"): desk.RENAMED.get(p.get("name"), p.get("name")) for kind, p, _ in entries if kind == "call"}
    for kind, payload, created in entries:  # newest first
        if kind != "output":
            continue
        try:
            output = json.loads(payload.get("output") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(output, dict) or output.get("error") or not output.get("session"):
            continue
        if chat_id := desk.resolve(store, output["session"]):
            return chat_id, DID.get(names.get(payload.get("call_id")), "acted on it"), created
    return None


def step_now(state, status):
    """The step the errand is in (or waits in), and what its agent keeps while it works; ({}, {}) with none."""
    plan = (state or {}).get("plan") or []
    wanted = ("waiting_user",) if status == "waiting_user" else ACTIVE
    step = next((s for s in plan if s.get("status") in wanted), None)
    if not step:
        return {}, {}
    kept = next((m for m in (step.get("sessions") or {}).values() if isinstance(m, dict) and "log" in m), {})
    return step, kept


def doing(step, kept):
    """What the errand is doing now, in its agent's own words where it has said ("[now] …" of its progress), else
    the step's goal."""
    if found := re.search(r"\[now\]\s*(.+)", str(kept.get("progress") or "")):
        return plain(found.group(1), GOAL_CHARS)
    return plain(step.get("goal"), GOAL_CHARS)


def on_phone(step, kept):
    """What the errand's agent last wrote about the screen in front of it: (app, words), or ("", "")."""
    lines = [line for line in kept.get("log") or [] if isinstance(line, str) and "| (runtime)" not in line]
    if not lines:
        return "", ""
    return ((step.get("app") or {}).get("name") or ""), plain(lines[-1].split(" | ", 1)[-1], SCREEN_CHARS)


def outcomes(state, limit):
    """For an errand whose task kept no notes: how its last steps went, from the journal."""
    out = []
    for entry in (state or {}).get("journal") or []:
        found = re.match(r"^\S+ (done|failed|handed back unfinished|verified|not verified)(?: \(attempt \d+\))?: (.*)$",
                         entry.get("line") or "", re.DOTALL)
        if found:
            label = {"done": "Done", "failed": "Did not work", "handed back unfinished": "Got only partway",
                     "verified": "Checked", "not verified": "Could not confirm"}[found.group(1)]
            out.append(f"{label}: {plain(found.group(2), NOTE_CHARS)}")
    return out[-limit:]


def said(store, chat_id, shown, now):
    """The last lines said in the errand: the user's, and penrose's answers (a run of its own lines counts as the
    last of them; the one already shown as the result or the question is left out)."""
    marks = ",".join("?" for _ in PUBLIC_SOURCES)
    rows = store.rows(
        f"SELECT role, content, spoken, created FROM messages WHERE chat_id=? AND content<>'' AND "
        f"(role='user' OR (role='assistant' AND source IN ({marks}))) ORDER BY created DESC LIMIT ?",
        (chat_id, *PUBLIC_SOURCES, SAID_LINES * 4),
    )
    rows = list(reversed(rows))
    kept = [m for i, m in enumerate(rows) if m["role"] == "user" or i + 1 == len(rows) or rows[i + 1]["role"] == "user"]
    words = lambda m: m["spoken"] if m["role"] == "assistant" and m["spoken"] else m["content"]
    if kept and kept[-1]["role"] == "assistant" and shown and plain(words(kept[-1]), 60) == plain(shown, 60):
        kept.pop()
    return [f'{"user" if m["role"] == "user" else "penrose"}, {ago(now - m["created"])}: "{plain(words(m), SAID_CHARS)}"'
            for m in kept[-SAID_LINES:]]


def state(store, about="", opened=False, at=0.0, now=None):
    """The block, and what the phone needs beside it: {"text", "about" (the errand shown in full as the one being
    talked about), "due" (the ids of that errand's events the user has not been told)}.

    `about`: the chat the phone holds as the one being talked about; `opened`: the user opened its card on their
    screen, at `at`. Without one, the errand the desk last acted on."""
    from ..api import chats as chats_api

    now = now or time.time()
    zone = store.zone()
    rows = frontdesk.chats(store)
    shown = rows[:ERRANDS]
    last = acted(store, now)
    if not any(r["id"] == about for r in rows):
        about, opened = (last[0] if last else ""), False
    if about and not any(r["id"] == about for r in shown):  # the one being talked about is always in the block
        shown = [*shown[: ERRANDS - 1], next(r for r in rows if r["id"] == about)]
    untold = {}
    for entry in desk.unannounced(store):
        untold.setdefault(entry["payload"].get("chat_id"), []).append(entry)

    clock = datetime.fromtimestamp(now, zone or timezone.utc)
    out = [f"[penrose now] {clock.strftime('%a %d %b')}, {hour(now, now, zone)}{'' if zone else ' UTC'}", ""]
    hint = " A hint only: what the user says next may be about any errand, or a new one."
    if opened and about and (not last or at >= last[2]):
        out.append(f"The user opened s:{desk.short(about)} on their screen {ago(now - at)}.{hint}")
    elif last:
        out.append(f"Last acted on: s:{desk.short(last[0])} ({last[1]} {ago(now - last[2])}).{hint}")
    else:
        out.append("Last acted on: nothing in the last hour.")

    live = 0
    with store.connect() as db:
        for place, row in enumerate(shown):
            chat_id, status = row["id"], row["status"] or "new"
            run = read_state(db, row["run_id"]) if row["run_id"] else None
            working, waiting = status in desk.WORKING, status == "waiting_user"
            full = place == 0 or chat_id == about or working or waiting or status == "failed"
            live += working or waiting
            out += ["", f'{place + 1}. s:{desk.short(chat_id)} "{row["title"]}"']
            quiet = now - row["updated"]
            if status == "running" and quiet > STALL:
                out.append(f"   status: stalled (working, but nothing has happened for {span(quiet)})")
            elif working:
                began = (run or {}).get("created") or row["updated"]
                out.append(f"   status: {desk.STATUS.get(status, status)} (for {span(now - began)})")
            elif waiting:
                out.append(f"   status: waiting for the user (for {span(quiet)})")
            elif status == "new":
                out.append("   status: just started")
            else:
                out.append(f"   status: {desk.STATUS.get(status, status)} {ago(quiet)} ({hour(row['updated'], now, zone)})")

            step, kept = step_now(run, status) if working or waiting else ({}, {})
            if working and step:
                out.append(f"   doing: {doing(step, kept)}")
            app, screen = on_phone(step, kept)
            if screen:
                out.append(f"   {'on the phone now' if working else 'last on the phone'}{f' ({app})' if app else ''}: {screen}")
            news = [e for e in untold.get(chat_id, []) if desk._kind(e["payload"]) in UNTOLD]
            main = ""
            reply = db.execute(
                "SELECT content, spoken, created FROM messages WHERE chat_id=? AND role='assistant' AND content<>'' "
                "ORDER BY created DESC LIMIT 1", (chat_id,)).fetchone() if waiting or status in ("completed", "failed") else None
            last_said = (reply["spoken"] or reply["content"]) if reply else ""
            written = reply["created"] if reply else row["updated"]  # when the question or the result was written
            if waiting:
                # The question as it was put to the user: the event's words, a step's own question, or (asked by the
                # orchestrator in the chat) the last thing it said.
                question = frontdesk.question_of(run) or {}
                event = next((e for e in reversed(news) if desk._kind(e["payload"]) == "asks"), None)
                main = (event["payload"]["text"] if event else "") or question.get("question") or last_said
                private = bool(question.get("sensitive")) or any(e["payload"].get("private") for e in news)
                if main:
                    out.append(f'   asks ({ago(now - (event["created"] if event else written))}): "{plain(main, ASK_CHARS)}"'
                               + (" (private: the user answers in a form on their screen, never aloud)" if private else ""))
            elif status in ("completed", "failed"):
                main = last_said
                if main:
                    out.append(f'   {"result" if status == "completed" else "what happened"} ({ago(now - written)}): '
                               f'"{plain(main, RESULT_CHARS)}"')
            if news:
                kinds = list(dict.fromkeys(UNTOLD[desk._kind(e["payload"])] for e in news))
                # Its pictures show on the user's screen as the desk tells it: the desk reads the captions only.
                pictures = next((e["payload"]["pictures"] for e in reversed(news) if e["payload"].get("pictures")), None)
                out.append(f"   not told yet: {' and '.join(kinds)}" + (f" {desk.on_screen(pictures)}" if pictures else ""))
            limit = NOTES if full else NOTES_BRIEF
            notes = [plain(n, NOTE_CHARS) for n in ((run or {}).get("notes") or [])[-limit:] if isinstance(n, str) and n.strip()]
            notes = notes or outcomes(run, limit)
            if notes:
                out.append("   notes:")
                out += [f"     - {n}" for n in notes]
            if not full:
                continue
            lines = said(store, chat_id, main, now)
            if lines:
                out.append("   said:")
                out += [f"     - {line}" for line in lines]
            if place < REWIND_ERRANDS:
                points = []
                for message_id in chats_api.rewindable(db, chat_id):
                    text = db.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()
                    if text:
                        points.append(f'm:{desk.short(message_id)} "{" ".join(text["content"].split())[:60]}"')
                if points:
                    out.append("   rewind points: " + "; ".join(points))
    if not shown:
        out += ["", "No errands yet."]
    out += ["", f"{live} running or waiting on the user." if live else "Nothing is running or waiting."]
    due = [e["id"] for e in untold.get(about, []) if desk._kind(e["payload"]) in UNTOLD] if about else []
    return {"text": "\n".join(out), "about": about, "due": due}
