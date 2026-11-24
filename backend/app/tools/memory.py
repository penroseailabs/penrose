"""The memory agent's tool (save and forget named memories), the tool that reads memories in full, and the gate's
(update the memory now, when that is all the user asks)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..errors import Rejected
from ..plan import memory_names
from .base import Tool


class Note(BaseModel):
    """One memory: a single fact, preference or piece of app know-how."""

    name: str = Field(
        pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$",
        description="Short kebab-case id, a few words: 'home-address', 'groceries-on-instacart', 'instacart-login'. Saving under an "
        "existing name replaces that memory: update it rather than adding a near-duplicate.",
    )
    type: Literal["user", "person", "thing", "reference", "feedback", "open", "app"] = Field(
        description="user: who the user is (name, city, addresses, their own phone numbers, emails and accounts, "
        "birthday, household, diet, sizes). person: someone in the user's life, one memory each, a pet too (who they "
        "are to the user, the name they are saved under, how the user reaches them). thing: what the user owns "
        "(make, model, serial number, where bought), a place they go (their gym, the clinic), or what they do or "
        "order regularly. reference: where something is kept or found (which app, account, group or document holds "
        "what). feedback: how the user wants the work done, a correction or a confirmed choice ('ask before paying', "
        "'groceries from Instacart'). open: what is pending right now and will be over (something owed to or by "
        "someone, an order, return or complaint in progress, a plan or shortlist being worked on), with `expires`. "
        "app: how an app works, learned on the phone (its login method, where a setting or button lives, a dialog "
        "that must be dismissed).",
    )
    description: str = Field(
        description="The memory's line in the index (under about 150 characters): the one part of it every agent always "
        "reads, and all most tasks ever read. Put the fact or the rule itself in it when it is evident, otherwise "
        "what was seen and where, with its exact value: 'Home "
        "address is 4500 Mueller Blvd, Austin, TX'; 'Always check the coupons on the DoorDash cart before ordering'. "
        "Never a label for it ('The user's address', 'DoorDash preference').",
    )
    content: str = Field(
        default="",
        description="Only what the line has no room for: further exact values, the steps that work in an app. Empty "
        "when the line says it all, which is most of the time; never a value or a rule the line already gives. It "
        "is read only when an agent asks for this memory by name, and always after its line. A 'Why:' line only "
        "when the reason will matter later (a correction: what went wrong and what the user said), never one that "
        "only says the user asked, and no line that only gives the date. A 'How to apply:' line only when applying "
        "it takes more than the line tells. About 600 characters at most.",
    )
    app: str = Field(default="", description="type app: the Android package, e.g. 'com.instacart.client'.")
    expires: str = Field(
        default="", pattern=r"^(\d{4}-\d{2}-\d{2})?$",
        description="YYYY-MM-DD after which the memory no longer holds and is deleted: a trip ('in Mexico City "
        "until 11 Apr' expires 2027-04-11), a stay, a temporary address, a one-off date, and for an open matter the "
        "day it stops mattering (the deadline, the delivery; 30 days on when left empty). Empty for a lasting fact.",
    )

    @model_validator(mode="after")
    def app_note_names_its_app(self):
        if self.type == "app" and not self.app.strip():
            raise ValueError("an app memory needs app: the package it is about")
        return self


class Remember(Tool):
    """Update the long-term memory. Both lists may be empty: most runs hold nothing worth keeping.

    Lengths here are guidance in the descriptions, not limits: a reply a little over one is still kept. A hard
    limit failed the whole update (every note) when one note ran long, and the retry rarely
    helped, since models do not count characters. The memory's size is held by consolidation instead."""

    tool: Literal["remember"] = "remember"
    save: list[Note] = Field(default_factory=list, description="Memories to create, or to replace by name; rarely "
                             "more than a few.")
    forget: list[str] = Field(
        default_factory=list,
        description="Names of memories that are wrong or outdated, that the user asked to forget, or that a saved "
        "memory merges.",
    )
    declined: list[str] = Field(
        default_factory=list,
        description="What the user asked to have kept that you did not keep, one short line each saying what and why: "
        "'Card number: never kept', 'Immigration status: never kept'. Only for the user's own requests (user_added, "
        "user_asked, or a 'remember...' in their messages); the user is told.",
    )


class ReadMemory(Tool):
    """Read memories in full, by name. `memory_index` has one line per memory, and that line is often all you
    need. Read a memory when you need what it says beyond its line: an exact value to give or enter, the why
    and how of a rule, what is known of a person or a place. All the names you need in one call. Ends the turn:
    you are called again with `memories_read`. Never to browse: only the memories this message or task turns on."""

    tool: Literal["read_memory"] = "read_memory"
    names: list[str] = Field(
        min_length=1, max_length=8,
        description="Names from `memory_index`: the first word of a line, such as 'home-address', without its type.",
    )

    @model_validator(mode="after")
    def names_are_names(self):
        self.names = memory_names(self.names)
        if not self.names:
            raise ValueError("read_memory needs the names of the memories to read")
        return self

    def check(self, plan, state):
        if all(name in state.get("memories_read", []) for name in self.names):
            raise Rejected("These memories are already in `memories_read`: decide from them.")

    async def run(self, o):
        o.read_memory(self.names)


class UpdateMemory(Tool):
    """The user only asks you to remember, change or forget something about them or how they want things done
    ("remember I'm vegetarian", "forget my old address", "I moved to Denver, keep that in mind"): the memory is
    updated now, and you are called again with `memory_updated` (what was saved, forgotten, or not kept and why)
    to answer from. Once per message. A message that also asks for work is delegated instead: what it asks to
    remember is kept after the task."""

    tool: Literal["update_memory"] = "update_memory"
    request: str = Field(
        description="What to remember, change or forget, in the user's words, with what the conversation adds (a "
        "'yes' to 'Is 512-555-0100 your number?' becomes 'My phone number is 512-555-0100').",
    )
