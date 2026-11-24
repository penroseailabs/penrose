"""The orchestrator's tools. A turn is at most one set_plan followed by exactly one of the others.

Each tool checks itself against the plan (`check`, which raises Rejected: the orchestrator is told why
and decides again) and then does its part through the orchestrator runtime (`run`).
"""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..errors import Rejected
from ..plan import AppTarget, StepSpec, find, new_step, next_step
from ..watches import words
from .base import Tool
from .memory import ReadMemory
from .youtube import Youtube


# A look_up query written as a Gmail search: an operator (`from:`, `subject:(...)`, `newer_than:30d`, `-category:
# promotions`), an upper-case OR, braces, or a word excluded with a minus. Gmail's server search reads these; every
# other source (Google, the phone's apps, the user's browsing, notifications, earlier chats) would take them for
# words, so such a query is searched in Gmail only.
GMAIL_OPERATOR = re.compile(
    r"(?:^|[\s({])-?(?:from|to|cc|bcc|subject|label|category|in|is|has|filename|after|before|older_than|newer_than"
    r"|older|newer|list|deliveredto|larger|smaller|rfc822msgid):\S|\{|(?:^|\s)-(?:[^\W\d_]|[\"(])",
    re.IGNORECASE)
GMAIL_CONNECTIVE = re.compile(r"\sOR\s|\sAROUND\s")  # upper case only: "rock or roll" is words
MAIL_QUERY_CHARS = 300  # a Gmail search with its OR lists is longer than a few words


def gmail_search(query):
    """Whether a look_up query is a Gmail search (operators), searched in Gmail only."""
    return bool(GMAIL_OPERATOR.search(query) or GMAIL_CONNECTIVE.search(query))


class Watch(BaseModel):
    """Something this task leaves open that will come back later as a notification (see Watching). Until it
    expires, a notification with the words in `match` is passed to this conversation as it arrives."""

    what: str = Field(
        min_length=3, description="What is awaited, as the user reads it under your message ('Jordan's reply with his address'). "
        "With `then`, the exact event with every condition the user put on it: a notification is judged from these "
        "words alone.",
    )
    match: list[str] = Field(
        min_length=1, max_length=4,
        description="One to four short searches, written as for look_up. A notification that has every word of one "
        "of them (in its app's name, its website, who it is from or its text) is passed on: '112-5550142-0042137', "
        "'Anker charger', 'Jordan Lee'.",
    )
    then: str = Field(
        default="", description="Only when the user asked for something to be done once it arrives: the whole instruction, "
        "standing on its own, with every value the user gave ('Jordan has sent his address: book an Uber from Home to "
        "it for 6 pm today, UberX, and tell the user the fare'). Empty: the notification is only passed on.",
    )
    apps: list[AppTarget] = Field(
        default_factory=list, max_length=4,
        description="With `then`, required: the app the notification will come from and the apps `then` needs, each "
        "with its package ID.",
    )
    days: int = Field(default=14, ge=1, le=60, description="How long to keep watching.")


WATCH = ("What this task leaves open that will come back as a notification: see Watching in your instructions. "
         "Empty when nothing is awaited, which is most turns.")
MAX_VERIFICATIONS = 2  # checks of one step: the first, and one more after it failed

STOP_WATCHING = ("The ids of watches in `watching` that are over: what they waited for has come, or the user no "
                 "longer wants it.")


def check_watches(tool):
    for w in tool.watch:
        if not any(len(found := words(query)) and max(map(len, found)) >= 3 for query in w.match):
            raise Rejected(f"The watch {w.what!r} needs a search in `match` with a word of three letters or more.")
        if w.then.strip() and not any(app.package.strip() for app in w.apps):
            raise Rejected(f"The watch {w.what!r} has `then`, so it needs `apps`: the app the notification will "
                           "come from and the apps the instruction needs, each with its package ID.")


class SetPlan(Tool):
    """Write the plan: the steps still to do, in order. Steps already done stay as they are, unless you
    list a step with the same id, which starts it over. Every step not done and not listed is dropped."""

    tool: Literal["set_plan"] = "set_plan"
    context: str = Field(
        default="", description="The shared context every subagent reads (see `context` under Planning). Empty keeps the "
        "current one.",
    )
    steps: list[StepSpec] = Field(min_length=1, max_length=12)

    def merged(self, plan):
        """The plan this would make, without changing anything."""
        listed = {s.id for s in self.steps}
        kept = [s for s in plan if s["status"] == "done" and s["id"] not in listed]
        if len(listed) != len(self.steps):
            raise Rejected("Step ids must be unique.")
        return kept + [new_step(s) for s in self.steps]

    def check(self, plan, state):
        self.merged(plan)

    async def run(self, o):
        o.set_plan(self)


class RunStep(Tool):
    """Start a fresh subagent on the next step (see Running). A step the user just unblocked privately
    continues instead."""

    tool: Literal["run_step"] = "run_step"
    step: str = Field(description="The step's id. It must be the first step not done: the plan runs in order.")
    note: str = Field(
        default="", description="What this subagent must know beyond the step: an answer to its question, a correction from "
        "the user, why the last attempt failed and what to do differently.",
    )

    def check(self, plan, state):
        step = find({"plan": plan}, self.step)
        if not step:
            raise Rejected(f"There is no step {self.step!r} in the plan.")
        first = next_step(plan)
        if step is not first:
            raise Rejected(f"Steps run in order: the next one is {first['id'] if first else 'none (all are done)'}, not {self.step}.")
        if step.get("kind") == "call" and not step.get("call"):
            raise Rejected(f"{self.step} is a call step without `call`: set_plan it with who, the purpose and what "
                           "the voice may share.")
        if step["status"] != "waiting_user" and step["status"] != "resume" and step.get("attempts", 0) >= settings.max_step_attempts:
            raise Rejected(
                f"{self.step} has had {step['attempts']} attempts. Change it with set_plan (a narrower goal, another "
                "approach), ask the user, or finish. A step stuck in an app's login goes to the user, not to another app."
            )

    async def run(self, o):
        step = find(o.state, self.step)
        if step["status"] == "waiting_user":  # the user gave what it waits for in the conversation: it continues
            step["status"] = "resume"
            step.pop("question", None)
        await o.run_step(self.step, self.note)


class VerifyStep(Tool):
    """Send a read-only verifier to check on the phone that a done step's result is really there (when: see
    Running)."""

    tool: Literal["verify_step"] = "verify_step"
    step: str = Field(description="The id of a step that is done.")
    claim: str = Field(description="What must be true, e.g. '2 × Horizon Organic whole milk (half gallon) are in the Instacart cart'.")
    how: list[str] = Field(
        default_factory=list, max_length=6,
        description="The checks to make, in order, e.g. ['Open the Instacart cart', 'Read the item names and quantities'].",
    )

    def check(self, plan, state):
        step = find({"plan": plan}, self.step)
        if not step:
            raise Rejected(f"There is no step {self.step!r} in the plan.")
        if step["status"] == "done":
            return
        # A step that reported completed and then failed only its check may be checked again, once (a narrower
        # claim, another screen): the verifier changes nothing, and an unsure check is not a failed booking.
        failed_check = step["status"] == "failed" and (step.get("verification") or {}).get("passed") is False
        if failed_check and step.get("verifications", 0) < MAX_VERIFICATIONS:
            return
        if failed_check:
            raise Rejected(
                f"{self.step} has been checked {step['verifications']} times and failed its check. Do not verify it "
                "again: finish and tell the user what the step reported and what the check could not confirm, ask the "
                "user, or run the step again with set_plan if it really did not happen."
            )
        raise Rejected(
            f"Only a done step can be verified; {self.step} is {step['status']}. Run it (run_step), change it "
            "(set_plan), ask the user, or finish."
        )

    async def run(self, o):
        await o.verify_step(self.step, self.claim, self.how)


class Option(BaseModel):
    """An answer the user can give with one tap."""

    label: str = Field(
        min_length=1, max_length=48,
        description="What the button says: a few words, a verb first for something you would do ('Compare the "
        "drafts', 'Draft a reply'), or the choice itself ('Jordan Kim').",
    )
    message: str = Field(
        min_length=1, max_length=600,
        description="What is sent as the user's own answer when they tap it, complete without the label: 'Open the "
        "lease draft Dana emailed and list what changed since the version I signed', 'Jordan Kim'.",
    )


class AskUser(Tool):
    """Ask the user and wait for the answer (when, and what never to ask: see The user)."""

    tool: Literal["ask_user"] = "ask_user"
    question: str = Field(description="Your message to the user, in markdown.")
    needs: str = Field(
        default="", max_length=120,
        description="What you need from the user, as a line in a list of things waiting for them: at most eight words, "
        "starting with a verb, naming the thing ('Choose how to pay $45 at Target', 'Say which Jordan to message'). "
        "Always give it.",
    )
    options: list[Option] = Field(
        default_factory=list, max_length=4,
        description="When the answers are few and known (which of two people, the things you offer to do at the end "
        "of a briefing): each as a button under your message. The user can still write something else. Empty for "
        "an open question.",
    )
    spoken: str = Field(
        default="", description="With `voice` in what you read: the same, as you would say it aloud. One to three plain "
        "sentences, no markdown. Otherwise empty.",
    )

    private: Literal["", "card", "password", "code", "other"] = Field(
        default="",
        description="Ask for a private value through a form instead of a written answer (see Private entries). "
        "card: number, expiry, CVV and name together; password; code: a one-time or verification code; other: name "
        "it in `private_label`. Empty for an ordinary question.",
    )
    private_label: str = Field(
        default="", description="With `private`: what it is for, as the user reads it over the form, e.g. 'Amazon password', "
        "'Card for the Hulu signup', 'Passport number'. Needed for other.",
    )
    watch: list[Watch] = Field(default_factory=list, max_length=3, description=WATCH)
    stop_watching: list[int] = Field(default_factory=list, max_length=8, description=STOP_WATCHING)

    def check(self, plan, state):
        if not self.question.strip():
            raise Rejected("ask_user needs question.")
        if self.private == "other" and not self.private_label.strip():
            raise Rejected("ask_user.private other needs private_label: what the value is.")
        check_watches(self)

    async def run(self, o):
        o.ask_user(self.question, {"kind": self.private, "label": self.private_label.strip()} if self.private else None,
                   **({"options": [option.model_dump() for option in self.options]} if self.options else {}),
                   **({"needs": self.needs} if self.needs.strip() else {}))
        o.watch(self)


class Finish(Tool):
    """End the task with the final message to the user (see What you say)."""

    tool: Literal["finish"] = "finish"
    answer: str = Field(description="Your final message to the user, in markdown.")
    spoken: str = Field(
        default="", description="With `voice` in what you read: the same, as you would say it aloud. One to three plain "
        "sentences, no markdown. Otherwise empty.",
    )

    watch: list[Watch] = Field(default_factory=list, max_length=3, description=WATCH)
    stop_watching: list[int] = Field(default_factory=list, max_length=8, description=STOP_WATCHING)

    def check(self, plan, state):
        if not self.answer.strip():
            raise Rejected("finish needs answer.")
        check_watches(self)

    async def run(self, o):
        await o.finish(self.answer, self.spoken)
        o.watch(self)


class LookUp(Tool):
    """Search, all at once and in about a second (two with `email`), with nothing opened on the phone: the
    phone (the penrose home app's search: apps and their screens, contacts, settings; with `email`, the user's
    Gmail), Google (in the phone's Chrome: the AI Overview, answer cards, the top results), earlier penrose
    conversations and the phone's notifications; with `apps`, Google Play too (the apps that do this, marked
    with the ones the user has). When: What the user means and Where to look; how to write the queries: the
    look-up skill. What the user searched for and the pages they went to on their other devices come with
    it, when some have a query's words. Ends the turn (a `youtube` and a `read_memory` may go with it); you
    are called again with `looked_up`. Up to twelve per user message."""

    tool: Literal["look_up"] = "look_up"
    queries: list[str] = Field(
        min_length=1, max_length=4,
        description="One to four short searches: the few words a thing of the user's is called on the phone or in an "
        "email: a name, a brand, a product, an order or store, a person ('fridge', 'refund', 'Jordan'); or a "
        "question as the user would type it into Google ('will it rain tomorrow'). Or a Gmail search with Gmail's "
        "operators (`from:amazon subject:(ordered OR \"your order\")`): it searches the user's email only, never "
        "the other sources (the email-search skill).",
    )
    email: bool = Field(
        default=False,
        description="Also search the user's email for the plain queries (a second or two more). When: see What the "
        "user means. A Gmail search needs no `email`.",
    )
    apps: bool = Field(
        default=False,
        description="Also search Google Play for the apps that do what each query says, each marked when it is on "
        "this phone or the user installed it before on any of their devices (a second more). A query that is a "
        "package ID ('com.dd.doordash') returns what that app says it does. When: see What the user means.",
    )

    def check(self, plan, state):
        if any(not q.strip() for q in self.queries):
            raise Rejected("Every look_up query needs words.")
        if len(state.get("looked_up", [])) >= settings.max_lookups:
            raise Rejected(f"You looked up {settings.max_lookups} times for this message: decide from `looked_up`.")

    async def run(self, o):
        queries = [q.strip()[:MAIL_QUERY_CHARS if gmail_search(q) else 100] for q in self.queries]
        await o.look_up(queries, email=self.email, apps=self.apps)


class RememberFindings(Tool):
    """Commit what this task found about the user's world to the long-term memory, so a later task starts from
    it (what and when: see `remember` under Running). The memory agent decides what sticks: nothing sensitive or
    private about third parties, nothing an app asked to have remembered."""

    tool: Literal["remember"] = "remember"
    findings: list[str] = Field(
        min_length=1, max_length=12,
        description="One finding each, in plain words, with its date and source: 'Morgan Blake (Engineering "
        "Manager, Northwind) is the user's collaborator on the Q3 launch; read in Teams chat \"Launch prep\", 27 Oct "
        "2026'. Facts about the user's world, not the task's own steps or what the user said here.",
    )

    def check(self, plan, state):
        if any(not f.strip() for f in self.findings):
            raise Rejected("Every finding needs words.")

    async def run(self, o):
        await o.remember([f.strip()[:400] for f in self.findings])


class NoteToSelf(Tool):
    """Condense this turn for your own later turns: call it on every turn, beside the turn's other calls (see
    `note` under Running). You are called afresh every turn and read only what your instructions list: none of
    this turn's thinking reaches the next one, which starts from these lines or works it all out again. It does
    nothing on the phone and the user does not read it."""

    tool: Literal["note"] = "note"
    # Plain text, not a list: a model writes one note as a sentence, and a sentence where a list belongs reaches
    # us as arguments nobody can read (Claude, 2026-11-22: the turn was lost, twice, and the task with it) or as
    # the wrong type (DeepSeek). Text is quoted by every provider, so this cannot arrive broken. note_lines
    # makes the lines of it.
    notes: str = Field(
        description="Plain text, one to four lines, a new line for each (bullets if you like). Each stands on its "
        "own with its reason or source: what you concluded "
        "or chose ('Pick: the hiking daypack, Sam camps most weekends; the headlamp and the "
        "stove are the alternatives'), what you found yourself ('His birthday is June 14 (Gmail: a calendar "
        "reminder)'), what you ruled out ('Ruled out the camp stove: Sam already has one (Amazon order email)'), what comes next and "
        "what is left unexplored ('After s2: ask which of the three to order. Not checked: Target order history').",
    )

    @field_validator("notes", mode="before")
    @classmethod
    def as_text(cls, value):
        """A list of lines (the tool's earlier shape, which a model may still write) is taken as its lines."""
        return "\n".join(str(item) for item in value) if isinstance(value, (list, tuple)) else value

    def check(self, plan, state):
        """Nothing to refuse: an empty note keeps nothing, and a long one is cut."""

    async def run(self, o):
        o.note(note_lines(self.notes))


NOTE_LENGTH = 1800  # a turn's note in all (what its six lines of 300 came to as a list); the rest is cut, never refused
BULLET = re.compile(r"^\s*(?:[-*•–—]|\d{1,2}[.)])\s+")


def note_lines(text):
    """A turn's note as the lines that are kept, read on later turns and shown in the chat: one for each line
    the model wrote, without its bullet or number. Also reads a note still being written, and a list."""
    if isinstance(text, (list, tuple)):
        text = "\n".join(str(item) for item in text)
    lines = (" ".join(BULLET.sub("", line).split()) for line in str(text or "")[:NOTE_LENGTH].splitlines())
    return [line for line in lines if line]

PHONE_REF = re.compile(r"(file|image):\d+")


class PutOnPhone(Tool):
    """Put files on the phone, in its Downloads/penrose folder, so an app or the Files app can use them (a photo to
    post, a PDF to send, an APK to install, screenshots to share): files the user attached to this chat (`file:N`,
    in the messages and `attached_files`), by the same route as the user's own Upload to phone, and pictures the
    subagents clipped (`image:N` in `pictures`), as JPEGs named after their captions. The journal then says each
    one's path on the phone (`phone_files` lists the user's files, `pictures` the pictures'): name that path in the
    step's note. Nothing already on the phone is put there again."""

    tool: Literal["put_on_phone"] = "put_on_phone"
    files: list[str] = Field(
        min_length=1, max_length=10,
        description='The refs of the files and pictures to put on the phone: ["file:12", "image:601"].',
    )

    def check(self, plan, state):
        if any(not PHONE_REF.fullmatch(f.strip()) for f in self.files):
            raise Rejected('Name each file or picture by its ref, like "file:12" or "image:601".')

    async def run(self, o):
        await o.put_on_phone([f.strip() for f in self.files])


NEXT = (RunStep, VerifyStep, AskUser, Finish, LookUp, Youtube, ReadMemory)
# The tools that only gather: they may end a turn together, each once.
GATHER = (LookUp, Youtube, ReadMemory)
