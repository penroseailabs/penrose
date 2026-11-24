"""Every agent, in one place: its prompt file, its model, and the tools it may call.

Every agent's turn is one object: the fields of its turn shape (below) and `actions`, its tool calls.

The gate and the orchestrator choose what happens next and answer with native tool calls: their tools are sent
as function definitions, and what the model wrote and called is put together into the turn object (`compose`,
below). A turn without a call is not an error: its text is the message that ends it. Phone agents answer with
native tool calls too (see agents/llm.py): the bridge's tools (app/bridge/tools.py) by name, plus the backend's
own tool classes. The agents that only fill in a form (memory, compaction, title and the like) answer with the
turn object itself, as JSON; the schema they are sent is built from the turn shape and the tool classes.
"""

import copy
from dataclasses import dataclass, replace
from functools import cached_property

from pydantic import BaseModel

from ..bridge.tools import INTERACTIONS, TOOLS
from ..skills import for_tools
from ..tools.base import turn_model
from ..tools.compaction import Summarize
from ..tools.device import DEVICE_TOOLS
from ..tools.frontdesk import Route
from ..tools.gate import Answer, Delegate
from ..tools.memory import ReadMemory, Remember, UpdateMemory
from ..tools.orchestrator import (
    NEXT,
    AskUser,
    Finish,
    LookUp,
    NoteToSelf,
    PutOnPhone,
    RememberFindings,
    RunStep,
    SetPlan,
    VerifyStep,
    note_lines,
)
from ..tools.recall import Recall
from ..tools.screen import AppAction, Clip, Log, Note, Progress, ReadPage
from ..tools.subagent import (
    AskLogin,
    AskForPasswordOrCode,
    Checkpoint,
    Handover,
    Report,
    TypePrivate,
)
from ..tools.title import SetTitle
from ..tools.triage import Sort
from ..tools.verifier import Verdict
from ..tools.watch import Judge
from ..tools.youtube import Youtube, YoutubeTranscript
from .llm import function_def
from .prompts import load

# The harness's tools by what they do. Its `finish` is not used: each phone agent ends with its own tool
# (report, verdict), which carries what the runtime needs back.
SCREEN = ("interact", "type_and_submit", "press_key", "swipe", "drag")
# No set_location: the phone is always where the user's computer is.
# No list_apps since 2026-11-22 (tool audit): 16 calls in about 16,800 recorded turns, and the brief's installed_apps
# now names every app with an icon, Google's and the system's too, which open_app matches by name. Its definition is
# commented out in app/bridge/tools.py; the MCP server and the CLI still offer it.
DEVICE = ("open_app", "open_link", "wait_for", "set_setting", "grant")  # was ("open_app", "list_apps", "open_link", ...)
LOOK = ("request_screenshot",)
# An app's own data. list_data (a whole list from its adapter) and network (what the app received from its servers)
# were taken out on 2026-11-22: list_data failed on all 27 recorded calls and network was never called; why is
# beside their commented-out definitions in app/bridge/tools.py.
READ = ("read_text",)  # was ("list_data", "read_text", "network")
LOOKING = ("tap", *(i for i in INTERACTIONS if i.startswith("scroll_")))  # what a read-only agent may do to an element

# --- turn shapes: what an agent writes besides its tool calls ---------------------------------------


class Turn(BaseModel):
    """Only tool calls."""


class OrchestratorTurn(BaseModel):
    summary: str  # the turn in a line, for the activity trace: what it said, or what it called (orchestrator_turn)
    reply: str = ""  # what it wrote to the user beside its calls


# --- what each agent says to the user (only the gate and the orchestrator speak in the chat) ---------

def gate_voice(out):
    actions = out.get("actions") or []
    first = actions[0] if actions and isinstance(actions[0], dict) else {}
    return first.get("text" if first.get("tool") == "answer" else "note", "") if first.get("tool") in ("answer", "delegate") else ""


def orchestrator_voice(out):
    parts = [out.get("reply") or ""]
    for action in out.get("actions") or []:
        if isinstance(action, dict) and action.get("tool") in ("ask_user", "finish"):
            parts.append(action.get("question") or action.get("answer") or "")
    return "\n\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())


# --- what each agent says aloud when the user is on voice (api/voice.py): the tool's `spoken`, else the short
# note that is already plain speech ---------------------------------------------------------------------------

def gate_spoken(out):
    actions = out.get("actions") or []
    first = actions[0] if actions and isinstance(actions[0], dict) else {}
    if first.get("tool") == "answer":
        return first.get("spoken") or ""
    return first.get("note") or "" if first.get("tool") == "delegate" else ""


def orchestrator_spoken(out):
    for action in out.get("actions") or []:
        if isinstance(action, dict) and action.get("tool") in ("ask_user", "finish"):
            return action.get("spoken") or ""
    return out.get("reply") or ""


def orchestrator_notes(out):
    """What the turn kept for the orchestrator's own later turns (`note`), as its lines: shown with the turn's
    reasoning."""
    return [line for action in out.get("actions") or [] if isinstance(action, dict) and action.get("tool") == "note"
            for line in note_lines(action.get("notes"))]


# --- a turn from native tool calls: what the model wrote (`text`) and what it called (`calls`, each
# {"tool": name, ...arguments}), as the turn object the JSON form gives. Also used on a turn still being
# written, for the live preview, so nothing here may assume a call is complete. ------------------------------

def gate_turn(text, calls):
    """No call: the text is the answer. Words written beside a delegate are that hand-over's note."""
    if not calls:
        return {"actions": [{"tool": "answer", "text": text}] if text else []}
    first = calls[0]
    if first.get("tool") == "delegate" and text and not first.get("note"):
        calls = [{**first, "note": text}, *calls[1:]]
    return {"actions": calls}


GATE_CALLING = (
    "You answer with one tool call, or with plain text and no call. Plain text with no call is your answer to the "
    "user, exactly as `answer` with that `text` (call `answer` itself only when it carries `spoken`). To hand "
    "over, call `delegate`."
)

ENDS = frozenset(cls.tool_name() for cls in NEXT)  # the calls that say what happens next


def orchestrator_turn(text, calls):
    """Text beside calls is the turn's `reply`. With no call that says what happens next (only remember, or
    none at all) the text is the final message: a finish. A turn that writes a plan is never that, whatever it
    kept beside the plan: a plan is work still to do, so the words are the reply and the runtime starts its
    first step. A file put on the phone is only ever for a step that uses it, so words beside put_on_phone are
    the reply of a turn that prepares, and the next turn makes the move. Notes to itself (`note`) change none
    of this: they go first, wherever among the calls the model wrote them. The trace's `summary` is the reply,
    or what was called."""
    notes = [call for call in calls if call.get("tool") == "note"]
    if notes:
        turn = orchestrator_turn(text, [call for call in calls if call.get("tool") != "note"])
        return {**turn, "summary": turn["summary"] or "note", "actions": [*notes, *turn["actions"]]}
    names = [call.get("tool") for call in calls]
    last = calls[-1] if calls else {}
    said = " ".join(text.split())
    prepares = "put_on_phone" in names or "set_plan" in names
    if said and not prepares and not ENDS.intersection(names):
        return {"summary": "Answering the user", "actions": [*calls, {"tool": "finish", "answer": text}]}
    if last.get("tool") in ("finish", "ask_user"):
        # Their message is in the call: the same words written beside it are not said twice.
        message = last.get("answer") or last.get("question") or ""
        text = "" if said and said in " ".join(str(message).split()) else text
        summary = "Answering the user" if last["tool"] == "finish" else "Asking the user"
    else:
        steps = last.get("steps") or []
        detail = (last.get("step") or "; ".join(str(q) for q in last.get("queries") or []) or last.get("search")
                  or ", ".join(str(n) for n in last.get("names") or [])
                  or ", ".join(str(v) for v in last.get("videos") or [])
                  or (steps[0].get("goal") if steps and isinstance(steps[0], dict) else "") or "")
        summary = said[:200] or (str(last.get("tool") or "").replace("_", " ") + (f": {detail}" if detail else ""))[:200]
    return {"summary": summary, **({"reply": text} if text else {}), "actions": calls}


ORCHESTRATOR_CALLING = (
    "You act by calling your tools; the calls of one turn run in order, under the same rules as your instructions "
    "give for a turn's actions. Plain text you write is shown to the user: beside "
    "tool calls it is the turn's `reply` (leave it out when there is nothing new to say); with no tool call at all "
    "it is your final message and ends the task, exactly as `finish` with that `answer` (call `finish` itself only "
    "when the ending carries `watch`, `stop_watching` or `spoken`). Beside `ask_user` or `finish`, write nothing: "
    "their message is in the call. Every turn that calls a tool also calls `note`, with the turn condensed for "
    "your own later turns."
)


# --- the agents --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Agent:
    name: str  # how it appears in the activity trace
    prompt: str  # agent_prompts/<prompt>.md
    tier: str  # its model: fast, heavy, or phone (the subagent model, the one that sees screenshots)
    tools: tuple  # the backend's tool classes it may call
    turn: type[BaseModel] = Turn
    max_actions: int = 1
    vision: bool = False  # may be sent screenshots
    voice: object = None  # output -> its message to the user, for the agents that speak in the chat
    spoken: object = None  # output -> what to say aloud for that message, when the user is on voice
    notes: object = None  # output -> the lines the turn kept for the agent's own later turns
    operator: tuple = ()  # phone agents: the harness tools it may call, by name
    interactions: tuple = ()  # interact's interactions it may use; empty means all of them
    support: bool = False  # on Claude, the cheaper support model instead of the agent's pick (memory, reflection)
    # An agent that answers with native tool calls: (text, calls) -> its turn, and what its prompt is told about
    # answering that way.
    compose: object = None
    calling: str = ""

    @property
    def instructions(self):
        """Its prompt, then the skills for its tools (backend/skills/tools), read on every call like the prompt."""
        guides = for_tools(cls.tool_name() for cls in self.tools)
        return load(self.prompt) + (f"\n\n{guides}" if guides else "")

    @property
    def acts(self):
        """A phone agent: it answers with native tool calls."""
        return bool(self.operator)

    @cached_property
    def schema(self):
        """The turn shape with `actions` that take this agent's tools."""
        return turn_model(self.name.title().replace(" ", "").replace("-", "") + "Turn", self.turn, self.tools, self.max_actions)

    @cached_property
    def classes(self):
        """Its own tool classes by name."""
        return {cls.tool_name(): cls for cls in self.tools}

    @cached_property
    def functions(self):
        """The function definitions it is sent: the harness's tools it may call, then its own."""
        harness = []
        for definition in TOOLS:
            if definition["function"]["name"] not in self.operator:
                continue
            if definition["function"]["name"] == "interact" and self.interactions:
                definition = copy.deepcopy(definition)
                definition["function"]["parameters"]["properties"]["interaction"]["enum"] = list(self.interactions)
            harness.append(definition)
        return harness + [function_def(cls) for cls in self.tools]


GATE = Agent(
    "Fast response", "gate", "fast",
    tools=(Answer, Delegate, Recall, ReadMemory, UpdateMemory), voice=gate_voice, spoken=gate_spoken,
    compose=gate_turn, calling=GATE_CALLING,
)

ORCHESTRATOR = Agent(
    "Orchestrator", "orchestrator", "heavy",
    tools=(SetPlan, RememberFindings, PutOnPhone, NoteToSelf, RunStep, VerifyStep, AskUser, Finish, LookUp, ReadMemory,
           Youtube),
    turn=OrchestratorTurn, max_actions=5,
    voice=orchestrator_voice, spoken=orchestrator_spoken, notes=orchestrator_notes,
    compose=orchestrator_turn, calling=ORCHESTRATOR_CALLING,
)

# Every few turns, and when it starts repeating itself, the runtime asks the subagent for a checkpoint (its own
# compaction): what it found, what did not work, where it is, its plan. That replaces its log, progress and older
# actions, so a long session stays as short to read as a new one. Checkpoint is one of its own tools, so the
# request that asks for it is the same as any other turn's up to the state, and the prompt cache covers it.
SUBAGENT = Agent(
    "Subagent", "subagent", "phone", vision=True,
    operator=(*SCREEN, *DEVICE, *LOOK, *READ),
    tools=(Log, Progress, ReadPage, Clip, AppAction, *DEVICE_TOOLS, YoutubeTranscript, AskForPasswordOrCode, TypePrivate, AskLogin, Report,
           Checkpoint),
)

# The subagent's last turn when the runtime ends its session (repeated actions, out of turns): its prompt and
# the same state, with handover as its only tool.
HANDOVER = replace(SUBAGENT, operator=(), tools=(Handover,))

VERIFIER = Agent(
    "Verifier", "verifier", "phone", vision=True,
    operator=("interact", "press_key", "swipe", "open_app", "wait_for", *LOOK, *READ),  # list_apps: see DEVICE
    interactions=(*LOOKING, "type", "submit"),  # typing only into a search box (runtime.phone.searchable)
    tools=(Note, ReadPage, Clip, Verdict),
)

MEMORY = Agent(
    "Memory", "memory", "heavy",
    tools=(ReadMemory, Remember), support=True,
)

# When a chat grows past its budget, the older messages are folded into one summary that every agent in the chat
# reads in their place (runtime/compaction.py). Runs after a reply, while the agents are idle.
COMPACTOR = Agent(
    "Compaction", "compaction", "heavy",
    tools=(Summarize,),
)

# Every few tasks, reads the recent tasks, the conversation summaries and every memory together, and writes what
# no single task showed: routines, repeated trouble, memories gone stale (runtime/reflection.py).
REFLECTION = Agent(
    "Reflection", "reflection", "heavy",
    tools=(ReadMemory, Remember), support=True,
)

# Once Relay starts to answer a new chat's first message, names the chat from it (runtime/title.py).
TITLER = Agent(
    "Title", "title", "fast",
    tools=(SetTitle,),
)

# Voice mode: which conversation the user's spoken words belong to, and a few words back (runtime/frontdesk.py).
FRONTDESK = Agent(
    "Front desk", "frontdesk", "fast",
    tools=(Route,),
)

# Whether a notification is what a chat's watch waits for, before it may wake the chat (app/watches.py). It has
# no tool that does anything: it reads what an app showed, which is someone else's words.
WATCH_CHECK = Agent(
    "Watch check", "watch_check", "fast",
    tools=(Judge,),
)

# What a burst of notifications from an app the user is briefed on is to them: noise, a line in the list, or a
# briefing (app/briefings/triage.py). Like the watch check it has no tool that does anything.
TRIAGE = Agent(
    "Triage", "triage", "fast",
    tools=(Sort,),
)

AGENTS = (GATE, ORCHESTRATOR, SUBAGENT, VERIFIER, MEMORY, COMPACTOR, REFLECTION, TITLER, FRONTDESK, WATCH_CHECK, TRIAGE)
