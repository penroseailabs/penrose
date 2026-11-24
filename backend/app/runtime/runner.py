"""One run, from the user's message to the reply: the gate, then the orchestrator until it finishes or asks.

The runner also holds what every agent shares: the model call (journaled as events for the activity trace),
the phone's lease (one per run, so the phone carries over from step to step), and the store.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

from .. import attachments
from ..agents import capture
from ..agents.catalog import route
from ..agents.llm import LLM
from ..agents.registry import GATE
from ..config import settings
from ..delivery import NOTICE, YIELDS_PHONE, told
from ..device.client import Devices
from ..errors import DeviceError, NewMessage, ProviderError, safe_error
from ..plan import ACTIVE
from ..pricing import cost
from ..store import HALTED

# A run in one of these states gives up its phone to a run that needs it.
from ..tools.gate import Answer
from ..tools.memory import ReadMemory, UpdateMemory
from ..tools.recall import Recall, recalled, searched
from . import pictures, title
from .clock import local_clock
from .compaction import compact
from .memory import about_user, learn, read, remember_now
from .orchestrator import Orchestrator
from .phone import call_line
from .reflection import reflect

log = logging.getLogger(__name__)

# The whole state is saved when a status changes, before every model call, and otherwise at most this often;
# between those, an event saves only the new turns (Store.save).
WHOLE_SAVE_SECONDS = 2.0
# How long one lease request waits at the gateway for the phone before it is sent again.
LEASE_WAIT_SECONDS = 20
# A task keeps the phone awake this long from its start, as the user's presence does (api/devices.py).
WAKE_FOR_TASK_SECONDS = 60
AWAKE_SECONDS = 90  # how long each request keeps the phone awake while a task decides (Runner.awake asks again)
# Streamed reasoning is written in pieces at most this often (the window adds them up).
THINKING_SECONDS = 0.25
# While the orchestrator thinks, a new message from the user is looked for this often (runner.call `heeds`).
HEED_SECONDS = 0.5


async def cancel(task):
    """Stop a task and wait until it has stopped, whatever it ended with."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def limit(state):
    """The lease of a task a notification woke names the apps it may use (gateway/scope.py); any other's does not."""
    return {"apps": state["scope"]} if state.get("scope") else {}


def preview(output):
    """The text fields of a partial turn, for the live trace."""
    return {k: v for k, v in (output or {}).items() if isinstance(v, str)}



class Runner:
    def __init__(self, store, model=None, devices=None):
        self.store = store
        self.model = model or LLM()
        self.devices = devices or Devices()
        self.prepared = set()  # devices whose animations are already off and screen locked to portrait
        self.zones = {}  # slot -> the time zone its clock was last put on
        # Per run: (its statuses, when) at its last whole save; and the runs with changes since then.
        self.saved = {}
        self.unsaved = set()
        self.background = set()  # tasks that run beside a run, like naming its chat (runtime/title.py)

    async def close(self):
        for task in self.background:
            task.cancel()
        await asyncio.gather(self.model.close(), self.devices.close())

    # --- state and events ---------------------------------------------------------------------------

    def save(self, state, whole=None, event=None):
        """Save the run, with `event` ((kind, payload)) in the same write. The whole state when asked, when the
        run's or a step's status changed, or when the last whole save is a while ago; otherwise only its turns,
        which carry what an action did (a crash must never lose that an action started)."""
        marks = (state.get("status"), tuple((s.get("id"), s.get("status")) for s in state.get("plan", [])))
        last, now = self.saved.get(state["id"]), time.monotonic()
        if whole is None:
            whole = last is None or last[0] != marks or now - last[1] >= WHOLE_SAVE_SECONDS
        self.store.save(state, whole=whole, event=event)
        if whole:
            self.saved[state["id"]] = (marks, now)
            self.unsaved.discard(state["id"])
        else:
            self.unsaved.add(state["id"])

    def flush(self, state):
        """Save what the last saves left out, before a wait (a model call) that may be long."""
        if state["id"] in self.unsaved:
            self.save(state, whole=True)

    def emit(self, state, kind, payload):
        self.save(state, event=(kind, payload))

    def cancelled(self, state):
        if self.store.status(state["id"]) in HALTED:
            raise asyncio.CancelledError()

    def hear(self, state):
        """The messages the user sent while the run worked (Enter, not Ctrl+Enter), taken from its inbox. They
        join `state["inbox"]`, so the orchestrator's next decision reads them as new user messages; the phone
        agent also shows them to the subagent at once. Returns the new ones. The notifications passed to the
        run meanwhile (app/notifications.py) are taken too, into `state["notifications"]`: the orchestrator's
        next decision reads them as what an app showed, and they are never among the user's messages."""
        if notices := self.store.take_messages(state["id"], NOTICE):
            noted = [row["content"] for row in notices]
            state.setdefault("notifications", []).extend(noted)
            self.emit(state, "notification_heard", {"notifications": noted})
        rows = self.store.take_messages(state["id"])
        if not rows:
            return []
        with self.store.connect() as db:  # the user's words, then a line on each file that came with them
            heard = [told(db, row["content"], row.get("attachments")) for row in rows]
        if attached := [i for row in rows for i in json.loads(row.get("attachments") or "[]")]:
            state.setdefault("attached", []).extend(attached)  # the photos are shown at the next decision
        state.setdefault("inbox", []).extend(heard)
        if provider := rows[-1].get("provider"):
            state["provider"] = provider
        if models := rows[-1].get("models"):
            state["models"] = json.loads(models)
        self.emit(state, "message_heard", {"messages": heard, "ids": [row["message_id"] for row in rows]})
        return heard

    def on_usage(self, state, identity):
        """A callback for the model client, `record(provider, model, usage, created)`: keeps each request's
        usage with its cost in credits, on the chat, and tells the window (a `usage` event). Accounting never
        fails the run."""
        def record(provider, model, usage, created=None):
            try:
                when = created or time.time()
                credits, peak = cost(provider, model, usage, when)
                row = self.store.record_usage(state["chat_id"], state["id"], identity.get("call_id"),
                                              identity.get("stage"), provider, model, usage, when, credits, peak)
                self.store.event(state["id"], "usage", row)
            except Exception as exc:
                log.warning("could not record model usage: %s", safe_error(exc))
        return record

    # --- one agent turn -----------------------------------------------------------------------------

    async def call(self, state, agent, context, node_id=None, images=None, check=None, heeds=False, hold=None):
        """One turn of `agent`, journaled as events. `check(turn)` vets the turn before anything is
        published; a Rejected from it fails the call like a provider error. `heeds`: a message the user sends
        meanwhile stops the call (NewMessage), so the caller decides again with it instead of acting on a
        decision it has already overtaken. `hold`: an asyncio.Event; until it is set the call runs unseen (no
        stage, thinking or text in the chat) and its turn is not returned, so a call started on speculation
        leaves nothing behind when it is cancelled instead."""
        self.cancelled(state)
        self.flush(state)
        call_id = uuid.uuid4().hex
        identity = {"stage": agent.name, "node_id": node_id, "call_id": call_id}
        shown = False

        def show():
            nonlocal shown
            shown = True
            self.store.event(state["id"], "stage_started", identity)
            for jpeg in images or []:
                image_id = self.store.image(state["id"], node_id, jpeg)
                self.store.event(state["id"], "stage_image", {**identity, "image_id": image_id})

        def unseen():
            """Still held: shows the stage once the hold is lifted."""
            if not shown and hold.is_set():
                show()
            return not shown

        if hold is None:
            show()
        extra = {"images": images} if images else {}  # a model without vision is never sent an empty list
        extra.update(route(state, agent))
        last_update, last_fields, thought = 0, None, ""
        # Reasoning streams in many small pieces: they are written together, a few times a second.
        unwritten, last_written = "", 0.0
        last_heed = time.monotonic()

        def heed():
            nonlocal last_heed
            # Not while held: the head start must not take the user's messages before it is handed the run.
            if heeds and shown and time.monotonic() - last_heed >= HEED_SECONDS:
                last_heed = time.monotonic()
                if self.store.has_messages(state["id"]):
                    raise NewMessage()

        def write_thinking():
            nonlocal unwritten, last_written
            if not shown:
                return  # kept until the stage shows
            if unwritten:
                self.store.event(state["id"], "stage_thinking", {**identity, "text": unwritten, "reset": False})
            unwritten, last_written = "", time.monotonic()

        async def on_thinking(text, reset=False):
            nonlocal thought, unwritten, last_written
            heed()
            thought = text if reset else thought + text  # streamed in increments; reset replaces a retried attempt
            if unseen():
                unwritten = thought  # all of it, written at once when the stage shows
                return
            if reset:
                unwritten, last_written = "", time.monotonic()
                self.store.event(state["id"], "stage_thinking", {**identity, "text": text, "reset": True})
                return
            unwritten += text
            if time.monotonic() - last_written >= THINKING_SECONDS:
                write_thinking()

        async def on_update(partial):
            nonlocal last_update, last_fields
            fields, now = preview(partial), time.monotonic()
            if fields and (fields == last_fields or now - last_update < 0.1):
                return
            self.cancelled(state)
            heed()
            if unseen():
                return
            last_update, last_fields = (now if fields else 0), fields
            message = self.said(state, agent.voice(partial)) if agent.voice else ""
            self.store.event(state["id"], "stage_delta", {**identity, "output": fields, "message": message})

        try:
            try:
                with capture.labelled(run=state["id"], chat=state.get("chat_id"), **identity):
                    turn = await self.model.ask(agent, context, on_update=on_update, on_thinking=on_thinking,
                                                on_usage=self.on_usage(state, identity), **extra)
            except ProviderError as exc:
                if images and not agent.vision:  # the model's API said no: with a picture in the request, that may be why
                    raise ProviderError(f"{exc} This message came with a picture: if the model cannot read pictures, "
                                        "choose one that can, or send the message again without it.") from None
                raise
            finally:
                write_thinking()
            if not shown:
                await hold.wait()
                show()
                write_thinking()
                if heeds and self.store.has_messages(state["id"]):
                    raise NewMessage()  # the user wrote before it was handed the run: decide again with it
            self.cancelled(state)
            if check:
                check(turn)
        except (Exception, asyncio.CancelledError) as exc:
            description = ("Cancelled" if isinstance(exc, asyncio.CancelledError)
                           else "Started over for the user's new message" if isinstance(exc, NewMessage)
                           else safe_error(exc))
            if shown:  # one still held was never seen
                self.store.event(state["id"], "stage_error", {**identity, "description": description})
            raise
        output = turn.model_dump()
        message = self.said(state, agent.voice(output)) if agent.voice else ""
        if message:
            saved = self.store.message(
                state["chat_id"], "assistant", message, source=agent.name, message_id=call_id, thinking=thought,
                spoken=agent.spoken(output) if agent.spoken else None,
            )
            self.store.event(state["id"], "agent_message", {**identity, "message": saved})
            title.begin(self, state, message)
        # What the chat keeps of a turn besides its message (api/chats.py `thoughts`): the notes it kept for its own
        # later turns, and its reasoning when it said nothing to the user, which no message carries.
        notes = agent.notes(output) if agent.notes else []
        if agent.voice and (notes or (thought and not message)):
            self.store.event(state["id"], "turn_thought", {**identity, "thinking": "" if message else thought, "notes": notes})
        # The size of what the agent read, as for the phone agents (act).
        size = {"input_chars": len(json.dumps(context, ensure_ascii=False, default=str))}
        self.store.event(state["id"], "stage_output", {**identity, "output": output, "size": size})
        return turn

    def said(self, state, message):
        """What an agent says to the user, with the pictures it refers to (`image:N`, kept by the phone agents'
        clip) turned into this run's image URLs; a reference the run does not hold is dropped."""
        return pictures.resolve(message, state["id"], state.get("pictures", [])) if message and "image:" in message else message

    async def act(self, state, agent, text, node_id=None, images=None):
        """One turn of a phone agent (native tool calls), journaled like `call`: (what it wrote besides its
        calls, its calls as [{"name", "arguments"}], its reasoning or ""). The trace shows the calls as the
        turn's actions, and the reasoning as the turn's thinking."""
        self.cancelled(state)
        self.flush(state)
        call_id = uuid.uuid4().hex
        identity = {"stage": agent.name, "node_id": node_id, "call_id": call_id}
        self.store.event(state["id"], "stage_started", identity)
        for jpeg in images or []:
            image_id = self.store.image(state["id"], node_id, jpeg)
            self.store.event(state["id"], "stage_image", {**identity, "image_id": image_id})
        extra = {"images": images} if images else {}
        extra.update(route(state, agent))
        try:
            with capture.labelled(run=state["id"], chat=state.get("chat_id"), **identity):
                content, calls, *more = await self.model.act(agent, text, on_usage=self.on_usage(state, identity), **extra)
            self.cancelled(state)
        except (Exception, asyncio.CancelledError) as exc:
            description = "Cancelled" if isinstance(exc, asyncio.CancelledError) else safe_error(exc)
            self.store.event(state["id"], "stage_error", {**identity, "description": description})
            raise
        thought = more[0] if more and isinstance(more[0], str) else ""
        usage = more[1] if len(more) > 1 and isinstance(more[1], dict) else {}
        if thought:
            self.store.event(state["id"], "stage_thinking", {**identity, "text": thought, "reset": True})
        actions = [{"tool": c.get("name"), **c.get("arguments", {})} for c in calls]
        summary = content or "; ".join(call_line(c) for c in calls) or "(no tool call)"
        # The size of what the agent read, for seeing how its context grows over a session.
        size = {"input_chars": len(text), **{k: usage[k] for k in ("prompt_tokens", "completion_tokens",
                                                                   "prompt_cache_hit_tokens") if k in usage}}
        self.store.event(state["id"], "stage_output", {**identity, "output": {"summary": summary, "actions": actions}, "size": size})
        return content, calls, thought

    def base_context(self, state):
        """What every agent that talks to the user knows: the conversation and the user."""
        # In order of how often it changes: the provider caches the prompt's unchanged prefix, so what is the same
        # for every chat (the user, their memories) comes first, then this chat (the conversation, which only
        # grows, before this request, so a new request still reuses it), then what differs on every call (the
        # clock, last; the orchestrator keeps it last among its own fields too).
        return {
            "locale": self.store.user_locale(),
            **about_user(self.store),
            "google_account": self.store.google_account(),
            **self.store.conversation_context(state["chat_id"]),
            # What the user attached to their messages: the text of the documents, and the files on the phone.
            **({"attached_files": found} if (found := attachments.sent_documents(self.store, state["chat_id"])) else {}),
            **({"phone_files": found} if (found := attachments.phone_files(self.store, state["chat_id"])) else {}),
            "request": state["request"],
            # The user spoke the request and hears the reply (api/voice.py): the agents also write `spoken`.
            **({"voice": True} if state.get("channel") == "voice" else {}),
            # What the user gave privately (api/chats.send_private): numbers and labels only, never the values.
            **({"private_entries": found} if (found := self.store.private_labels(state["chat_id"])) else {}),
            "current_datetime_utc": datetime.now(timezone.utc).isoformat(),
            **local_clock(self.store),
        }

    async def phone_location(self, state):
        """The phone's GPS fix: the run's phone when it has one, else the one phone there is. None when no fix
        was given (the window sets it from the computer's position) or there are several phones to choose from."""
        if slot := state.get("slot"):
            fix = await self.devices.location(slot)
        else:
            known = [fix for fix in (await self.devices.locations()).values() if fix]
            fix = known[0] if len(known) == 1 else None
        return {"lat": fix["lat"], "lng": fix["lng"]} if fix and fix.get("lat") is not None else None

    async def search_slot(self, state):
        """The phone a look-up searches: the run's own, else the first one up. A search opens nothing on the
        phone, so it needs no lease."""
        if slot := state.get("slot"):
            return slot
        return next((p["id"] for p in await self.devices.phones() if p.get("connected")), None)

    # --- the run ------------------------------------------------------------------------------------

    async def run(self, state):
        # The phone wakes while the first model call runs: by the time a look-up or a step needs it, it is out of
        # Doze and its apps are back on the network (gateway/sleep.py keep_awake).
        early = asyncio.create_task(self.wake_phone(state))
        try:
            await self.advance(state)
            await self.afterwards(state)
        finally:
            early.cancel()

    async def wake_phone(self, state, seconds=WAKE_FOR_TASK_SECONDS):
        with contextlib.suppress(Exception):
            if slot := await self.search_slot(state):
                await self.devices.wake(slot, seconds)

    @contextlib.asynccontextmanager
    async def awake(self, state):
        """The phone kept awake for as long as the body runs, however long that is (gateway/sleep.py keep_awake,
        asked again before it runs out). Without it the phone sleeps 15 s after its last use and dozes 15 s after
        that, and what uses it next (a look-up, a step) waits for its apps to come back: sleep is for a phone
        nobody is working with, never for one a task is thinking about."""
        async def keep():
            while True:
                await self.wake_phone(state, AWAKE_SECONDS)
                await asyncio.sleep(AWAKE_SECONDS / 2)

        keeping = asyncio.create_task(keep())
        await asyncio.sleep(0)  # the first request is on its way before the body starts; nothing waits for it
        try:
            yield
        finally:
            keeping.cancel()

    async def afterwards(self, state):
        """After the reply, so it never delays one, and after the steps, so it can learn from their lessons:
        keep what the run taught, fold the chat's older messages once it is long, and every few tasks reflect
        on the recent ones. Each is best effort and never fails the run."""
        await learn(self, state)
        await compact(self, state)
        await reflect(self, state)

    async def advance(self, state):
        state["status"] = "running"
        state.pop("error", None)
        # A stopped run's lease was released after its state was last saved, so the stored slot may
        # belong to nobody now. Lease again: the gateway hands a run that still holds one the same slot.
        state.pop("slot", None)
        self.emit(state, "run_started", {})
        try:
            if state.get("route"):
                return await Orchestrator(self, state).drive()
            # The orchestrator starts beside the gate: most messages are handed to it, and it then has a head start
            # of the gate's whole decision. Until the gate hands it over, it is unseen and does nothing.
            handed = asyncio.Event()
            state["trigger"] = "new_request"
            ahead = asyncio.create_task(Orchestrator(self, state, hold=handed).drive())
            try:
                orchestrate = await self.gate(state)
            except BaseException:
                await cancel(ahead)
                raise
            if not orchestrate:
                await cancel(ahead)
                state.pop("trigger", None)
                return
            handed.set()
            await ahead
        except asyncio.CancelledError:
            if self.store.status(state["id"]) in HALTED:
                await self.release(state)
            raise
        except Exception as exc:
            # The user reads a safe line; the log keeps where it broke.
            log.exception("run %s failed", state["id"])
            state["status"] = "failed"
            state["error"] = safe_error(exc)
            for step in state.get("plan", []):
                if step["status"] in ACTIVE:
                    step["status"] = "failed"
                    step["problem"] = state["error"]
            await self.release(state)
            self.store.message(state["chat_id"], "assistant", state["error"])
            self.emit(state, "error", {"message": state["error"]})

    async def gate(self, state):
        """The fast response agent's decision: True hands the message to the orchestrator; False means it was
        answered and the run is complete."""
        context = self.base_context(state)
        photos = attachments.images_of(self.store, state.get("attached"))  # the model's API says if it cannot see them
        turn = await self.call(state, GATE, context, images=photos or None)
        action = turn.actions[0]
        # Each once: the gate searches earlier conversations, reads memories in full, or updates the memory, then
        # decides.
        used = set()
        while isinstance(action, (Recall, ReadMemory, UpdateMemory)) and type(action) not in used:
            used.add(type(action))
            if isinstance(action, Recall):
                context["recalled"] = recalled(self.store, action, state["chat_id"])
                self.emit(state, "chats_searched", searched(action.queries, context["recalled"]["found"],
                                                            action.after, action.before))
            elif isinstance(action, ReadMemory):
                context["memories_read"] = read(self.store, action.names)
            else:
                context["memory_updated"] = await remember_now(self, state, action.request)
            turn = await self.call(state, GATE, context, images=photos or None)
            action = turn.actions[0]
        if isinstance(action, (Recall, ReadMemory, UpdateMemory)):
            action = None  # twice is not allowed: the orchestrator takes it from here
        state["route"] = {"orchestrate": not isinstance(action, Answer)}
        if isinstance(action, Answer):
            state.pop("attached", None)  # answered: nothing is left for the orchestrator to look at
            state["status"] = "completed"
            self.emit(state, "completed", {"reply": action.text, "spoken": action.spoken})
            return False
        return True

    def wait(self, state):
        """The run waits for the user: a question in the chat, or a step's private entry or Google card."""
        state["status"] = "waiting_user"
        state["questions"] = [
            {"node_id": s["id"], **s["question"]}
            for s in state.get("plan", []) if s["status"] == "waiting_user" and s.get("question")
        ]
        # The orchestrator's question may ask for something private (a card): the screens show its form at once.
        self.emit(state, "questions", {"questions": state["questions"],
                                       **({"private": state["private_asked"]} if state.get("private_asked") else {}),
                                       **({"options": state["options"]} if state.get("options") else {})})

    async def finish(self, state, answer, spoken=""):
        for step in state.get("plan", []):
            if step["status"] == "waiting_user":  # the task no longer needs that entry
                step["status"] = "stopped"
                step.pop("question", None)
        await self.release(state)
        state["status"] = "completed"
        state["questions"] = []
        self.emit(state, "completed", {"reply": self.said(state, answer), "spoken": spoken})

    def previous_task(self, state):
        """The last planned task in this conversation, so a follow-up can build on it."""
        for row in self.store.rows(
            "SELECT state,updated FROM runs WHERE chat_id=? AND id!=? ORDER BY updated DESC LIMIT 5", (state["chat_id"], state["id"]),
        ):
            prior = json.loads(row["state"])
            if prior.get("plan"):
                zone = self.store.zone()
                return {
                    "request": prior["request"], "status": prior["status"],
                    # When its facts were true: a follow-up checks again what may have changed since.
                    "ended": datetime.fromtimestamp(row["updated"], zone or timezone.utc).strftime("%A %d %B %Y, %H:%M")
                    + ("" if zone and self.store.user_locale().get("timezone") else " UTC"),
                    "facts": prior.get("facts", {}), "journal": [j["line"] for j in prior.get("journal", [])][-15:],
                    # What its orchestrator kept for itself: what it ruled out and why, the leads it did not follow.
                    **({"notes": prior["notes"]} if prior.get("notes") else {}),
                }
        return None

    # --- the phone ----------------------------------------------------------------------------------

    async def lease(self, state, step):
        """The run's phone, leased once and kept until the run ends. Waits while the gateway is busy."""
        if state.get("slot"):
            return state["slot"]
        if slot := await self.take_phone(state):
            return await self.leased(state, slot)
        step["status"] = "waiting_device"
        # Claude Code (api/phone.py) holds it as `cli:<client>`: the user knows which session, so say so.
        claude = any(owner.startswith("cli:") for owner in (await self.devices.holders()).values())
        # Kept on the step (plan.node_views) so the window can say why nothing happens, not only in the event.
        step["waiting"] = ("Waiting for the phone: Claude Code is using it. Work resumes automatically when it is free."
                           if claude else "Waiting for the phone. Work resumes automatically when it is free.")
        self.emit(state, "node_status", {"node_id": step["id"], "status": "waiting_device", "description": step["waiting"]})
        try:
            return await self.wait_for_phone(state, step, claude)
        finally:
            step.pop("waiting", None)

    async def wait_for_phone(self, state, step, claude):
        """The gateway holds each request until the phone is released or comes up (gateway/devices.py), so the
        run takes it the moment it is free; the request is renewed now and then, which also lets a stop end the
        wait."""
        deadline = asyncio.get_running_loop().time() + settings.device_wait_seconds
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            self.cancelled(state)
            asked = asyncio.get_running_loop().time()
            if slot := await self.devices.acquire(state["id"], wait=min(LEASE_WAIT_SECONDS, left), **limit(state)):
                return await self.leased(state, slot)
            if slot := await self.take_phone(state):  # its holder may only be waiting for the user by now
                return await self.leased(state, slot)
            if asyncio.get_running_loop().time() - asked < 1:
                await asyncio.sleep(1)  # the gateway is down or restarting: it could not hold the request
        if claude:
            raise DeviceError("The phone stayed busy: Claude Code is using it. Release it there (`relay phone release`), "
                              "or try again once it is free.")
        raise DeviceError("The phone stayed busy with another task. Stop that task, or try again once it finishes.")

    async def take_phone(self, state):
        """A free phone, or one held by a run that is only waiting for the user (or has ended without letting
        go). A waiting run must not hold the phone hostage: it leases one again when the user answers it."""
        if slot := await self.devices.acquire(state["id"], **limit(state)):
            return slot
        for owner in (await self.devices.holders()).values():
            held = owner.partition(":")[0]
            if held != state["id"] and self.store.status(held) in YIELDS_PHONE and not self.kept_for_user(state, held):
                with contextlib.suppress(Exception):
                    await self.devices.release(owner)
                if slot := await self.devices.acquire(state["id"], **limit(state)):
                    return slot
        return None

    def kept_for_user(self, state, held):
        """Whether a task penrose began on its own (briefings/prepare.py) must leave the phone to the run `held`: one
        that waits for the user to sign in on the phone or to give a private entry the phone's screen is ready for.
        The user's own tasks may take it; penrose's never take it from under them."""
        if not state.get("background"):
            return False
        waiting = self.store.load(held) or {}
        return bool(waiting.get("private_asked")) or any(
            q.get("kind") == "login" or q.get("sensitive") for q in waiting.get("questions") or [])

    async def unlimit(self, state):
        """The user wrote to a task a notification woke: it is theirs now, and may use any app. The limit goes from
        the task and from the lease it holds (a later lease is taken without it)."""
        if not state.pop("scope", None) or not state.get("slot"):
            return
        with contextlib.suppress(Exception):
            await self.devices.acquire(state["id"])

    async def leased(self, state, slot):
        state["slot"] = slot
        # False when the run lost the phone while it waited: another task has used it since.
        state["phone_kept"] = getattr(slot, "kept", True)
        await self.prepare_device(slot, state["id"])
        return slot

    async def release(self, state):
        with contextlib.suppress(Exception):
            await self.devices.release(state["id"])
        state.pop("slot", None)

    async def prepare_device(self, slot, owner):
        """System animations off and the screen locked to portrait, once per device: every screen then finishes
        changing sooner and the agent always sees the same layout. And the phone's clock on the user's time zone,
        again whenever theirs changes: the emulator starts on the computer's, a hosted server's UTC. Best effort."""
        zone = self.store.user_locale().get("timezone")
        if zone and self.zones.get(slot) != zone and await self.devices.set_timezone(slot, zone):
            self.zones[slot] = zone
        if slot in self.prepared:
            return
        self.prepared.add(slot)
        try:
            await self.devices.ops(slot, owner, [{"op": "animations", "value": "off"}, {"op": "rotation", "value": "portrait"}])
        except (ValueError, DeviceError, httpx.TransportError):
            self.prepared.discard(slot)  # try again with the next step

    async def type_secret(self, state, step):
        """The user's private entry goes straight into the focused field: never through a model or an event."""
        secret = self.store.consume_secret(state["id"], step["id"])
        if not secret:
            return
        if not state.get("phone_kept", True):
            # The focused field may now belong to another app: never type a private value blind.
            del secret
            step.setdefault("notices", []).append(
                "The user's private entry was NOT typed: another task used the phone while this step waited. "
                "Get back to the field it is for, then ask for it again."
            )
            return
        await self.devices.action(state["slot"], state["id"], {"kind": "type_text", "text": secret, "sensitive": True})
        del secret
        step["turns"].append({"summary": "Entered the user's private entry into the focused field."})
        step.setdefault("notices", []).append(
            "The runtime typed the user's private entry into the focused field (it shows as [private]). Continue from the screen."
        )

    async def installed_apps(self, state):
        """Every app with an icon on the phone ([{"name", "package", "shortcuts"}]), so that finding an app, its
        package ID or a shortcut into it never costs a turn; [] when the phone cannot say."""
        try:
            return await self.devices.apps(state["slot"], state["id"])
        except (DeviceError, httpx.TransportError):
            return []

    async def device_state(self, state):
        """One read of the phone's settings and foreground app (the `state` op), or None."""
        try:
            results = await self.devices.ops(state["slot"], state["id"], [{"op": "state"}])
        except (ValueError, DeviceError, httpx.TransportError):
            return None
        return results[0].get("state") if results and results[0].get("ok") else None
