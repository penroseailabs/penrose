"""Sorting a briefing: once it has gone quiet, an agent with no tools (TRIAGE) reads it beside what penrose knows
about the user and says whether it is noise, a line for the list, or worth a briefing, with a headline in
penrose's words, what is asked of the user, the things said that matter, and the words the matter goes by. It
reads other people's words and can start nothing, so what an app shows can at worst put a line in the list.

The worker runs one Sorter beside the notification intake (app/worker.py).
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque

from ..agents.catalog import route
from ..agents.registry import TRIAGE
from ..pricing import cost
from ..runtime.memory import about_user
from . import groups, policy

log = logging.getLogger(__name__)

# A briefing is sorted once nothing has come for QUIET_SECONDS, or MAX_WAIT_SECONDS after the first notification
# not yet looked at: a conversation that never pauses is still sorted.
QUIET_SECONDS, MAX_WAIT_SECONDS = 60, 300
# One that was sorted is not sorted again sooner than this, however much it says meanwhile: a group that never
# stops talking is read a few times an hour, not every few minutes.
RESORT_SECONDS = 900
# At most this many sorts an hour, whatever arrives.
SORTS_PER_HOUR = 40
# A sort that failed is tried again after RETRY_SECONDS, then four times as long each time, up to MAX_RETRY_SECONDS.
# After MAX_TRIES the briefing stays listed as it came, until more arrives for it.
RETRY_SECONDS, MAX_RETRY_SECONDS, MAX_TRIES = 120, 3600, 3
# The sorter never rests for less than this: a clock that is off must not make it spin.
MIN_REST_SECONDS = 1
# How much of a briefing the sorting agent reads: its newest notifications, and a few from before it.
READ, READ_EARLIER = 12, 6
# The agent's requests are kept under this id: they belong to no chat.
LEDGER = "briefings"


class NotAsked(Exception):
    """The sorting agent could not be asked (no key, the provider is down): the briefing is listed as it came and
    is still due a sort."""


def due(store, now):
    """(the briefings due a sort, the newest first; the seconds until the next one is due, or None when none
    waits)."""
    rows = store.rows(
        "SELECT * FROM briefings WHERE dismissed IS NULL AND chat_id IS NULL AND count>sorted_count AND latest>? ORDER BY latest DESC",
        (now - groups.RECENT_SECONDS,))
    ready, wait = [], None
    for row in rows:
        at = min(row["latest"] + QUIET_SECONDS, (row["pending"] or row["began"]) + MAX_WAIT_SECONDS)
        if row["sorted"]:
            at = max(at, row["sorted"] + RESORT_SECONDS)
        if at <= now:
            ready.append(row)
        else:
            wait = at - now if wait is None else min(wait, at - now)
    return ready, wait


def picks(store):
    """What a sort, which belongs to no task, runs on: the models the user chose for notifications (policy.models),
    else those of their latest task. A sort is on the fast tier: the agent's provider, its cheap model."""
    if chosen := policy.models(policy.load(store)):
        return {"provider": chosen["agent"]["provider"], "models": chosen}
    return last_pick(store)


def last_pick(store):
    """The provider and models of the user's latest task."""
    rows = store.rows("SELECT json_extract(state,'$.provider') AS provider, json_extract(state,'$.models') AS models "
                      "FROM runs ORDER BY updated DESC LIMIT 1")
    if not rows:
        return {}
    models = json.loads(rows[0]["models"]) if rows[0]["models"] else None
    return {"provider": rows[0]["provider"], **({"models": models} if models else {})}


def as_it_came(notes):
    """What a briefing is listed as while the agent has not said: its latest notification, as the app showed it."""
    last = notes[-1] if notes else {}
    line = " ".join(f"{last.get('sender') or last.get('title') or ''}: {last.get('text') or ''}".split()).strip(": ")
    return line[:90] or "New notifications"


def context(store, briefing, notes):
    """What the sorting agent reads: the burst, what the same conversation showed before, what penrose knows about
    the user, and its own earlier verdict when more has arrived since."""
    about = about_user(store)
    found = {
        "locale": store.user_locale(),
        **{k: about[k] for k in ("memory_index",) if k in about},
        "from": groups.origin(briefing),
        "notifications": [groups.as_read(store, n, 400) for n in notes],
    }
    if briefing["count"] > len(notes):
        found["more_before_these"] = briefing["count"] - len(notes)
    if before := groups.earlier(store, briefing, READ_EARLIER):
        found["earlier_from_it"] = [groups.as_read(store, n, 160) for n in before]
    if briefing["sorted"]:
        found["sorted_before_as"] = {"verdict": briefing["verdict"], "headline": briefing["headline"]}
    return found


def keep_usage(store):
    """A callback for the model client that keeps the request's usage and cost under LEDGER. Never fails a sort."""
    def record(provider, model, usage, created=None):
        with contextlib.suppress(Exception):
            at = created or time.time()
            credits, peak = cost(provider, model, usage, at)
            store.record_usage(LEDGER, None, None, TRIAGE.name, provider, model, usage, at, credits, peak)
    return record


# A briefing the user has not opened or dismissed: only such a one is written by a sort, which reads it, asks a
# model, and writes seconds later.
UNTOUCHED = "chat_id IS NULL AND dismissed IS NULL"


async def sort(runner, briefing_id, last=False):
    """Have the sorting agent look at a briefing and keep what it said. Returns the verdict, or None when there is
    nothing to sort (the user opened or dismissed it meanwhile, or nothing new has come).

    When the agent cannot be asked, the briefing is listed as its latest notification, so nothing that came in is
    lost, and NotAsked is raised: it is still due. With `last` (the caller's final try) it is left listed as it
    is instead, until more arrives for it."""
    store = runner.store
    briefing = groups.find(store, briefing_id)
    if not briefing or briefing["chat_id"] or briefing["dismissed"] or briefing["count"] <= briefing["sorted_count"]:
        return None
    notes = groups.notes_of(store, briefing, READ)
    asked = True
    try:
        turn = await runner.model.ask(TRIAGE, context(store, briefing, notes), on_usage=keep_usage(store),
                                      **route(picks(store), TRIAGE))
        said = turn.actions[0]
        verdict, headline, why = said.verdict, " ".join(said.headline.split())[:120], " ".join(said.why.split())[:240]
        search, urgent = [" ".join(s.split())[:60] for s in said.search if s.strip()][:4], bool(said.urgent)
        asks, gist = " ".join(said.asks.split())[:200], [" ".join(g.split())[:160] for g in said.gist if g.strip()][:4]
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - whatever stopped the agent, the briefing is still listed
        log.warning("a briefing could not be sorted: %s", exc)
        asked = False
        verdict, headline = briefing["verdict"] or groups.LIST, briefing["headline"] or as_it_came(notes)
        why, search, urgent = briefing["why"] or "", json.loads(briefing["search"] or "[]"), bool(briefing["urgent"])
        asks, gist = briefing["asks"] or "", json.loads(briefing["gist"] or "[]")
    now = time.time()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if asked or last:
            db.execute(
                # What came while it was being sorted waits for the next quiet moment, counted from now.
                "UPDATE briefings SET sorted=?,sorted_count=?,pending=CASE WHEN count>? THEN ? ELSE NULL END,verdict=?,"
                f"headline=?,why=?,search=?,urgent=?,asks=?,gist=? WHERE id=? AND {UNTOUCHED}",
                (now if asked else briefing["sorted"], briefing["count"], briefing["count"], now, verdict, headline, why,
                 json.dumps(search), int(urgent), asks, json.dumps(gist), briefing_id))
        else:
            db.execute(f"UPDATE briefings SET verdict=?,headline=? WHERE id=? AND {UNTOUCHED}", (verdict, headline, briefing_id))
    store.changed()  # the screens that show the list hear of it
    if not asked and not last:
        raise NotAsked(briefing_id)
    return verdict


class Sorter:
    """In the worker: sorts each briefing once it has gone quiet. `poke` when a notification joined one; `listen`
    (the worker's doorbell, bell.Signal.listen) gives the event that says the API wrote something, which is how
    it hears that the user chose an app (actions.choose_app takes in what the app showed today). `on_brief` is
    called when a sort finds one worth the user's attention, which the worker then prepares (prepare.py)."""

    def __init__(self, runner, listen=None, on_brief=None):
        self.runner, self.listen, self.on_brief = runner, listen, on_brief
        self.news = asyncio.Event()
        self.times = deque()  # when each sort of the last hour began
        self.retry = {}  # briefing id -> (when a failed sort may be tried again, how often it has failed)

    def poke(self):
        self.news.set()

    def allowance(self, now):
        """The seconds until another sort fits in the hour (0: now)."""
        while self.times and self.times[0] <= now - 3600:
            self.times.popleft()
        return 0 if len(self.times) < SORTS_PER_HOUR else self.times[0] + 3600 - now

    async def turn(self):
        """Sort what is due. Returns the seconds until there may be more to do, or None when nothing waits."""
        now = time.time()
        ready, wait = due(self.runner.store, now)
        waits = [] if wait is None else [wait]
        self.retry = {b["id"]: self.retry[b["id"]] for b in ready if b["id"] in self.retry}  # the rest are settled
        for briefing in ready:
            again, tries = self.retry.get(briefing["id"], (0, 0))
            if again > now:
                waits.append(again - now)
                continue
            if pause := self.allowance(now):
                waits.append(pause)
                break
            self.times.append(now)
            try:
                verdict = await sort(self.runner, briefing["id"], last=tries + 1 >= MAX_TRIES)
                self.retry.pop(briefing["id"], None)
                if verdict == groups.BRIEF and self.on_brief:
                    self.on_brief()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not isinstance(exc, NotAsked):
                    log.exception("could not sort briefing %s", briefing["id"])
                pause = min(RETRY_SECONDS * 4 ** tries, MAX_RETRY_SECONDS)
                self.retry[briefing["id"]] = (now + pause, tries + 1)
                waits.append(pause)
        return min(waits) if waits else None

    async def rest(self, seconds, rung=None):
        """Until a notification joins a briefing, the API has news, or `seconds` pass (None: as long as it takes)."""
        waits = [asyncio.ensure_future(self.news.wait()), *([asyncio.ensure_future(rung.wait())] if rung else [])]
        try:
            await asyncio.wait(waits, timeout=None if seconds is None else max(seconds, MIN_REST_SECONDS),
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

    async def run(self):
        """For good: never fails the worker."""
        while True:
            self.news.clear()
            rung = self.listen() if self.listen else None  # before the read, so news during it still counts
            try:
                wait = await self.turn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("the briefings could not be read")
                wait = RETRY_SECONDS
            await self.rest(wait, rung)
