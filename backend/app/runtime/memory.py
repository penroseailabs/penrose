"""Long-term memory, built as Claude Code builds its own: one named note per fact, typed, with a one-line
description; an index of those lines, held to a fixed size, that the agents who plan and answer always see;
the notes themselves read in full only when one is asked for by name; and one agent that writes them, updating
by name instead of piling up near-duplicates. Nothing is searched, and nothing sums the notes up (the index is
the summary): the agent reading the index is what decides which note matters.

    recall   about_user(store)       gate and orchestrator: the index (one line per memory)
             read(store, names)      their `read_memory` tool: the named notes in full
             for_step(store, app, names)  a phone agent: the notes its step names (the orchestrator chose them
                                     from the index) and the notes on its app; it sees no index, as a Claude
                                     Code subagent knows of the memory only what its brief carries
    learn    learn(runner, state)    after each run: the user's new messages and the subagents' lessons (with
                                     learning off, only what the user plainly asked to have remembered)
             remember_now(...)       the gate, when the user only asks to remember or forget: done before the
                                     reply, so the reply says what happened
             remember_findings(...)  the orchestrator, during a task: what it found about the user's world (its
                                     `remember` tool), so a long task's findings outlive a failure
             add(model, store, text) the panel: a note the user writes, placed by the agent

Memories were true when written: every agent sees each one's date and is told that the phone and the
user's latest words win.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

from ..agents.catalog import route
from ..agents.registry import MEMORY
from ..config import settings
from ..errors import safe_error
from ..models import PRIVATE_MARK
from ..tools.memory import ReadMemory

log = logging.getLogger(__name__)

# What the API (api/runs.py) records in place of a private answer: there is nothing to learn from it.
PRIVATE_ANSWER = "[Private code provided]"
READINGS = 3  # times the memory agent may read notes in full before it must decide
# With learning off, a message is read only when it may ask to have something kept or forgotten: what the user
# asks for is still done, nothing else is picked up.
ASKS_TO_REMEMBER = re.compile(r"\b(remember|forget|keep in mind|note that|from now on|memor)", re.IGNORECASE)


def day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def today():
    return day(datetime.now(timezone.utc).timestamp())


def shown(memory, whole=True):
    """A memory as an agent reads it in full: its line (`description`), then what the line has no room for
    (`content`; many have none). Not `whole`: the line alone."""
    view = {"name": memory["name"], "type": memory["type"], "description": memory["description"]}
    if whole and memory["content"].strip():
        view["content"] = memory["content"]
    view["updated"] = day(memory["updated"])
    if memory["app"]:
        view["app"] = memory["app"]
    if expires(memory):
        view["valid_until"] = expires(memory)
    return view


def expires(memory):
    """The memory's last valid day (ISO), or "" for a lasting one. Rows from before the column have none."""
    return (memory["expires"] if "expires" in memory.keys() else "") or ""


def current(rows):
    """The memories that still hold: a trip or a stay is not recalled after its date."""
    now = today()
    return [m for m in rows if not expires(m) or expires(m) >= now]


# --- recall -----------------------------------------------------------------------------------------

# The kinds of memory (tools/memory.py says what each holds), in the order the index lists them: what changes
# least first, since the provider caches the prompt's unchanged prefix, and what is pending right now last.
SECTIONS = ("user", "person", "thing", "reference", "feedback", "app", "open")


def line(memory):
    """A memory as its line in its section of the index: its name (an app note with its package), what it holds,
    and its last day when it has one. For an open matter that day is when the note lapses, often thirty days
    from when it was written, and is worded so: read as "until", it was taken for the matter's deadline."""
    where = f" ({memory['app']})" if memory["type"] == "app" and memory["app"] else ""
    last = "note kept until" if memory["type"] == "open" else "until"
    until = f" ({last} {expires(memory)})" if expires(memory) else ""
    return f"{memory['name']}{where}: {' '.join((memory['description'] or '').split())}{until}"


def index(rows, whole=False):
    """The index: one line per memory, in sections by kind, oldest first within each, so a new memory adds a line
    at the end of its section. It is held to memory_index_lines and memory_index_chars (Claude Code loads the
    first 200 lines or 25 KB of its index, and nothing past that): when the memories outgrow it, open matters
    and then the least recently updated of the rest are left out, and the memory agent, which reads the `whole`
    index, is asked to merge and prune until it fits. Returns ({kind: lines}, how many are left out)."""
    kept, used = [], 0
    for memory in sorted(rows, key=lambda m: (m["type"] != "open", m["updated"]), reverse=True):
        size = len(line(memory)) + 1
        if len(kept) >= settings.memory_index_lines or used + size > settings.memory_index_chars:
            break
        kept.append(memory)
        used += size
    sections = {}
    for memory in sorted(rows if whole else kept, key=lambda m: m["created"]):
        sections.setdefault(memory["type"], []).append(line(memory))
    order = (*SECTIONS, *sorted(set(sections) - set(SECTIONS)))
    return {kind: sections[kind] for kind in order if kind in sections}, len(rows) - len(kept)


def crowded(rows):
    """The index is full or nearly so (nine tenths of either limit): time to merge and prune."""
    sections, left = index(rows, whole=True)
    lines = [x for section in sections.values() for x in section]
    return (bool(left) or len(lines) > 0.9 * settings.memory_index_lines
            or sum(len(x) + 1 for x in lines) > 0.9 * settings.memory_index_chars)


def about_user(store):
    """What the agents that talk to the user read: the index of the memories."""
    sections, left = index(current(store.memories()))
    if not sections:
        return {}
    if left:
        sections["not_listed"] = f"{left} older memories are not listed: the index is full"
    return {"memory_index": sections}


def lapse(store):
    """Memories past their last day are deleted: a trip that is over, a matter nobody renewed. No agent recalled
    them any more; now nothing keeps them either."""
    now = today()
    for memory in store.memories():
        if expires(memory) and expires(memory) < now:
            store.forget_memory(memory["name"])


def read(store, names, every=False):
    """The memories asked for by name, in full: {name: the memory, or a line saying there is none}. `every`:
    the ones past their day too (the memory agent's reading; nobody else recalls those)."""
    rows = store.memories()
    known = {m["name"]: m for m in (rows if every else current(rows))}
    return {name: shown(known[name]) if name in known else "There is no memory with this name: use a name from memory_index."
            for name in dict.fromkeys(names)}


def unknown(store, names):
    """The names among these that no current memory has."""
    known = {m["name"] for m in current(store.memories())}
    return [name for name in dict.fromkeys(names) if name not in known]


def for_step(store, app, names=()):
    """What a phone agent reads with its step: the memories its step names (the user's rules it must follow and
    the values it needs, chosen by the orchestrator from the index), then the newest notes on the step's app
    from earlier visits. The agent reads this on every turn, so the app's notes are given in full only while
    they fit memory_step_chars together, newest first; the ones past that are given as their line alone."""
    package = (app or {}).get("package")
    rows = current(store.memories())
    known = {m["name"]: m for m in rows}
    named = [name for name in dict.fromkeys(names or ()) if name in known]
    notes = [m for m in rows if m["type"] == "app" and package and m["app"] == package and m["name"] not in named]
    views, room = [shown(known[name]) for name in named], settings.memory_step_chars
    for note in notes[:settings.memory_app_notes]:
        room -= len(note["content"])
        views.append(shown(note, whole=room >= 0))
    return views


# --- learning ---------------------------------------------------------------------------------------

def apply(store, action, existing, chat_id=None, source="chat"):
    """Apply the memory agent's decision to the store: (saved rows, removed contents)."""
    saved, removed = [], []
    known = {m["name"]: m for m in existing}
    for name in dict.fromkeys(action.forget):
        if name in known and (row := store.forget_memory(name)):
            removed.append(row["content"])
    for note in action.save:
        fields = note.model_dump()
        if note.type == "open" and not note.expires:  # what is pending is never kept for good
            fields["expires"] = day(datetime.now(timezone.utc).timestamp() + settings.memory_open_days * 86400)
        old = known.get(note.name)
        if old and all(old[k] == fields[k] for k in ("type", "description", "content")) and (
                note.type == "open" or expires(old) == fields["expires"]):
            continue
        saved.append(store.save_memory(**fields, source=source, chat_id=chat_id))
    return saved, removed


def context_for(store, **extra):
    lapse(store)
    existing = store.memories()
    context = {
        "today": store.local_day(datetime.now(timezone.utc).timestamp(), store.zone()),  # the user's day, not UTC's
        "memory_index": index(existing, whole=True)[0],  # every memory's line: it is the one that prunes them
        **extra,
    }
    if crowded(existing):
        context["consolidate"] = True
    return context, existing


async def decided(ask, context, store):
    """The memory agent's decision (its `remember`), once it has read the notes it asked for: it sees the index,
    and reads in full with `read_memory` the ones it means to replace, merge or forget. `ask(context)` is the
    model call. A few readings at most; one that never decides fails the pass, which is tried again later."""
    for _ in range(READINGS + 1):
        action = (await ask(context)).actions[0]
        if not isinstance(action, ReadMemory):
            return action
        context["memories_read"] = {**context.get("memories_read", {}), **read(store, action.names, every=True)}
    raise RuntimeError("the memory agent kept reading memories and never decided")


async def learn(runner, state):
    """Keep what will help later: durable facts and wishes from the user's new messages, and app know-how
    from the subagents' lessons. With learning off ("Learn from my tasks"), only what the user plainly asks to
    have remembered or forgotten, and the messages are marked read either way, so turning learning back on
    never picks up what was said while it was off. Best effort: it never fails the run, and what it could not
    read is read again after the next run; a failure is logged and shown in the trace."""
    store = runner.store
    try:
        auto = store.profile_get("auto_memory", True)
        chat_id = state["chat_id"]
        since = scanned(store, chat_id)
        new = store.rows(
            # Only the user's own words: a notification passed to the chat (role notification) is someone else's.
            "SELECT content,created FROM messages WHERE chat_id=? AND role='user' AND created>? ORDER BY created",
            (chat_id, since),
        )
        messages = [m["content"] for m in new if m["content"] not in (PRIVATE_ANSWER, PRIVATE_MARK)]
        if not auto and not any(ASKS_TO_REMEMBER.search(m) for m in messages):
            messages = []
        lessons = state.get("lessons", [])[state.get("lessons_learned", 0):] if auto else []
        saved, removed, declined = [], [], []
        if messages or lessons:
            context, existing = context_for(
                store, new_user_messages=messages,
                recent_conversation=store.conversation(chat_id, limit=10, before=since),
                **({} if auto else {"explicit_only": True}),
            )
            if lessons:
                context["task"] = {
                    "request": state["request"], "status": state["status"], "lessons": lessons,
                    "journal": [j["line"] for j in state.get("journal", [])][-30:],
                }
            usage = runner.on_usage(state, {"stage": MEMORY.name})
            action = await decided(lambda c: runner.model.ask(MEMORY, c, on_usage=usage, **provider_of(state, MEMORY)), context, store)
            saved, removed = apply(store, action, existing, chat_id=chat_id)
            declined = action.declined
        if new:
            store.execute("INSERT OR REPLACE INTO memory_scans VALUES (?,?)", (chat_id, new[-1]["created"]))
        if lessons:
            state["lessons_learned"] = len(state["lessons"])
            # Only this field: the run may already be working again (the worker runs this in the background).
            store.set_run_field(state["id"], "lessons_learned", state["lessons_learned"])
        if saved or removed or declined:
            store.event(state["id"], "memories_updated", {"added": saved, "removed": removed, "declined": declined})
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # best effort; the next run reads the same messages and lessons again
        log.warning("memory pass failed: %s", safe_error(exc))
        store.event(state["id"], "memory_failed", {"description": safe_error(exc)})


def scanned(store, chat_id):
    """When the memory last read this chat's messages (0: never)."""
    rows = store.rows("SELECT scanned FROM memory_scans WHERE chat_id=?", (chat_id,))
    return rows[0]["scanned"] if rows else 0


async def remember_now(runner, state, request):
    """The user only asked, in the chat, to remember, change or forget something: done now, before the reply,
    whatever the learning setting (it governs what Relay picks up by itself, not what it is asked). Returns
    what the gate answers from: {saved, forgotten, declined} or {failed}. The message is marked read when it is
    the only unread one, so the pass after the run does not take it again."""
    store = runner.store
    chat_id = state["chat_id"]
    try:
        context, existing = context_for(
            store, user_asked=request, recent_conversation=store.conversation(chat_id, limit=6),
        )
        usage = runner.on_usage(state, {"stage": MEMORY.name})
        action = await decided(lambda c: runner.model.ask(MEMORY, c, on_usage=usage, **provider_of(state, MEMORY)), context, store)
        saved, removed = apply(store, action, existing, chat_id=chat_id)
        unread = store.rows(
            "SELECT created FROM messages WHERE chat_id=? AND role='user' AND created>? ORDER BY created",
            (chat_id, scanned(store, chat_id)),
        )
        if len(unread) == 1:
            store.execute("INSERT OR REPLACE INTO memory_scans VALUES (?,?)", (chat_id, unread[0]["created"]))
        if saved or removed or action.declined:
            store.event(state["id"], "memories_updated", {"added": saved, "removed": removed, "declined": action.declined})
        outcome = {"saved": [m["description"] for m in saved], "forgotten": removed, "declined": action.declined}
        if not (saved or removed or action.declined):
            outcome["unchanged"] = "The memory already held this, or there was nothing to forget: nothing changed."
        return outcome
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("memory update failed: %s", safe_error(exc))
        store.event(state["id"], "memory_failed", {"description": safe_error(exc)})
        return {"failed": "The memory could not be updated just now. Say so plainly: the user can ask again, or add it "
                          "in the memory panel (What penrose remembers)."}


async def remember_findings(runner, state, findings):
    """The orchestrator commits what the task found about the user's world (its `remember` tool), during the task,
    so a provider failure later loses only what came after. The memory agent decides what sticks, under its rules.
    Returns one line for the journal: what was kept, declined, or why nothing was."""
    store = runner.store
    try:
        context, existing = context_for(
            store, task_findings=findings, task={"request": state["request"], "status": state["status"]},
            recent_conversation=store.conversation(state["chat_id"], limit=6),
        )
        usage = runner.on_usage(state, {"stage": MEMORY.name})
        action = await decided(lambda c: runner.model.ask(MEMORY, c, on_usage=usage, **provider_of(state, MEMORY)), context, store)
        saved, removed = apply(store, action, existing, chat_id=state["chat_id"])
        if saved or removed or action.declined:
            store.event(state["id"], "memories_updated", {"added": saved, "removed": removed, "declined": action.declined})
        parts = []
        if saved:
            parts.append("kept " + ", ".join(m["name"] for m in saved))
        if removed:
            parts.append(f"forgot {len(removed)}")
        if action.declined:
            parts.append("declined " + "; ".join(action.declined))
        return "; ".join(parts) if parts else f"nothing new to keep from {len(findings)} findings"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("memory update failed: %s", safe_error(exc))
        store.event(state["id"], "memory_failed", {"description": safe_error(exc)})
        return f"could not be updated now ({safe_error(exc)[:120]}); try again later in the task"


def provider_of(state, agent):
    """The keyword arguments that put `agent`'s model call on the run's provider, model and effort."""
    return route(state, agent)


async def add(model, store, text, provider="deepseek", agent=None):
    """A note the user writes in the panel, placed by the memory agent (named, typed, merged with what is
    there). When the agent cannot run, the note is kept as the user wrote it, so
    nothing the user typed is lost, unless the agent declined it (that is never kept). Returns (saved rows,
    removed contents, placed by the agent?, what was declined and why)."""
    context, existing = context_for(store, user_added=text)
    try:
        action = await decided(lambda c: model.ask(MEMORY, c, **chosen(provider, agent)), context, store)
    except Exception as exc:
        log.warning("memory agent could not place the user's note: %s", safe_error(exc))
        return [store.save_memory(**plain_note(store, text), source="user")], [], False, []
    saved, removed = apply(store, action, existing, source="user")
    if action.declined:  # never kept, even when the user writes it (a card number, immigration status)
        return saved, removed, True, action.declined
    if not saved and not action.forget:  # the agent kept nothing: the user's own words are kept as they are
        saved = [store.save_memory(**plain_note(store, text), source="user")]
    return saved, removed, True, []


def chosen(provider, agent=None):
    """The model for a change made in the panel: the agent the user picked in the composer ({provider, model,
    effort}, what their tasks' memory runs on), else the provider's own."""
    if agent:
        return route({"models": {"agent": agent}}, MEMORY)
    return {"provider": provider} if provider in ("openai", "anthropic") else {}


def plain_note(store, text):
    """A user memory named after its first words, with a free name."""
    base = "-".join(re.findall(r"[a-z0-9]+", text.lower())[:6])[:50] or "note"
    taken = {m["name"] for m in store.memories()}
    name = next(n for n in (base, *(f"{base}-{i}" for i in range(2, 1000))) if n not in taken)
    return {"name": name, "type": "user", "description": text[:150], "content": text[:600] if len(text) > 150 else ""}
