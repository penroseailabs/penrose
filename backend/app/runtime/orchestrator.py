"""The orchestrator's loop: decide, do it (run a step, verify one, ask, finish), read what came back, decide again.

Each orchestrator turn reads the plan, the facts, the journal and the latest report: never a subagent's
turns. A subagent starts fresh on every step; when it ends, its report is all that comes back, and a line in
the journal. That keeps the orchestrator's context small and on the goal, and each subagent's on its step.

The orchestrator starts fresh on every turn too: none of a turn's reasoning reaches the next one. What a turn
worked out that the plan, the facts and the journal do not show (what it found itself, what it ruled out and
why, a lead it did not follow) it keeps with `note`; its notes are read back on every later turn, and by the
task that follows this one in the chat.
"""

import asyncio
import contextlib

import httpx

from .. import attachments, notifications, watches
from .. import youtube as yt
from ..agents.llm import Parts
from ..agents.registry import HANDOVER, ORCHESTRATOR, SUBAGENT, VERIFIER
from ..config import settings
from ..errors import DeviceError, NeedUser, NewMessage, Rejected, safe_error
from ..plan import find, journal_view, log, next_step, plan_view, step_view
from ..skills import for_app, index, recipes
from ..tools.orchestrator import (
    GATHER,
    NEXT,
    AskUser,
    Finish,
    NoteToSelf,
    PutOnPhone,
    RememberFindings,
    RunStep,
    SetPlan,
    gmail_search,
)
from ..tools.recall import searched
from ..tools.subagent import Report
from ..tools.verifier import Verdict
from .clock import CLOCK_KEYS
from .call import Call, transcript_view
from .memory import for_step, read, remember_findings, unknown
from .phone import PhoneAgent

# What may come before a turn's last tool.
PREP = (SetPlan, RememberFindings, PutOnPhone, NoteToSelf)
NOTES_KEPT = 40  # the orchestrator's notes to itself that a task keeps; a later one drops the oldest
MEMORIES_READ = 16  # memories read in full that a task keeps before it; a later reading drops the oldest
# Of what every agent that talks to the user knows (runner.base_context), what changes while a task runs: the
# conversation gains a line with every reply, a step puts a file on the phone, the user gives a value privately.
# The orchestrator reads these after what stays the same for the whole task (decide).
CHANGING = ("conversation", "request", "phone_files", "private_entries")
# A report's status -> the step's. unfinished: the runtime ended the session and the subagent handed over.
REPORTED = {"completed": "done", "failed": "failed", "needs_input": "needs_input", "unfinished": "unfinished"}


MAX_RESTARTS = 2  # decisions started over in a row for the user's new messages
LOOKUP_CHARS = 3000  # of one query's phone results, and of its Google results: the best matches come first
# Gmail was asked for a query and its servers gave no answer (the home app's `mail`): what the look-up says then,
# in a field of its own, so "no such email" and "could not look" are never read as the same thing.
GMAIL_NOT_SEARCHED = {
    "phone_only": "Gmail's servers did not answer, so only the emails stored on the phone are listed (the recent ones)",
    "failed": "Gmail's servers did not answer",
    "pending": "Gmail had not answered in time",
}
GMAIL_UNKNOWN = ("NOT SEARCHED: {why}. This does not mean there is no such email: whether there is one is unknown. "
                 "Look these words up again before you rely on it; if Gmail still cannot be searched, tell the user "
                 "you could not see their email, never that there is none.")


def installed_lines(apps):
    """The phone's apps as a phone agent reads them, one a line: the name, then the package ID
    ("Maps com.google.android.apps.maps"); the ID alone when the phone gave no names."""
    return [f"{a['name']} {a['package']}" if a.get("name") else a["package"] for a in apps]


def clipped(text, limit=LOOKUP_CHARS):
    """The first lines of a search's results that fit, saying how many more there were."""
    if len(text) <= limit:
        return text
    lines, kept, size = text.splitlines(), [], 0
    for line in lines:
        if size + len(line) > limit:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept) + f"\n({len(lines) - len(kept)} more; search narrower words to see them)"


async def stopped(task):
    """`task` cancelled if it still runs, and waited for: its own cleanup (a hang-up, a released line) is done."""
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        if (current := asyncio.current_task()) and current.cancelling():
            raise  # this task is being stopped itself: that goes on up
    except Exception:  # noqa: BLE001 - its outcome is no longer wanted
        pass


def previous_attempt(step):
    """What the last attempt at a step got to, for the next subagent: what it was asked, its report, its
    last checkpoint (what it found and what did not work), its log, the screens it went through and its last
    actions. A step rewritten before it ran again keeps the
    attempt it had."""
    mem = (step.get("sessions") or {}).get(SUBAGENT.prompt) or {}
    if not (step.get("report") or mem):
        return step.get("previous_attempt") or {}
    attempt = {"goal": step.get("goal"), "app": (step.get("app") or {}).get("name"), "report": step.get("report"),
               "problem": step.get("problem"), "checkpoint": mem.get("checkpoint"), "progress": mem.get("progress"),
               "log": mem.get("log"), "notes": mem.get("notes"),
               "screens": mem.get("trail"), "last_actions": (mem.get("actions") or [])[-6:]}
    return {k: v for k, v in attempt.items() if v}


class Orchestrator:
    def __init__(self, runner, state, hold=None):
        self.runner, self.state = runner, state
        self.latest = None  # what the last step, verification or user message brought
        self.done = False
        self.only_prepared = False  # the last turn only prepared (remember, put_on_phone): the next one must move
        # Started beside the gate (Runner.advance): its first decision stays unseen and undone until this is set.
        self.hold = hold

    # --- the loop -----------------------------------------------------------------------------------

    def hear(self):
        """The user's new messages, once it is handed the run: a head start the gate cancels leaves them in the
        inbox, to go out as the next message (delivery.settle)."""
        if self.hold is None or self.hold.is_set():
            return self.runner.hear(self.state)
        return []

    async def drive(self):
        state = self.state
        state.setdefault("plan", [])
        # A step the user just unblocked (a private entry, the Google card) continues before anything else,
        # unless the user wrote meanwhile: then the orchestrator reads that first.
        resumed = next((s for s in state["plan"] if s["status"] == "resume"), None)
        if resumed and not (self.hear() or state.get("inbox")):
            await self.run_step(resumed["id"], "")
        for n in range(1, settings.max_orchestrator_turns + 1):
            self.hear()
            if any(s["status"] == "waiting_user" for s in state["plan"]) and not state.get("inbox"):
                return self.runner.wait(state)  # a subagent waits for the user's private entry
            # The phone stays awake while it decides: a decision takes up to minutes of thinking and most often
            # ends in a look-up or a step, and a phone that slept meanwhile is slow for its first searches.
            async with self.runner.awake(state):
                turn = await self.decide(must_conclude=n == settings.max_orchestrator_turns)
            for action in turn.actions:
                await action.run(self)
            if self.done:
                return
        return self.runner.wait(state)

    async def decide(self, must_conclude=False, restarts=0):
        state = self.state
        state.pop("private_asked", None)  # a question asked privately is answered, or overtaken, by now
        state.pop("options", None)  # and so are the answers it offered
        state.pop("needs", None)  # and the line it waited in the user's list with
        self.hear()  # what the user wrote while the last step worked
        trigger = state.pop("trigger", None) or ("user_message" if state.get("inbox") else "step_reported")
        if state.get("inbox"):
            state["phone_seconds"] = 0  # the user wrote: a fresh budget for what they ask now
            state.pop("looked_up", None)  # and fresh look-ups: what it found is in the context and facts by now
            state.pop("youtube", None)  # and the videos it read: the answer is in the conversation by now
            # A task a notification woke may use only its watch's apps; the user's own message lifts that.
            await self.runner.unlimit(state)
            state.pop("preparing", None)  # and a briefing they answered is an ordinary task from here,
            state.pop("background", None)  # theirs, which gives way to nothing (briefings/prepare.py)
        used = state.get("phone_seconds", 0) / 60
        must_conclude = must_conclude or used >= settings.task_phone_minutes
        # In the order of how long each field stays the same: the provider caches the prompt's unchanged prefix, so
        # a field that changes costs every field after it on every call (on Claude it is written to the cache
        # again, which costs more than reading it fresh). In parts (llm.Parts):
        # - the skills index and the task recipes: the same for every chat and every turn. They end a part of
        #   their own, cached on Claude, and both are sent on every turn: recipes left out once there was a plan
        #   moved everything after them, which was then written again, at many times what reading them costs;
        # - the user, and what this chat holds for the whole task;
        # - the lists that only grow, each cut item by item so only a new item is written: the videos, the
        #   look-ups, then the conversation, which gains a line with every reply. Whatever stands after a list
        #   that grew is written again, so the shortest additions come last: a reply is a line, a look-up pages;
        # - what a step or the user changes (CHANGING, the watches), then the task's own state, whose plan
        #   changes on every turn, with the clock last.
        base = self.runner.base_context(state)
        clock = {k: base.pop(k) for k in CLOCK_KEYS if k in base}
        changing = {k: base.pop(k) for k in CHANGING if k in base}
        context = Parts()
        if skills := index():
            context["skills"] = skills
        if known := recipes():
            context["recipes"] = known
        context.end()
        context.update(base)
        if state.get("woken"):  # a notification started this task (watches.wake): what it waited for, and its limits
            context["woken_by"] = {**state["woken"], "limited_to_apps": bool(state.get("scope"))}
        if state.get("briefing"):  # what arrived, and the earlier conversations on the same matter (app/briefings/dossier.py)
            context["briefing"] = {**state["briefing"], "preparing": bool(state.get("preparing")),
                                   "on_your_own": bool(state.get("background"))}
        # The videos before the look-ups: transcripts are the bulk and are read once or twice, look-ups are
        # small and come many times.
        for gathered in ("youtube", "looked_up"):
            if state.get(gathered):
                context[gathered] = state[gathered]
                context.grows(gathered)
        context["conversation"] = changing.pop("conversation", [])
        context.grows("conversation")
        context.update(changing)
        # What this conversation waits for (app/watches.py), so a turn neither sets one twice nor leaves one that is over.
        if watching := watches.open_for(self.runner.store, state["chat_id"]):
            context["watching"] = [{k: w[k] for k in ("id", "what", "match", "then", "apps", "until") if k in w} for w in watching]
        if state.get("memories_read"):  # by name, read now: a memory changed since is read as it stands
            context["memories_read"] = read(self.runner.store, state["memories_read"])
        context.update({
            "context": state.get("context", ""),
            "facts": state.get("facts", {}),
            "journal": journal_view(state),
            **({"notes": state["notes"]} if state.get("notes") else {}),  # its own, from earlier turns (note)
            "plan": plan_view(state),
        })
        if state.get("pictures"):
            context["pictures"] = state["pictures"]
        if not state["plan"] and (previous := self.runner.previous_task(state)):
            context["previous_task"] = previous
        context.update({
            "trigger": trigger,
            "new_user_messages": state.get("inbox", []),
            # What apps on the phone showed since the last decision, passed to this chat (app/notifications.py).
            **({"new_notifications": state["notifications"]} if state.get("notifications") else {}),
            "latest": self.latest,
            "phone_location": await self.runner.phone_location(state),
            "phone_time": {"used_minutes": round(used, 1), "budget_minutes": settings.task_phone_minutes},
            "must_conclude": must_conclude,
            **clock,
        })

        def check(turn):
            moves = [a for a in turn.actions if not isinstance(a, NoteToSelf)]
            plans = [a for a in moves if isinstance(a, SetPlan)]
            if plans and all(isinstance(a, (SetPlan, RememberFindings)) for a in moves):
                # A new plan with nothing to start: start its first step, which is the only valid run_step anyway.
                # Findings kept beside the plan (remember) leave nothing the step waits for. A file put on the
                # phone does (its path goes in the step's note), so that turn is left to the next decision.
                plan = state["plan"]
                for action in plans:
                    plan = action.merged(plan)
                if first := next_step(plan):
                    turn.actions.append(RunStep(step=first["id"]))
            *prep, last = turn.actions
            # The tools that only gather (look_up, youtube, read_memory) may end a turn together, each once: a
            # model that calls tools asks for a search and a video at once, and refusing that costs a whole call.
            # Each runs, and the next decision reads what all of them found.
            gather = []
            if isinstance(last, GATHER):
                while prep and isinstance(prep[-1], GATHER):
                    gather.insert(0, prep.pop())
                if len({type(a) for a in (*gather, last)}) <= len(gather):
                    raise Rejected("One look_up, one youtube and one read_memory at most in a turn: put every "
                                   "query in the one look_up and every video in the one youtube.")
            # A turn that only prepares (keeps findings or notes, puts files on the phone) is a turn: it is done and the next
            # decision reads what came of it, as a model that calls tools expects. Once, so it still ends in a move.
            only_prepares = isinstance(last, PREP) and not self.only_prepared and not must_conclude
            if only_prepares:
                prep = turn.actions
            if (not only_prepares and not isinstance(last, NEXT)) or any(not isinstance(a, PREP) for a in prep):
                raise Rejected("End the turn with exactly one of run_step, verify_step, ask_user or finish, or with "
                               "look_up, youtube and read_memory (one of them, or several together); only set_plan, "
                               "remember, put_on_phone and note may come before.")
            if sum(isinstance(a, SetPlan) for a in prep) > 1:
                raise Rejected("At most one set_plan in a turn.")
            plan = state["plan"]
            for action in prep:
                if isinstance(action, SetPlan):
                    action.check(plan, state)
                    named = [name for step in action.steps for name in step.memories]
                    if missing := unknown(self.runner.store, named):
                        raise Rejected(f"No memory is named {', '.join(missing)}. A step's `memories` are names from "
                                       "`memory_index` (the first word of a line, such as 'home-address'): name ones "
                                       "that are listed, or leave it empty.")
                    plan = action.merged(plan)
                else:
                    action.check(plan, state)
            if only_prepares:
                return
            for action in gather:
                action.check(plan, state)
            last.check(plan, state)
            if must_conclude and not isinstance(last, (AskUser, Finish)):
                raise Rejected("This is your last turn for now: ask_user or finish.")

        for attempt in range(3):
            try:
                # A message the user sends while it thinks overtakes the decision: it starts over with the
                # message (a few times at most, so a stream of messages still ends in a decision).
                photos = attachments.images_of(self.runner.store, state.get("attached"))
                turn = await self.runner.call(state, ORCHESTRATOR, context, check=check, heeds=restarts < MAX_RESTARTS,
                                              hold=self.hold, images=photos or None)
                state.pop("attached", None)  # looked at: the next decision does not carry them again
                self.hold = None
                break
            except NewMessage:
                state["trigger"] = trigger
                return await self.decide(must_conclude, restarts + 1)
            except Rejected as exc:
                if attempt == 2:
                    raise Rejected(
                        "penrose could not decide its next move (the planner's answer was invalid three times). "
                        "Send your message again to retry."
                    ) from exc
                context["rejected_decision"] = f"Your previous decision could not be applied: {exc} Decide again."
                if attempt == 1:
                    # Two decisions that could not be applied: the third ends the turn with what there is (finish, or
                    # ask the user) rather than the run failing and a resend landing on the same wall.
                    must_conclude = context["must_conclude"] = True
                    context["rejected_decision"] += " Twice now: ask_user or finish, with what the task has done so far."
        self.only_prepared = isinstance(turn.actions[-1], PREP)
        if self.only_prepared:
            # Nothing it was told is used up yet: the next decision reads the same messages and the same report,
            # with what the preparing left in the journal.
            state["trigger"] = trigger
            self.runner.emit(state, "orchestrated", {"next": turn.actions[-1].tool, "summary": turn.summary})
            return turn
        for message in state.get("inbox", []):
            log(state, f"user: {message}")
        state["inbox"] = []
        for note in state.pop("notifications", []):
            log(state, f"notification: {note}")
        self.latest = None
        self.runner.emit(state, "orchestrated", {"next": turn.actions[-1].tool, "summary": turn.summary})
        return turn

    # --- what the tools do --------------------------------------------------------------------------

    def set_plan(self, tool):
        state = self.state
        before = {s["id"]: s for s in state["plan"]}
        state["plan"] = tool.merged(state["plan"])
        for step in state["plan"]:  # a step rewritten under its id keeps what its last attempt found
            if (old := before.get(step["id"])) is not None and old is not step and (attempt := previous_attempt(old)):
                step["previous_attempt"] = attempt
        if tool.context:
            state["context"] = tool.context
        log(state, "plan: " + " | ".join(f"{s.id}: {s.goal[:80]}" for s in tool.steps))
        self.runner.emit(state, "dag", {"summary": state.get("context", ""), "nodes": [step_view(s) for s in state["plan"]]})

    async def look_up(self, queries, email=False, apps=False):
        """Search the phone (the Relay home app; Gmail too with `email`), Google (in the phone's Chrome), Google
        Play with `apps` (which apps do this, and which of them the user has), the user's own browsing on their
        other devices (Chrome's history on the phone), earlier conversations and the notifications the phone
        received for each query, the phone's searches at the same time; the next decision reads what came back as
        `looked_up`. A source that cannot be searched says why; the others still count."""
        state = self.state
        # A Gmail search (operators) is for Gmail alone: the other sources would read its operators as words.
        mail_only = {q for q in queries if gmail_search(q)}
        plain = [q for q in queries if q not in mail_only]
        email = email or bool(mail_only)
        phone, web, play, browsed, problem, web_problem, play_problem, history_problem = {}, {}, {}, {}, None, None, None, None
        if not (slot := await self.runner.search_slot(state)):
            problem = web_problem = play_problem = "no phone is running"
        else:
            devices = self.runner.devices
            store = [devices.play_search(slot, plain)] if apps and plain else []
            home, google, visited, *store = await asyncio.gather(
                devices.home_search(slot, queries, email=email),
                devices.web_search(slot, plain) if plain else asyncio.sleep(0, []),
                devices.history_search(slot, plain) if settings.history_lookup and plain else asyncio.sleep(0, []),
                *store, return_exceptions=True)
            if store and isinstance(store[0], BaseException):
                if not isinstance(store[0], DeviceError):
                    raise store[0]
                play_problem = safe_error(store[0])
            elif store:
                play = {r["query"]: r for r in store[0]}
            if isinstance(visited, BaseException):
                if not isinstance(visited, DeviceError):
                    raise visited
                history_problem = safe_error(visited)
            else:
                browsed = {r["query"]: r for r in visited}
            if isinstance(home, BaseException):
                if not isinstance(home, DeviceError):
                    raise home
                problem = safe_error(home)
            else:
                phone = {r["query"]: r for r in home}
            if isinstance(google, BaseException):
                if not isinstance(google, DeviceError):
                    raise google
                web_problem = safe_error(google)
            else:
                web = {r["query"]: r for r in google}
        results, chats, unseen = [], {}, []
        for query in queries:
            result = {"query": query}
            answer = phone.get(query, {})
            if problem or answer.get("error"):
                result["phone"] = f"not searched: {problem or answer['error']}"
            elif query in mail_only:  # its emails only, with the room Google's results would have had
                mails = "\n".join(line for line in answer.get("found", "").splitlines() if line.startswith("Mail "))
                result["phone"] = clipped(mails, 2 * LOOKUP_CHARS) or "nothing found"
            else:
                result["phone"] = clipped(answer.get("found", "")) or "nothing found"
            if email and (why := "the phone's search did not answer" if problem or answer.get("error")
                          else GMAIL_NOT_SEARCHED.get(answer.get("mail"))):
                result["gmail"] = GMAIL_UNKNOWN.format(why=why)
                unseen.append(query)
            if query in mail_only:
                results.append(result)
                continue
            found = web.get(query, {})
            if web_problem or found.get("error"):
                result["web"] = f"not searched: {web_problem or found['error']}"
            else:
                result["web"] = clipped(found.get("found", "")) or "nothing found"
            if apps:
                listed = play.get(query, {})
                if play_problem or listed.get("error"):
                    result["play"] = f"not searched: {play_problem or listed['error']}"
                else:
                    result["play"] = clipped(listed.get("found", "")) or "nothing found"
            # The user's own browsing has the words of few queries: it is there when it has them.
            if lines := browsed.get(query, {}).get("found"):
                result["history"] = lines
            # A conversation is listed once, under the first query that found it: the queries of one look-up are
            # mostly wordings of the same thing, and found the same conversations again.
            earlier = [c for c in self.runner.store.search_history(query, limit=5, exclude_chat=state["chat_id"])
                       if c["chat_id"] not in chats]
            if earlier:
                result["earlier_conversations"] = earlier
                chats.update({c["chat_id"]: c for c in earlier})
            if leads := notifications.search(self.runner.store, query, exclude_chat=state["chat_id"]):
                result["notifications"] = leads
            results.append(result)
        state.setdefault("looked_up", []).append(results)
        self.runner.emit(state, "chats_searched", searched(queries, list(chats.values())))
        state["trigger"] = "looked_up"
        log(state, f"looked up on the phone{', in Gmail' if email else ''}, on Google{', on Google Play' if apps else ''}, "
                   "in earlier conversations and in "
                   "the phone's notifications: "
                   f"{'; '.join(queries)[:200]}"
                   + (f" (phone not searched: {problem})" if problem else "")
                   + (f" (Google not searched: {web_problem})" if web_problem and web_problem != problem else "")
                   + (f" (Google Play not searched: {play_problem})" if play_problem and play_problem != problem else "")
                   + (f" (the user's Chrome history not read: {history_problem})" if history_problem else "")
                   + (f" (Gmail NOT searched for: {'; '.join(unseen)[:160]}: whether such emails exist is unknown)"
                      if unseen and not problem else ""))

    async def youtube(self, tool):
        """Search YouTube, or read the videos the orchestrator picked (their captions, on this computer); the next
        decision reads what came back as `youtube`. A video that cannot be read says why; the others still count."""
        state = self.state
        if tool.search.strip():
            query = tool.search.strip()
            try:
                entry = {"search": query, "videos": await yt.search(query)}
            except yt.YouTubeError as exc:
                entry = {"search": query, "videos": f"not searched: {exc}"}
            what = f"searched YouTube for {query[:80]!r}"
        else:
            words = max(2500, 9000 // len(tool.videos))  # of transcript per video: the call's total stays about the same

            async def one(video):
                try:
                    return await yt.read(video, tool.start, tool.end, max_words=words, comments=tool.comments)
                except yt.YouTubeError as exc:
                    return f"not read: {exc}"

            texts = await asyncio.gather(*(one(v) for v in tool.videos))
            entry = {"read": [{"video": v, "transcript": t} for v, t in zip(tool.videos, texts, strict=True)]}
            what = f"read {len(texts)} YouTube video{'s' if len(texts) != 1 else ''}"
        state.setdefault("youtube", []).append(entry)
        state["trigger"] = "youtube"
        log(state, what)

    def read_memory(self, names):
        """The named memories, in full, are in the next decision's `memories_read`, and stay there for the task (the
        newest MEMORIES_READ of them)."""
        state = self.state
        kept = [name for name in state.get("memories_read", []) if name not in names]
        state["memories_read"] = (kept + list(names))[-MEMORIES_READ:]
        state["trigger"] = "memories_read"
        log(state, "read memories: " + ", ".join(names))

    def note(self, notes):
        """Keep the orchestrator's notes to itself: every later decision of the task reads them (`notes`), and so
        does the task that follows it in the chat (Runner.previous_task). Not in the journal, which says what
        happened and is cut from the front as it grows."""
        kept = self.state.setdefault("notes", [])
        kept += [n for n in notes if n and n not in kept]
        del kept[:-NOTES_KEPT]

    async def remember(self, findings):
        """Commit the task's findings about the user's world to the memory, through the memory agent; the journal
        says what was kept, so the next decision knows."""
        outcome = await remember_findings(self.runner, self.state, findings)
        log(self.state, "memory: " + outcome)

    async def put_on_phone(self, refs):
        """Put files of this chat (`file:N`) and pictures the subagents clipped (`image:N`) on the phone, by the route
        the user's own upload takes; the journal says where each went, or why not, so the next decision names the
        path to the phone agent."""
        store, state = self.runner.store, self.state
        ids = [int(r.split(":")[1]) for r in refs if r.startswith("file:")]
        marks = ",".join("?" for _ in ids)
        found = {r["id"]: r for r in store.rows(
            f"SELECT * FROM attachments WHERE chat_id=? AND sent=1 AND done=1 AND id IN ({marks})",
            (state["chat_id"], *ids))} if ids else {}
        for ref in refs:
            if ref.startswith("image:"):
                await self.put_picture_on_phone(int(ref.split(":")[1]))
                continue
            i = int(ref.split(":")[1])
            row = found.get(i)
            if not row:
                log(state, f"could not put file:{i} on the phone: the user attached no such file to this chat")
                continue
            try:
                path = await attachments.put_on_phone(store, self.runner.devices.client, row, slot=state.get("slot"))
            except DeviceError as exc:
                log(state, f"could not put file:{i} ({row['name']}) on the phone: {exc}")
                continue
            log(state, f"put file:{i} ({row['name']}) on the phone: {path}")

    async def put_picture_on_phone(self, n):
        """A picture a subagent clipped, written to the chat's upload folder and pushed as a JPEG named after its
        caption. The picture keeps its phone path, so it goes once and `pictures` says where it is."""
        state = self.state
        picture = next((p for p in state.get("pictures", []) if p["ref"] == f"image:{n}"), None)
        if not picture:
            log(state, f"could not put image:{n} on the phone: no subagent kept such a picture in this task")
            return
        if picture.get("phone_path"):
            log(state, f"image:{n} is on the phone already: {picture['phone_path']}")
            return
        rows = self.runner.store.rows("SELECT jpeg FROM images WHERE id=? AND run_id=?", (n, state["id"]))
        if not rows:
            log(state, f"could not put image:{n} on the phone: the picture is no longer kept")
            return
        name = attachments.safe_name(f"{picture.get('caption') or f'penrose picture {n}'}.jpg")
        local = attachments.folder(state["chat_id"]) / "pictures" / f"image-{n}.jpg"
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(rows[0]["jpeg"])
            slot = await attachments.pick_slot(self.runner.devices.client, state.get("slot"))
            if not slot:
                raise DeviceError("there is no phone running")
            path = await attachments.push(self.runner.devices.client, slot, str(local), name)
        except (DeviceError, OSError) as exc:
            log(state, f"could not put image:{n} on the phone: {exc}")
            return
        picture["phone_path"] = path
        log(state, f"put image:{n} (\"{picture.get('caption', '')}\") on the phone: {path}")

    def ask_user(self, question, private=None, options=None, needs=""):
        log(self.state, f"asked the user: {question[:300]}" + (f" (privately: {private['kind']})" if private else "")
            + (f" (offering: {'; '.join(o['label'] for o in options)})" if options else ""))
        if private:  # the form the window shows beside the question: {"kind", "label"} (api/chats.send_private takes it)
            self.state["private_asked"] = private
        if options:  # the buttons under the question: [{"label", "message"}], each sent as the user's own answer
            self.state["options"] = options
        if needs := " ".join((needs or "").split()):  # its line in the user's list of what waits for them (briefings/feed.py)
            self.state["needs"] = needs
        self.runner.wait(self.state)
        self.done = True

    def watch(self, tool):
        """Open and close the watches the turn's last tool named, after the message to the user has gone out. Never
        fails the turn."""
        if not (tool.watch or tool.stop_watching):
            return
        state, store = self.state, self.runner.store
        try:
            for watch_id in tool.stop_watching:
                watches.end(store, watch_id, watches.STOPPED, chat_id=state["chat_id"])
            kept = watches.keep(store, state, [w.model_dump() for w in tool.watch])
        except Exception:  # noqa: BLE001 - the reply is out; a watch that was not kept is only logged
            log(state, "could not keep the watches")
            return
        for w in tool.watch:
            log(state, f"watching for: {w.what}" + (" (it wakes this conversation)" if w.then.strip() else ""))
        self.runner.store.event(state["id"], "watching", {"watches": kept})

    async def finish(self, answer, spoken=""):
        log(self.state, "finished")
        await self.runner.finish(self.state, answer, spoken)
        self.done = True

    async def run_step(self, step_id, note):
        """A subagent works on the step until it reports; its report is what comes back."""
        runner, state = self.runner, self.state
        step = find(state, step_id)
        continuing = step["status"] == "resume"
        if continuing:
            for answer in step.pop("answers", []):  # what the user wrote for this step instead of the entry
                step.setdefault("notices", []).append(f"From the user: {answer}")
            if note:
                step.setdefault("notices", []).append(f"From the orchestrator: {note}")
            log(state, f"{step_id} continues after the user's entry")
        else:
            if attempt := previous_attempt(step):
                step["previous_attempt"] = attempt
            for key in ("report", "problem", "verification", "verified", "sessions", "question", "user_messages"):
                step.pop(key, None)
            step["attempts"] = step.get("attempts", 0) + 1
            step["note"] = note
            log(state, f"{step_id} started (attempt {step['attempts']}): {step['goal'][:200]}")
        step["status"] = "running"
        runner.emit(state, "node_status", {"node_id": step_id, "status": "running", "slot": state.get("slot")})
        session = None
        # A question step reads and reports: a short time budget, then it hands over what it has. An action step
        # (a login, a cart, a booking) gets the long one. Either way the orchestrator hears back in minutes.
        question = step.get("kind") == "question"
        max_turns = settings.subagent_max_turns
        minutes = settings.question_step_minutes if question else settings.action_step_minutes
        self.works_in(step)
        try:
            await runner.lease(state, step)
            step["status"] = "running"
            if continuing and not state.get("phone_kept", True):
                step.setdefault("notices", []).append(
                    "Another task used the phone while this step waited for the user: the screen has moved on. "
                    "Find your place again before you continue."
                )
            await runner.type_secret(state, step)
            if not continuing:  # a fresh fix: an app that starts listening now gets it at once
                await runner.devices.location(state["slot"], refresh=True)
            session = PhoneAgent(
                runner, state, step, SUBAGENT, brief=self.subagent_brief, max_turns=max_turns, max_seconds=60 * minutes,
                give_up=lambda reason: Report(status="failed", summary=reason), fresh=not continuing,
                handover=HANDOVER, listens=True,
            )
            if step.get("kind") == "call":
                report = await self.dial_and_talk(session, step)
            else:
                report = await session.run()
        except NeedUser as exc:
            self.clock(session)
            step["status"] = "waiting_user"
            step["question"] = {"question": exc.question, "sensitive": exc.sensitive, **({"kind": exc.kind} if exc.kind else {})}
            log(state, f"{step_id} waits for the user: {exc.question[:200]}")
            runner.emit(state, "node_status", {"node_id": step_id, "status": "waiting_user", "question": step["question"]})
            return
        except (DeviceError, httpx.TransportError) as exc:
            report = Report(status="failed", summary=(
                f"The phone could not be reached or did not answer: {safe_error(exc)} An action may or may not have "
                "happened: the next attempt must look at the screen first and never repeat an uncertain booking, purchase or message."
            ))
        self.clock(session)
        screen = session.driver.screen if session else None
        self.keep_pictures(step, session)
        self.record(step, report, seen_app=screen.header.get("app") if screen else None)

    async def dial_and_talk(self, session, step):
        """A call step: the subagent dials while penrose's voice already listens on the line, so the voice
        answers their first "Hello?" at once. Once they speak, the dialling is done (the subagent is stopped);
        if the subagent cannot place the call, the line closes and its report comes back."""
        call = Call(self.runner, self.state, step)
        line = asyncio.create_task(call.run())
        dial = asyncio.create_task(session.run())
        answered = asyncio.create_task(call.answered.wait())
        try:
            await asyncio.wait({line, dial, answered}, return_when=asyncio.FIRST_COMPLETED)
            if not dial.done():  # they answered, or the line could not open: the dialling is over either way
                await stopped(dial)
                if not call.answered.is_set():
                    return await line
                report = Report(status="completed", summary="The call was answered.")
            else:
                report = dial.result()  # NeedUser and the rest go up, with the line closed below
            if report.status != "completed" and not call.answered.is_set():
                return report
            log(self.state, f"{step['id']} dialled; penrose is on the call")
            call.dialled()
            return await line
        finally:
            for task in (dial, answered, line):
                await stopped(task)

    def keep_pictures(self, step, session):
        """The pictures a session clipped for the user join the task's `pictures`, where the orchestrator reads
        them (a reference, a caption, where it was taken), and the step's view."""
        clips = (session.mem.get("clips") if session else None) or []
        if not clips:
            return
        kept = self.state.setdefault("pictures", [])
        known = {p["ref"] for p in kept}
        for c in clips:
            picture = {"ref": f"image:{c['id']}", "caption": c["caption"], "kind": c["kind"], "step": step["id"],
                       "where": c.get("place", ""), "size": f"{c['w']}×{c['h']}"}
            if picture["ref"] not in known:
                kept.append(picture)
                step.setdefault("pictures", []).append(picture["ref"])

    def clock(self, session):
        """Add what this session has used since it was last counted to the task's phone time."""
        if session is None:
            return
        used = session.seconds()
        self.state["phone_seconds"] = self.state.get("phone_seconds", 0) + max(0.0, used - session.mem.get("counted", 0))
        session.mem["counted"] = used

    def works_in(self, step, package=None, said=""):
        """Tell the notification log which app this chat's step works in and what the step is about, so what
        that app shows from the people and things the step names is passed to this chat (notifications.route)."""
        package = (step.get("app") or {}).get("package") or package
        about = (self.state.get("context"), step.get("goal"), step.get("instructions"), step.get("done_when"), said)
        with contextlib.suppress(Exception):  # the step never fails for it
            notifications.worked_in(self.runner.store, self.state["chat_id"], package, about)

    def record(self, step, report, seen_app=None):
        state = self.state
        self.works_in(step, seen_app, report.summary)
        step["report"] = report.model_dump(exclude={"tool", "lessons"}, exclude_defaults=True)
        # For the memory agent after the run; the orchestrator does not need them. Only from a step that worked: a
        # failed one explains its failure as the app's limit ("the keypad takes only minutes and seconds", "the
        # first Save only closes the keyboard"), and kept as lessons those steered later tasks wrong (2026-11-15).
        if report.lessons and report.status == "completed":
            app = step.get("app") or {}
            state.setdefault("lessons", []).append({
                "step": step["id"], "app": app.get("name", ""), "package": app.get("package") or seen_app or "",
                "status": report.status, "summary": report.summary, "lessons": report.lessons,
            })
        step["status"] = REPORTED[report.status]
        state.setdefault("facts", {}).update(report.facts)
        if report.status == "failed":
            step["problem"] = report.summary
        if report.status in ("failed", "unfinished"):
            self.runner.emit(state, "node_feedback", {"node_id": step["id"], "attempt": step["attempts"], "feedback": report.summary})
        line = {"completed": "done", "failed": "failed", "needs_input": "needs input",
                "unfinished": "handed back unfinished"}[report.status]
        log(state, f"{step['id']} {line}: {report.question if report.status == 'needs_input' else report.summary}")
        self.latest = {"step": step["id"], "report": step["report"]}
        if step.get("call_transcript"):  # what was said on the call: the other person's words are data
            self.latest["call_transcript"] = transcript_view(step)
        self.runner.emit(state, "node_status", {
            "node_id": step["id"], "status": "succeeded" if report.status == "completed" else step["status"],
            "output": report.summary, **({"error": report.summary} if report.status == "failed" else {}),
        })

    async def verify_step(self, step_id, claim, how):
        """A verifier checks on the phone, read-only, that the step's result is really there."""
        runner, state = self.runner, self.state
        step = find(state, step_id)
        step["status"] = "verifying"
        step["verifications"] = step.get("verifications", 0) + 1  # a check that failed may be made again, once
        runner.emit(state, "node_status", {"node_id": step_id, "status": "running"})
        log(state, f"{step_id} verifying: {claim[:200]}")
        device_state = None

        async def brief(session):
            nonlocal device_state
            if session.mem["turns"] == 1 and device_state is None:
                device_state = await runner.device_state(state) or {}
            view = {"claim": claim, "how": how, "step": {"goal": step["goal"], "done_when": step["done_when"]}}
            # What the step itself found wrong: a check of the screen alone passed a note the step had just
            # read back as an empty file (2026-11-15).
            if issues := (step.get("report") or {}).get("issues"):
                view["step"]["reported_issues"] = issues
            if skill := for_app(step.get("app")):
                view["app_skill"] = skill
            if memories := for_step(runner.store, step.get("app")):  # the notes on the app only
                view["memories"] = memories
            if session.mem["turns"] == 1 and device_state:
                view["device_state"] = device_state
            if session.packages is None:  # first turn: what open_app can open, by name
                session.packages = await runner.installed_apps(state)
                if session.packages:
                    view["installed_apps"] = installed_lines(session.packages)
            return view

        session = None
        try:
            await runner.lease(state, step)
            step["status"] = "verifying"
            session = PhoneAgent(
                runner, state, step, VERIFIER, brief=brief, max_turns=settings.verifier_max_turns,
                give_up=lambda reason: Verdict(passed=False, reason=f"The check could not be finished: {reason}"),
            )
            verdict = await session.run()
        except (DeviceError, httpx.TransportError) as exc:
            verdict = Verdict(passed=False, reason=f"The phone could not be checked: {safe_error(exc)}")
        self.clock(session)
        self.keep_pictures(step, session)
        step["verification"] = {"passed": verdict.passed, "reason": verdict.reason, "evidence": verdict.evidence}
        if verdict.passed:
            step["status"], step["verified"] = "done", True
            log(state, f"{step_id} verified: {verdict.reason}")
        else:
            step["status"], step["problem"] = "failed", f"Verification failed: {verdict.reason}"
            log(state, f"{step_id} not verified: {verdict.reason}")
            runner.emit(state, "node_feedback", {"node_id": step_id, "attempt": step.get("attempts", 0), "feedback": step["problem"]})
        self.latest = {"step": step_id, "verification": step["verification"]}
        runner.emit(state, "node_status", {
            "node_id": step_id, "status": "succeeded" if verdict.passed else "failed",
            **({} if verdict.passed else {"error": step["problem"]}),
        })

    # --- what a subagent reads ----------------------------------------------------------------------

    async def subagent_brief(self, session):
        """The stable part of every subagent turn: the step, the shared context, the facts, the orchestrator's note."""
        runner, state, step = self.runner, self.state, session.step
        brief = {
            "task": state.get("context") or state["request"],
            "step": {k: step[k] for k in ("id", "goal", "app", "kind", "instructions", "done_when") if step.get(k)},
        }
        if state.get("facts"):
            brief["facts"] = state["facts"]
        if session.location is None:  # read once per session: the fix the phone was given
            fix = await runner.devices.location(state["slot"])
            session.location = (
                f"GPS at lat {fix['lat']:.4f}, lng {fix['lng']:.4f} (the user's computer's position, set on the "
                "phone's GPS). This is where the user is: \"near me\", \"here\" and distances mean this point. "
                "Apps that ask for location get it the moment they listen; `dumpsys location` shows nothing until "
                "one does. Never move it."
                if fix and fix.get("lat") is not None else
                "not set on this phone (no fix was given). Apps may show no position; ask nothing and move nothing."
            )
        brief["phone_location"] = session.location
        if skill := for_app(step.get("app")):  # authored, before what earlier visits learned
            brief["app_skill"] = skill
        if memories := for_step(runner.store, step.get("app"), step.get("memories")):
            brief["memories"] = memories
        if step.get("kind") == "call":
            brief["call_step"] = (
                "This step only places the call. Open the app, find the contact and start a voice call (camera off in "
                "a video app; allow the microphone if asked), and report completed as soon as the call screen shows it "
                "calling, ringing or connected. Never wait for an answer, speak, type in the call or hang up: penrose's "
                "voice takes the call from there. If the contact cannot be found or called, report failed with what you saw."
            )
        if state.get("scope"):
            brief["only_these_apps"] = (
                "A notification started this task, and nobody is watching it. It may only use "
                f"{', '.join((state.get('woken') or {}).get('only_apps') or state['scope'])}: the phone refuses a tap, "
                "typed text, a launch or a command anywhere else. If the step cannot be done in those apps, report "
                "that; never look for another way round."
            )
        if step.get("note"):
            brief["note_from_orchestrator"] = step["note"]
        if step.get("user_messages"):  # sent while this step worked, newest last; they outrank the note
            brief["user_messages"] = step["user_messages"]
        if step.get("previous_attempt"):
            brief["previous_attempt"] = step["previous_attempt"]
        if private := runner.store.private_labels(state["chat_id"]):  # labels only: the runtime types the values
            brief["private_entries"] = private
        if on_phone := attachments.phone_files(runner.store, state["chat_id"]):  # the user's files in the phone's folder
            brief["phone_files"] = on_phone
        if session.google is None:  # read once per session: the user signed in before the task
            session.google = {"signed_in": await session.devices.accounts(session.slot, session.owner)}
        brief["google_account"] = session.google
        if session.packages is None:  # first turn, or an install or ADB command may have changed them
            session.packages = await runner.installed_apps(state)
            brief["installed_apps"] = installed_lines(session.packages) or "unavailable; check with adb shell pm list packages"
        if (app := step.get("app")) and app.get("package"):
            mine = next((a for a in session.packages if a["package"] == app["package"]), None)
            brief["target_app_installed"] = bool(mine) if session.packages else "unknown; check with pm list packages"
            if mine and mine.get("shortcuts"):  # where the app's own shortcuts lead: open_app's `shortcut`
                brief["app_entry_points"] = mine["shortcuts"]
        if uncertain := [t["summary"] for t in step["turns"] if t.get("status") == "uncertain"]:
            brief["uncertain_actions"] = uncertain[-5:]
        return brief
