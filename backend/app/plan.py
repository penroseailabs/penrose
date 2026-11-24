"""The plan: the orchestrator's ordered steps, and the compact record of how the run is going.

A run's state holds:
- `plan`: the steps, in order. Each is a StepSpec the orchestrator wrote, plus what the runtime
  recorded about it (status, attempts, the subagent's report, the verifier's verdict).
- `facts`: what reports established that later steps need ("logged_in_as": "+1 512…").
- `journal`: one line per thing that happened ("s1 done: logged in with an OTP"). This is the
  compacted chain the orchestrator reads instead of transcripts.

A subagent's own turns never reach the orchestrator: when it ends, its report is all that goes up.
"""

import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# How many journal lines the orchestrator reads; older ones are summed up in a count.
JOURNAL_LINES = 60

# Step statuses. A step starts pending and runs until its subagent reports:
#   done         the subagent reported it completed (verified=True once a verifier agreed)
#   failed       the subagent or the verifier says it is not done (`problem` says why)
#   unfinished   the runtime ended the subagent's session (repeated actions, out of turns) and it handed
#                over: what it found, where it is, why it did not finish (`report.handover`)
#   needs_input  the subagent needs a choice or value; the orchestrator answers or asks the user
#   waiting_user the subagent waits for the user's private entry or the Google card; its session is kept
#   resume       the user answered that; the same session continues
#   stopped      the user pressed Stop while it worked
ACTIVE = ("running", "verifying", "waiting_device")


class AppTarget(BaseModel):
    name: str = Field(description="The app, e.g. Instacart.")
    package: str = Field(default="", description="Its official Android package ID when known, e.g. com.instacart.client.")
    website: str = Field(default="", description="The website to use only if no suitable app exists.")


class CallBrief(BaseModel):
    """What penrose's voice may do on a call: everything it knows about the call, since it decides nothing else."""

    contact: str = Field(max_length=200, description="Who is called, as penrose addresses them, e.g. 'Grace'.")
    purpose: str = Field(
        max_length=1000,
        description="What to find out or tell them, e.g. 'Ask where the spare house keys are'.",
    )
    questions: list[str] = Field(default_factory=list, max_length=6, description="The questions to ask, in order.")
    caller: str = Field(
        default="", max_length=100,
        description="The user's name as the person knows them, e.g. 'Alex': penrose opens with 'calling for Alex'. "
        "Empty only when the call is to the user themself.",
    )
    may_share: str = Field(
        default="", max_length=1000,
        description="What it may tell them: the user's name as they know it, and the context the purpose needs "
        "('calling for Alex, who is locked out'). Nothing else about the user is shared.",
    )
    language: str = Field(default="", max_length=10, description="ISO code of the call's language; empty: the user's.")
    max_minutes: int = Field(default=5, ge=1, le=15, description="Hard limit; the call is ended at this length.")


class StepSpec(BaseModel):
    """One step of the plan: a whole outcome in one app (see Planning)."""

    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$", description="Short and stable, e.g. s1, cart, order.")
    goal: str = Field(
        max_length=1500,
        description="The outcome, naming the app.",
    )
    app: AppTarget | None = Field(default=None, description="The app this step works in.")
    kind: Literal["question", "action", "call"] = Field(
        default="action",
        description="question: reads and reports, on a short budget. action: changes something, on the long budget. "
        "call: a voice call to a person; needs `call`. See Planning.",
    )
    call: CallBrief | None = Field(default=None, description="For a call step only: what the voice may say and ask.")
    instructions: str = Field(
        default="", max_length=4000,
        description="What the subagent must respect in this step: exact values to enter, choices already made, limits "
        "on what it may confirm or pay. Not how to use the phone: it knows that. Not the goal again, and not what "
        "the plan's `context` already says.",
    )
    done_when: str = Field(
        max_length=1000,
        description="What is true on the phone when the whole step is done, specific enough to check: the end state "
        "in a line or two.",
    )
    memories: list[str] = Field(
        default_factory=list, max_length=8,
        description="Names from `memory_index` of the memories this step turns on: the user's rules for this app or "
        "this kind of purchase, payment or message, and what it will have to enter or choose (an address, a size). "
        "The subagent reads them in full. Besides the notes on its own app it sees no other memory, so a rule "
        "you do not name here is a rule it does not know.",
    )

    @field_validator("memories", mode="after")
    @classmethod
    def names_as_listed(cls, names):
        return memory_names(names)


def memory_names(names):
    """Memory names as the index lists them. A name copied with the rest of its line ("call-on-whatsapp
    (feedback):") or in quotes is still that name: its first word, without the punctuation around it."""
    words = [(str(name).split() or [""])[0].strip("`'\"[]():,.") for name in names]
    return list(dict.fromkeys(word for word in words if word))


def new_step(spec: StepSpec):
    return {**spec.model_dump(), "status": "pending", "attempts": 0, "turns": []}


def find(state, step_id):
    return next((s for s in state.get("plan", []) if s["id"] == step_id), None)


def next_step(plan):
    """The step that runs next: the first one not done. The plan runs in order."""
    return next((s for s in plan if s["status"] != "done"), None)


def log(state, line):
    state.setdefault("journal", []).append({"at": time.time(), "line": line[:400]})


def journal_view(state):
    lines = [j["line"] for j in state.get("journal", [])]
    if len(lines) > JOURNAL_LINES:
        return [f"({len(lines) - JOURNAL_LINES} earlier lines omitted)", *lines[-JOURNAL_LINES:]]
    return lines


def step_view(step):
    """One step as the orchestrator reads it: the brief and how it ended, never the subagent's turns."""
    view = {k: step[k] for k in ("id", "goal", "app", "kind", "instructions", "done_when", "memories", "call") if step.get(k)}
    view["status"] = step["status"]
    if step.get("attempts"):
        view["attempts"] = step["attempts"]
    if step.get("verified"):
        view["verified"] = True
    if step.get("report"):
        view["report"] = step["report"]
    if step.get("problem"):
        view["problem"] = step["problem"]
    if step.get("pictures"):
        view["pictures"] = step["pictures"]
    if not step.get("report") and (kept := unreported(step)):
        view["unreported_session"] = kept
    if uncertain := [t["summary"] for t in step.get("turns", []) if t.get("status") == "uncertain"]:
        view["uncertain_actions"] = uncertain[-5:]
    if step["status"] == "waiting_user" and step.get("question"):
        view["waiting_for"] = ("the user's private entry" if step["question"].get("sensitive")
                               else "the user to sign in on the phone" if step["question"].get("kind") == "login" else "the user")
    return view


def unreported(step):
    """What a session that ended without a report (Stop, a restart of the services) kept: its last checkpoint,
    progress, log or notes and the screens it went through, so its findings are not lost with it."""
    kept = {}
    for mem in (step.get("sessions") or {}).values():
        for key, name in (("checkpoint", "checkpoint"), ("progress", "progress"), ("log", "log"), ("notes", "notes"),
                          ("trail", "screens")):
            if mem.get(key):
                kept[name] = mem[key]
    return kept


def plan_view(state):
    """The plan as the orchestrator reads it. A report's facts are also in the run's `facts` (where later reports
    update them), so the report names them and `facts` holds their values: sent once, not with every step."""
    steps = []
    for step in state.get("plan", []):
        view = step_view(step)
        report = view.get("report")
        if isinstance(report, dict) and isinstance(report.get("facts"), dict):
            view["report"] = {**report, "facts": list(report["facts"])}
        steps.append(view)
    return steps


# --- the view the current frontend draws ------------------------------------------------------------

FRONTEND_STATUS = {"done": "succeeded", "verifying": "running", "resume": "pending", "unfinished": "blocked"}


def node_views(state):
    """`state["nodes"]`: the plan as the frontend's task graph reads it, each step a node that depends on
    the one before. Written on every save and never read back by the runtime."""
    nodes, previous = {}, None
    for step in state.get("plan", []):
        report = step.get("report") or {}
        node = {
            "status": FRONTEND_STATUS.get(step["status"], step["status"]),
            "attempt": step.get("attempts", 0),
            "output": report.get("summary", ""),
            "turns": step.get("turns", []),
            "spec": {
                "id": step["id"], "objective": step["goal"], "app": step.get("app"),
                "steps": [step["goal"]], "depends_on": [previous] if previous else [],
                "system_prompt": step.get("instructions", ""), "tools": [], "verifier": step["done_when"],
            },
        }
        if step.get("problem"):
            node["error"] = step["problem"]
        if step.get("waiting"):
            node["waiting"] = step["waiting"]
        if step.get("verification"):
            node["verification"] = step["verification"]
        if step.get("question"):
            node["question"] = step["question"]
        if step.get("answers"):
            node["guidance"] = step["answers"]
        if state.get("slot"):
            node["slot"] = state["slot"]
        nodes[step["id"]] = node
        previous = step["id"]
    return nodes
